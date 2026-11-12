"""Immutable tenant assets, validated metadata and bounded ZIP/CSV interchange."""
from __future__ import annotations

import csv
import hashlib
import io
import json
import re
import secrets
import stat
import warnings
import zipfile
from datetime import timedelta
from pathlib import Path, PurePosixPath
from uuid import UUID, uuid4

from PIL import Image, UnidentifiedImageError
from sqlalchemy import select, update

from app.core.tenant_context import require_tenant_id
from app.core.time import ensure_shanghai_datetime, now_shanghai
from app.models.guess_image import GuessImageImport, GuessImageQuestion
from plugins._shared.resources import resolve_owned_path, tenant_media_root

CODE = "guess_image"
MAX_IMAGE = 5 * 1024 * 1024
MAX_PIXELS = 4_000_000
MAX_ZIP = 20 * 1024 * 1024
MAX_EXPANDED = 80 * 1024 * 1024
MAX_ROWS = 1000
COLUMNS = ("id", "image", "answer", "category", "tags", "source", "license", "status")
STATUSES = {"draft", "enabled", "disabled"}


class BankError(ValueError):
    pass


def root() -> Path:
    return tenant_media_root(require_tenant_id(), CODE)


def asset_path(asset_id: str, *, thumbnail: bool = False) -> Path:
    if not re.fullmatch(r"[a-f0-9]{64}", asset_id):
        raise BankError("图片编号不合法")
    try:
        return resolve_owned_path(root(), f"images/{asset_id}.{'thumb.png' if thumbnail else 'image'}")
    except (OSError, ValueError) as exc:
        raise BankError("图片不存在或不属于当前租户") from exc


def decode_image(data: bytes) -> tuple[str, bytes]:
    if not data or len(data) > MAX_IMAGE:
        raise BankError("图片必须在 5 MiB 以内")
    try:
        with warnings.catch_warnings():
            warnings.simplefilter("error", Image.DecompressionBombWarning)
            with Image.open(io.BytesIO(data)) as picture:
                if picture.format not in {"PNG", "JPEG"} or getattr(picture, "n_frames", 1) != 1:
                    raise BankError("仅支持静态 PNG / JPEG 图片")
                if picture.width * picture.height > MAX_PIXELS:
                    raise BankError("图片不能超过 400 万像素")
                picture.load()
                picture = picture.convert("RGB")
                picture.thumbnail((360, 240))
                output = io.BytesIO()
                picture.save(output, format="PNG")
        return hashlib.sha256(data).hexdigest(), output.getvalue()
    except (UnidentifiedImageError, OSError, ValueError, Image.DecompressionBombError, Image.DecompressionBombWarning) as exc:
        if isinstance(exc, BankError):
            raise
        raise BankError("图片解码失败") from exc


def _atomic(path: Path, data: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{uuid4().hex}.tmp")
    try:
        temporary.write_bytes(data)
        temporary.replace(path)
    finally:
        temporary.unlink(missing_ok=True)


def save_asset(data: bytes) -> str:
    asset_id, thumbnail = decode_image(data)
    # Content addressed, never mutate or eagerly unlink an image used by a running game.
    directory = root() / "images"
    directory.mkdir(exist_ok=True)
    for suffix, content in (("image", data), ("thumb.png", thumbnail)):
        path = resolve_owned_path(root(), f"images/{asset_id}.{suffix}", must_exist=False)
        if not path.exists():
            _atomic(path, content)
    return asset_id


def metadata(values: dict) -> dict:
    result = {}
    for key, limit in (("answer", 80), ("category", 40), ("source", 500), ("license", 500)):
        value = str(values.get(key) or "")
        if key != "answer":
            value = value.strip()
        if len(value) > limit or any(ord(c) < 32 for c in value):
            raise BankError(f"{key} 超长或包含控制字符")
        result[key] = value
    if not result["answer"].strip():
        raise BankError("答案不能为空；匹配时保留大小写及空格")
    tags = values.get("tags", "")
    tags = tags.split("|") if isinstance(tags, str) else tags
    tags = list(dict.fromkeys(str(item).strip() for item in (tags or []) if str(item).strip()))
    if len(tags) > 12 or any(len(tag) > 24 or any(ord(c) < 32 for c in tag) for tag in tags):
        raise BankError("最多 12 个标签，每个不超过 24 字")
    result["tags_json"] = json.dumps(tags, ensure_ascii=False)
    result["status"] = str(values.get("status") or "draft")
    if result["status"] not in STATUSES:
        raise BankError("题目状态不合法")
    if result["status"] == "enabled" and (not result["source"] or not result["license"]):
        raise BankError("启用题目必须填写来源及授权说明")
    return result


def save_question(db, values: dict, data: bytes | None, *, question=None):
    validated = metadata(values)
    asset_id = save_asset(data) if data else (question.asset_id if question else "")
    if not asset_id:
        raise BankError("请上传题目图片")
    if question is None:
        question = GuessImageQuestion(tenant_id=require_tenant_id(), question_id=str(uuid4()), asset_id=asset_id)
        db.add(question)
    for key, value in {**validated, "asset_id": asset_id}.items():
        setattr(question, key, value)
    db.flush()
    return question


def _safe_zip_name(name: str) -> bool:
    path = PurePosixPath(name)
    return bool(name and not name.startswith("/") and "\\" not in name and ":" not in name
                and not any(part in {"..", ".", ""} for part in name.rstrip("/").split("/"))
                and not path.is_absolute())


def read_archive(data: bytes) -> tuple[list[dict], dict[str, bytes]]:
    if len(data) > MAX_ZIP:
        raise BankError("ZIP 不能超过 20 MiB")
    try:
        with zipfile.ZipFile(io.BytesIO(data)) as archive:
            infos = archive.infolist()
            if len(infos) > 2 * MAX_ROWS + 10 or sum(item.file_size for item in infos) > MAX_EXPANDED:
                raise BankError("ZIP 条目过多或解压后超过 80 MiB")
            names = set()
            files = {}
            for item in infos:
                if not _safe_zip_name(item.orig_filename) or not _safe_zip_name(item.filename) or item.filename.casefold() in names:
                    raise BankError("ZIP 包含不安全或重复的路径")
                names.add(item.filename.casefold())
                mode = item.external_attr >> 16
                if stat.S_ISLNK(mode) or item.flag_bits & 1 or item.compress_type not in {0, 8}:
                    raise BankError("不支持符号链接、加密或特殊压缩方式")
                if item.is_dir():
                    continue
                if item.filename != "questions.csv" and not (
                    item.filename.startswith("images/") and PurePosixPath(item.filename).suffix.lower() in {".png", ".jpg", ".jpeg"}
                ):
                    raise BankError("ZIP 只允许 questions.csv 和 images/ 下的 PNG / JPEG")
                if item.file_size > MAX_IMAGE or item.file_size > max(1, item.compress_size) * 200:
                    raise BankError("ZIP 内文件超限或压缩比异常")
                files[item.filename] = archive.read(item)  # verifies CRC, no extraction to arbitrary paths
            if "questions.csv" not in files:
                raise BankError("ZIP 根目录缺少 questions.csv")
            images = {key: value for key, value in files.items() if key != "questions.csv"}
            if len(images) > MAX_ROWS:
                raise BankError("最多导入 1000 张图片")
            for value in images.values():
                decode_image(value)
            reader = csv.DictReader(io.StringIO(files["questions.csv"].decode("utf-8-sig"), newline=""))
            if not reader.fieldnames or len(set(reader.fieldnames)) != len(reader.fieldnames) or not set(COLUMNS) <= set(reader.fieldnames):
                raise BankError("CSV 列不完整或重复")
            rows = []
            seen_ids = set()
            for number, item in enumerate(reader, 2):
                if len(rows) >= MAX_ROWS:
                    raise BankError("最多导入 1000 道题")
                if None in item or any(value is None for value in item.values()):
                    raise BankError(f"CSV 第 {number} 行列数不正确")
                values = metadata(item)
                image = item["image"]
                if image not in images:
                    raise BankError(f"CSV 第 {number} 行图片不存在")
                identifier = str(UUID(item["id"])) if item["id"] else str(uuid4())
                if identifier in seen_ids:
                    raise BankError("CSV 存在重复题目 ID")
                seen_ids.add(identifier)
                rows.append({**values, "question_id": identifier, "image": image})
            if not rows:
                raise BankError("题包中没有题目")
            return rows, images
    except (zipfile.BadZipFile, UnicodeError, csv.Error, ValueError, RuntimeError, NotImplementedError, EOFError) as exc:
        if isinstance(exc, BankError):
            raise
        raise BankError("题包格式、编码或题目 ID 不正确") from exc


def preview_import(db, data: bytes, *, user_id: int, session_id: int) -> tuple[str, dict]:
    rows, _ = read_archive(data)
    token = secrets.token_urlsafe(32)
    digest = hashlib.sha256(token.encode()).hexdigest()
    now = now_shanghai()
    # Expired upload bytes are removed on subsequent previews, not by an unbounded background scanner.
    expired = db.scalars(select(GuessImageImport).where(GuessImageImport.expires_at < now).limit(100)).all()
    for item in expired:
        (root() / "imports" / f"{item.token_hash}.zip").unlink(missing_ok=True)
        db.delete(item)
    existing = set(db.scalars(select(GuessImageQuestion.question_id)).all())
    report = {"total": len(rows), "skip": sum(row["question_id"] in existing for row in rows),
              "samples": [{"answer": row["answer"], "category": row["category"], "status": row["status"]} for row in rows[:20]]}
    _atomic(root() / "imports" / f"{digest}.zip", data)
    db.add(GuessImageImport(tenant_id=require_tenant_id(), token_hash=digest, user_id=user_id, session_id=session_id,
                           expires_at=now + timedelta(minutes=10), status="preview", report_json=json.dumps(report, ensure_ascii=False)))
    db.flush()
    return token, report


def commit_import(db, token: str, *, user_id: int, session_id: int) -> dict:
    digest = hashlib.sha256(token.encode()).hexdigest()
    item = db.scalar(select(GuessImageImport).where(GuessImageImport.token_hash == digest,
                     GuessImageImport.user_id == user_id, GuessImageImport.session_id == session_id))
    if not item or ensure_shanghai_datetime(item.expires_at) <= now_shanghai():
        raise BankError("导入预览不存在、已过期或不属于当前会话")
    # UPDATE serializes duplicate confirms on SQLite as well as MariaDB.
    db.execute(update(GuessImageImport).where(GuessImageImport.token_hash == digest).values(updated_at=now_shanghai()))
    item = db.scalar(select(GuessImageImport).where(GuessImageImport.token_hash == digest).execution_options(populate_existing=True))
    if not item or item.user_id != user_id or item.session_id != session_id or ensure_shanghai_datetime(item.expires_at) <= now_shanghai():
        raise BankError("导入预览不存在、已过期或不属于当前会话")
    if item.status == "committed":
        return json.loads(item.report_json)
    rows, images = read_archive(resolve_owned_path(root(), f"imports/{digest}.zip").read_bytes())
    # A no-op write serializes imports against other confirms in this tenant.
    from app.models.saas import Tenant
    db.execute(update(Tenant).where(Tenant.id == require_tenant_id()).values(updated_at=Tenant.updated_at))
    existing = set(db.scalars(select(GuessImageQuestion.question_id).with_for_update()).all())
    imported = skipped = 0
    for row in rows:
        if row["question_id"] in existing:
            skipped += 1
            continue
        values = dict(row)
        image = values.pop("image")
        db.add(GuessImageQuestion(tenant_id=require_tenant_id(), asset_id=save_asset(images[image]), **values))
        imported += 1
    item.status = "committed"
    report = {"imported": imported, "skipped": skipped, "total": len(rows)}
    item.report_json = json.dumps(report)
    db.flush()
    # Keep preview bytes until expiry: transaction rollback must remain retryable.
    return report


def export_archive(db) -> bytes:
    rows = db.scalars(select(GuessImageQuestion).order_by(GuessImageQuestion.id).limit(MAX_ROWS + 1)).all()
    if len(rows) > MAX_ROWS:
        raise BankError("单次导出最多 1000 题，请先拆分题库")
    stream = io.StringIO(newline="")
    writer = csv.DictWriter(stream, fieldnames=COLUMNS)
    writer.writeheader()
    images = {}
    for row in rows:
        data = asset_path(row.asset_id).read_bytes()
        with Image.open(io.BytesIO(data)) as picture:
            extension = "png" if picture.format == "PNG" else "jpg"
        name = f"images/{row.asset_id}.{extension}"
        images[name] = data
        writer.writerow({"id": row.question_id, "image": name, "answer": row.answer, "category": row.category,
                         "tags": "|".join(json.loads(row.tags_json)), "source": row.source, "license": row.license, "status": row.status})
    if sum(map(len, images.values())) + len(stream.getvalue().encode("utf-8-sig")) > MAX_EXPANDED:
        raise BankError("导出题包展开后超限，请拆分题库")
    output = io.BytesIO()
    with zipfile.ZipFile(output, "w", zipfile.ZIP_DEFLATED) as archive:
        archive.writestr("questions.csv", stream.getvalue().encode("utf-8-sig"))
        for name, data in images.items():
            archive.writestr(name, data)
    if len(output.getvalue()) > MAX_ZIP:
        raise BankError("导出题包超过 20 MiB，请拆分题库")
    return output.getvalue()


def resource_audit(db) -> dict:
    references = set(db.scalars(select(GuessImageQuestion.asset_id)).all())
    files = {path.stem for path in (root() / "images").glob("*.image")}
    missing = 0
    for asset_id in references:
        try:
            decode_image(asset_path(asset_id).read_bytes())
            asset_path(asset_id, thumbnail=True)
        except (OSError, ValueError):
            missing += 1
    return {"referenced": len(references), "stored": len(files), "missing": missing, "retained": len(files - references)}
