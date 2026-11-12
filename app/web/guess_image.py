"""Question-bank administration belongs exclusively to the authenticated tenant."""
from __future__ import annotations

import json
from urllib.parse import quote

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import FileResponse, HTMLResponse, RedirectResponse, Response
from fastapi.templating import Jinja2Templates
from sqlalchemy import func, or_, select
from starlette.datastructures import UploadFile

from app.core.db import TenantSessionLocal
from app.core.tenant_db import open_tenant_session
from app.models.entities import Plugin
from app.models.guess_image import GuessImageQuestion
from app.services import guess_image_bank as bank
from app.services.ops import record_audit
from app.services.plugin_governance import blocked_plugin_codes
from app.services.saas_auth import has_tenant_role
from app.web.auth_common import TENANT_CSRF_COOKIE, TENANT_LOGOUT_PATH, TENANT_PASSWORD_PATH, require_tenant_csrf
from app.web.tenant_navigation import build_tenant_navigation

BASE = "/plugin-admin/guess_image"
router = APIRouter(prefix=BASE, tags=["guess-image"])
templates = Jinja2Templates(directory="app/templates")


def _access(request, db, *, edit=False):
    principal = getattr(request.state, "current_user", None)
    if not principal:
        raise HTTPException(401, "Authentication required")
    if edit and not has_tenant_role(principal, "operator"):
        raise HTTPException(403, "Permission denied")
    plugin = db.scalar(select(Plugin).where(Plugin.code == bank.CODE, Plugin.missing_at.is_(None)))
    if not plugin or bank.CODE in blocked_plugin_codes(db, principal.tenant_id):
        raise HTTPException(404, "Plugin not available")
    return principal


def _context(request, **extra):
    principal = request.state.current_user
    return {"request": request, "page_title": "猜图片 · 管理题库", "base": BASE,
        "current_user": principal, "csrf_token": request.cookies.get(TENANT_CSRF_COOKIE, ""),
        "password_action": TENANT_PASSWORD_PATH, "logout_action": TENANT_LOGOUT_PATH,
        "module_links": {}, "can_edit": has_tenant_role(principal, "operator"),
        "notice": request.query_params.get("notice", ""), **build_tenant_navigation(BASE), **extra}


def _question(db, identifier):
    row = db.scalar(select(GuessImageQuestion).where(GuessImageQuestion.question_id == identifier))
    if row is None:
        raise HTTPException(404, "Question not found")
    return row


def _redirect(notice):
    return RedirectResponse(f"{BASE}?notice={quote(notice)}", status_code=303)


def _audit(db, principal, action, target_id=None, detail=None):
    record_audit(db, f"guess-image.{action}", "guess_image_question", target_id, detail, actor=f"tenant-user:{principal.user_id}")


async def _upload(form, name, limit):
    value = form.get(name)
    if not isinstance(value, UploadFile) or not value.filename:
        return None
    data = await value.read(limit + 1)
    if len(data) > limit:
        raise bank.BankError("上传文件超过大小上限")
    return data


@router.get("", response_class=HTMLResponse)
async def bank_page(request: Request, q: str = "", category: str = "", status: str = "", page: int = 1):
    with open_tenant_session(TenantSessionLocal) as db:
        _access(request, db)
        conditions = []
        q, category, status = q[:80], category[:40], status[:16]
        if q:
            escaped = q.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
            conditions.append(or_(GuessImageQuestion.answer.like(f"%{escaped}%", escape="\\"),
                                  GuessImageQuestion.tags_json.like(f"%{escaped}%", escape="\\")))
        if category:
            conditions.append(GuessImageQuestion.category == category)
        if status:
            conditions.append(GuessImageQuestion.status == status)
        total = db.scalar(select(func.count()).select_from(GuessImageQuestion).where(*conditions)) or 0
        pages = max(1, (total + 23) // 24)
        page = max(1, min(page, pages))
        rows = db.scalars(select(GuessImageQuestion).where(*conditions).order_by(GuessImageQuestion.id.desc())
                          .offset((page - 1) * 24).limit(24)).all()
        categories = db.scalars(select(GuessImageQuestion.category).distinct().order_by(GuessImageQuestion.category)).all()
        counts = dict(db.execute(select(GuessImageQuestion.status, func.count()).group_by(GuessImageQuestion.status)).all())
        return templates.TemplateResponse(request=request, name="guess_image_bank.html", context=_context(request,
            rows=rows, categories=categories, counts=counts, total=total, page=page, pages=pages,
            q=q, category=category, status=status, tags=lambda row: " · ".join(json.loads(row.tags_json)),
            page_query=f"q={quote(q)}&category={quote(category)}&status={quote(status)}"))


@router.get("/new", response_class=HTMLResponse)
async def new_page(request: Request):
    with open_tenant_session(TenantSessionLocal) as db:
        _access(request, db, edit=True)
        return templates.TemplateResponse(request=request, name="guess_image_form.html", context=_context(request, question=None, draft={}, error=""))


@router.get("/questions/{identifier}", response_class=HTMLResponse)
async def edit_page(request: Request, identifier: str):
    with open_tenant_session(TenantSessionLocal) as db:
        _access(request, db, edit=True)
        row = _question(db, identifier)
        draft = {key: getattr(row, key) for key in ("answer", "category", "source", "license", "status")}
        draft["tags"] = "|".join(json.loads(row.tags_json))
        return templates.TemplateResponse(request=request, name="guess_image_form.html", context=_context(request, question=row, draft=draft, error=""))


async def _save(request, identifier=None):
    with open_tenant_session(TenantSessionLocal) as db:
        principal = _access(request, db, edit=True)
        async with request.form(max_files=1, max_fields=10) as form:
            require_tenant_csrf(db, request, str(form.get("csrf_token") or ""))
            row = _question(db, identifier) if identifier else None
            draft = {key: str(form.get(key) or "") for key in ("answer", "category", "tags", "source", "license", "status")}
            try:
                data = await _upload(form, "image", bank.MAX_IMAGE)
                row = bank.save_question(db, draft, data, question=row)
                db.flush()
                _audit(db, principal, "update" if identifier else "create", row.id)
            except (bank.BankError, OSError) as exc:
                db.rollback()
                return templates.TemplateResponse(request=request, name="guess_image_form.html", status_code=400,
                    context=_context(request, question=row, draft=draft, error=str(exc)))
        return _redirect("题目已保存")


@router.post("/new")
async def create(request: Request):
    return await _save(request)


@router.post("/questions/{identifier}")
async def update_question(request: Request, identifier: str):
    return await _save(request, identifier)


@router.post("/questions/{identifier}/delete")
async def delete(request: Request, identifier: str):
    with open_tenant_session(TenantSessionLocal) as db:
        principal = _access(request, db, edit=True)
        async with request.form() as form:
            require_tenant_csrf(db, request, str(form.get("csrf_token") or ""))
        row = _question(db, identifier)
        row_id = row.id
        db.delete(row)
        _audit(db, principal, "delete", row_id)
        return _redirect("题目已删除；图片保留供进行中的游戏使用")


@router.get("/media/{asset_id}")
async def media(request: Request, asset_id: str, original: bool = False):
    with open_tenant_session(TenantSessionLocal) as db:
        _access(request, db)
        if not db.scalar(select(GuessImageQuestion.id).where(GuessImageQuestion.asset_id == asset_id).limit(1)):
            raise HTTPException(404, "Image not found")
        try:
            path = bank.asset_path(asset_id, thumbnail=not original)
            if original:
                from PIL import Image
                with Image.open(path) as picture:
                    content_type = "image/png" if picture.format == "PNG" else "image/jpeg"
            else:
                content_type = "image/png"
            return FileResponse(path, media_type=content_type, headers={"Cache-Control": "private, no-store", "X-Content-Type-Options": "nosniff"})
        except (ValueError, OSError):
            raise HTTPException(404, "Image not found")


@router.post("/imports/preview", response_class=HTMLResponse)
async def preview(request: Request):
    with open_tenant_session(TenantSessionLocal) as db:
        principal = _access(request, db, edit=True)
        async with request.form(max_files=1, max_fields=2) as form:
            require_tenant_csrf(db, request, str(form.get("csrf_token") or ""))
            try:
                data = await _upload(form, "archive", bank.MAX_ZIP)
                if not data:
                    raise bank.BankError("请选择 ZIP 题包")
                token, report = bank.preview_import(db, data, user_id=principal.user_id, session_id=principal.session_id)
                _audit(db, principal, "import-preview", detail=f"total={report['total']}")
            except (bank.BankError, OSError) as exc:
                db.rollback()
                return _redirect(str(exc))
        return templates.TemplateResponse(request=request, name="guess_image_import.html", context=_context(request, token=token, report=report))


@router.post("/imports/confirm")
async def confirm(request: Request):
    with open_tenant_session(TenantSessionLocal) as db:
        principal = _access(request, db, edit=True)
        async with request.form() as form:
            require_tenant_csrf(db, request, str(form.get("csrf_token") or ""))
            try:
                report = bank.commit_import(db, str(form.get("token") or ""), user_id=principal.user_id, session_id=principal.session_id)
                _audit(db, principal, "import-confirm", detail=json.dumps(report))
            except (bank.BankError, OSError) as exc:
                db.rollback()
                return _redirect(str(exc))
        return _redirect(f"导入完成：新增 {report['imported']} 题，跳过重复 ID {report['skipped']} 题")


@router.get("/export")
async def export(request: Request):
    with open_tenant_session(TenantSessionLocal) as db:
        principal = _access(request, db, edit=True)
        try:
            data = bank.export_archive(db)
        except (bank.BankError, OSError) as exc:
            return _redirect(str(exc))
        _audit(db, principal, "export")
        return Response(data, media_type="application/zip", headers={"Content-Disposition": 'attachment; filename="guess-image.zip"', "Cache-Control": "private, no-store"})


@router.get("/audit", response_class=HTMLResponse)
async def audit(request: Request):
    with open_tenant_session(TenantSessionLocal) as db:
        principal = _access(request, db, edit=True)
        result = bank.resource_audit(db)
        _audit(db, principal, "resource-audit", detail=json.dumps(result))
        return templates.TemplateResponse(request=request, name="guess_image_audit.html", context=_context(request, audit=result))
