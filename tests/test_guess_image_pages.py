from __future__ import annotations

import re

import pytest
from sqlalchemy import select

import app.web.guess_image as web
from app.core.settings import settings
from app.core.tenant_context import tenant_scope
from app.core.tenant_db import tenant_session
from app.models.entities import AuditLog, Plugin
from app.models.guess_image import GuessImageQuestion
from app.services import plugin_directory
from app.services import guess_image_bank as bank
from test_guess_image_bank import pack, png
from test_provider_node_pages import _login, provider_page_context  # noqa: F401

BASE = "/plugin-admin/guess_image"


@pytest.fixture()
def pages(provider_page_context, monkeypatch, tmp_path):
    client, ids = provider_page_context
    monkeypatch.setattr(web, "TenantSessionLocal", ids["factory"])
    monkeypatch.setattr(settings, "data_dir", str(tmp_path / "assets"))
    with ids["factory"]() as db:
        plugin_directory.sync_plugins_from_directory(db, force=True)
        db.commit()
    with tenant_scope(ids["beta"]), tenant_session(ids["factory"]) as db:
        other = bank.save_question(db, {"answer": "乙租户专属"}, png("blue"))
        db.commit()
        ids["other_question"] = other.question_id
        ids["other_asset"] = other.asset_id
    return client, ids


def test_generic_second_button_tenant_navigation_and_readonly_permissions(pages):
    client, ids = pages
    _login(client, "readonly")
    html = client.get("/plugins").text
    rows = html.split('<article class="plugin-catalog-row">')[1:]
    guess = next(row.split("</article>")[0] for row in rows if "管理题库" in row.split("</article>")[0])
    assert ">配置</a>" in guess and f'href="{BASE}"' in guess
    weather = next(row.split("</article>")[0] for row in rows if "天气" in row.split("</article>")[0])
    assert "管理题库" not in weather
    response = client.get(BASE)
    assert response.status_code == 200 and "共 0 道题" in response.text
    assert "添加题目" not in response.text
    assert client.get(BASE + "/new").status_code == 403
    assert client.post(BASE + "/new").status_code == 403
    assert client.get(BASE + "/export").status_code == 403
    from app.web.tenant_navigation import build_tenant_navigation
    assert next(item for item in build_tenant_navigation(BASE)["nav_items"] if item["label"] == "插件")["active"]


def test_crud_csrf_media_and_cross_tenant_ids(pages):
    client, ids = pages
    csrf = _login(client, "operator")
    assert client.post(BASE + "/new", data={"answer": "x"}).status_code == 403
    response = client.post(BASE + "/new", data={"csrf_token": csrf, "answer": "苹果", "category": "水果", "status": "enabled", "source": "https://example.org", "license": "CC0"}, files={"image": ("anything.png", png(), "image/png")})
    assert response.status_code == 303
    with tenant_scope(ids["alpha"]), tenant_session(ids["factory"]) as db:
        row = db.scalar(select(GuessImageQuestion))
        identifier, asset = row.question_id, row.asset_id
    assert client.get(BASE + "/media/" + asset).status_code == 200
    assert client.get(BASE + "/media/" + asset + "?original=true").headers["content-type"] == "image/png"
    assert client.get(BASE + "/media/" + ids["other_asset"]).status_code == 404
    assert client.get(BASE + "/questions/" + ids["other_question"]).status_code == 404
    assert client.post(BASE + "/questions/" + ids["other_question"], data={"csrf_token": csrf, "answer": "attack"}).status_code == 404
    response = client.post(BASE + "/questions/" + identifier, data={"csrf_token": csrf, "answer": " Apple ", "status": "disabled", "source": "<script>alert(1)</script>"})
    assert response.status_code == 303
    html = client.get(BASE + "?q=Apple&status=disabled").text
    assert "&lt;script&gt;" in html and "<script>alert(1)</script>" not in html
    assert "共 1 道题" in html
    assert client.post(BASE + "/questions/" + identifier + "/delete", data={"csrf_token": csrf}).status_code == 303
    assert client.get(BASE + "/media/" + asset).status_code == 404
    with tenant_scope(ids["alpha"]), tenant_session(ids["factory"]) as db:
        assert bank.asset_path(asset).exists()  # immutable files survive a game snapshot
        assert {row.action for row in db.scalars(select(AuditLog)).all()} >= {"guess-image.create", "guess-image.update", "guess-image.delete"}


def test_zip_preview_confirmation_export_and_audit(pages):
    client, ids = pages
    csrf = _login(client, "admin")
    response = client.post(BASE + "/imports/preview", data={"csrf_token": csrf}, files={"archive": ("questions.zip", pack(), "application/zip")})
    assert response.status_code == 200 and "确认导入" in response.text
    token = re.search(r'name="token" value="([^"]+)"', response.text).group(1)
    assert "共 0 道题" in client.get(BASE).text
    assert client.post(BASE + "/imports/confirm", data={"csrf_token": csrf, "token": token}).status_code == 303
    assert client.post(BASE + "/imports/confirm", data={"csrf_token": csrf, "token": token}).status_code == 303
    assert "共 1 道题" in client.get(BASE).text
    exported = client.get(BASE + "/export")
    assert exported.status_code == 200 and len(bank.read_archive(exported.content)[0]) == 1
    assert "图片资源检查" in client.get(BASE + "/audit").text


def test_platform_unavailability_is_enforced_on_every_bank_route(pages):
    client, ids = pages
    csrf = _login(client, "admin")
    with ids["factory"]() as db:
        row = db.scalar(select(Plugin).where(Plugin.code == bank.CODE))
        row.platform_status = "disabled"
        db.commit()
    for path in (BASE, BASE + "/new", BASE + "/export", BASE + "/audit", BASE + "/media/" + ids["other_asset"]):
        assert client.get(path).status_code == 404
    assert client.post(BASE + "/new", data={"csrf_token": csrf}).status_code == 404
