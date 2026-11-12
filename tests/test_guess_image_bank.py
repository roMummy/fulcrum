from __future__ import annotations

import csv
import io
import json
import stat
import zipfile
from datetime import timedelta
from uuid import uuid4

import pytest
from PIL import Image
from sqlalchemy import create_engine, select
from sqlalchemy.orm import sessionmaker

from app.core.db import Base
from app.core.settings import settings
from app.core.tenant_context import tenant_scope
from app.core.tenant_db import tenant_session
from app.core.time import now_shanghai
from app.models.guess_image import GuessImageImport, GuessImageQuestion
from app.models.saas import Tenant
from app.services import guess_image_bank as bank


def png(color="green", size=(32, 24)):
    data = io.BytesIO()
    Image.new("RGB", size, color).save(data, "PNG")
    return data.getvalue()


def pack(rows=None, **extra):
    stream = io.StringIO(newline="")
    writer = csv.DictWriter(stream, fieldnames=bank.COLUMNS)
    writer.writeheader()
    for row in rows if rows is not None else [{}]:
        writer.writerow({"id": str(uuid4()), "image": "images/a.png", "answer": "苹果", "category": "水果",
            "tags": "红色|水果", "source": "https://example.org/test", "license": "CC0", "status": "enabled", **row})
    output = io.BytesIO()
    with zipfile.ZipFile(output, "w", zipfile.ZIP_DEFLATED) as z:
        for name, data in {"questions.csv": stream.getvalue().encode("utf-8-sig"), "images/a.png": png(), **extra}.items():
            z.writestr(name, data)
    return output.getvalue()


@pytest.fixture()
def env(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "data_dir", str(tmp_path / "data"))
    engine = create_engine(f"sqlite:///{tmp_path / 'bank.db'}")
    Base.metadata.create_all(engine)
    factory = sessionmaker(engine, expire_on_commit=False)
    with factory() as db:
        db.add_all([Tenant(id=1, code="one", name="One"), Tenant(id=2, code="two", name="Two")])
        db.commit()
    with tenant_scope(1), tenant_session(factory) as db:
        yield db, factory


def test_image_decode_caps_and_immutable_tenant_paths(env):
    db, factory = env
    asset = bank.save_asset(png())
    assert bank.save_asset(png()) == asset
    assert bank.asset_path(asset).read_bytes() == png()
    assert bank.asset_path(asset, thumbnail=True).is_file()
    for bad in (b"not image", b"x" * (bank.MAX_IMAGE + 1), png(size=(2001, 2000))):
        with pytest.raises(bank.BankError):
            bank.save_asset(bad)
    for bad in ("../secret", "A" * 64, "", "/tmp/image"):
        with pytest.raises(ValueError):
            bank.asset_path(bad)
    with tenant_scope(2):
        with pytest.raises(ValueError):
            bank.asset_path(asset)


def test_metadata_and_exact_answer_are_preserved(env):
    db, _ = env
    row = bank.save_question(db, {"answer": " Apple ", "status": "draft"}, png())
    assert row.answer == " Apple "
    for values in ({"answer": " "}, {"answer": "x" * 81}, {"answer": "x", "status": "enabled"},
                   {"answer": "x", "tags": "|".join(str(i) for i in range(13))}, {"answer": "x\n"}):
        with pytest.raises(bank.BankError):
            bank.metadata(values)
    assert not {"played_count", "correct_count", "weight"} & set(row.__table__.columns.keys())


def test_import_preview_confirm_idempotent_export_roundtrip_and_audit(env):
    db, _ = env
    data = pack()
    token, report = bank.preview_import(db, data, user_id=1, session_id=7)
    assert report["total"] == 1
    assert not db.scalar(select(GuessImageQuestion))
    assert bank.commit_import(db, token, user_id=1, session_id=7) == {"imported": 1, "skipped": 0, "total": 1}
    assert bank.commit_import(db, token, user_id=1, session_id=7)["imported"] == 1
    token2, _ = bank.preview_import(db, data, user_id=1, session_id=7)
    assert bank.commit_import(db, token2, user_id=1, session_id=7)["skipped"] == 1
    exported = bank.export_archive(db)
    rows, images = bank.read_archive(exported)
    assert rows[0]["answer"] == "苹果" and len(images) == 1
    assert bank.resource_audit(db) == {"referenced": 1, "stored": 1, "missing": 0, "retained": 0}
    row = db.scalar(select(GuessImageQuestion))
    original = bank.asset_path(row.asset_id)
    bank.save_question(db, {"answer": "香蕉"}, png("yellow"), question=row)
    assert original.is_file() and bank.resource_audit(db)["retained"] == 1
    db.delete(row)
    db.flush()
    assert bank.resource_audit(db)["retained"] == 2


def test_preview_requires_original_tenant_user_session_and_ttl(env):
    db, factory = env
    token, _ = bank.preview_import(db, pack(), user_id=1, session_id=7)
    db.commit()
    for user, session in ((2, 7), (1, 8)):
        with pytest.raises(bank.BankError):
            bank.commit_import(db, token, user_id=user, session_id=session)
    with tenant_scope(2), tenant_session(factory) as other:
        with pytest.raises(bank.BankError):
            bank.commit_import(other, token, user_id=1, session_id=7)
    preview = db.scalar(select(GuessImageImport))
    preview.expires_at = now_shanghai() - timedelta(seconds=1)
    db.flush()
    with pytest.raises(bank.BankError):
        bank.commit_import(db, token, user_id=1, session_id=7)


@pytest.mark.parametrize("mutate", [
    lambda: pack(**{"../escape.png": png()}),
    lambda: pack(**{"images/../escape.png": png()}),
    lambda: pack(**{"images/escape.png": png()}).replace(b"images/escape.png", b"images\\escape.png"),
    lambda: pack(**{"images/A.PNG": png(), "images/a.PNG": png()}),
    lambda: pack([{"image": "images/missing.png"}]),
    lambda: pack([{"source": ""}]),
    lambda: pack([{"id": "invalid"}]),
    lambda: pack([{"id": "00000000-0000-0000-0000-000000000001"}] * 2),
    lambda: pack(**{"images/a.png": b"fake"}),
    lambda: b"not a zip",
])
def test_hostile_archives_are_rejected(mutate):
    with pytest.raises(bank.BankError):
        bank.read_archive(mutate())


def test_zip_symlink_and_expanded_limit_are_rejected(monkeypatch):
    data = io.BytesIO()
    with zipfile.ZipFile(data, "w") as z:
        item = zipfile.ZipInfo("images/link.png")
        item.external_attr = (stat.S_IFLNK | 0o777) << 16
        z.writestr(item, "../../escape")
    with pytest.raises(bank.BankError):
        bank.read_archive(data.getvalue())
    monkeypatch.setattr(bank, "MAX_EXPANDED", 20)
    with pytest.raises(bank.BankError):
        bank.read_archive(pack())


def test_questions_are_tenant_isolated(env):
    db, factory = env
    bank.save_question(db, {"answer": "苹果"}, png())
    db.commit()
    with tenant_scope(2), tenant_session(factory) as other:
        assert not other.scalars(select(GuessImageQuestion)).all()
        assert bank.resource_audit(other)["referenced"] == 0
