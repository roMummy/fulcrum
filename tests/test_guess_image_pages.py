from __future__ import annotations

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
    rows = html.split('<article class="plugin-card')[1:]
    guess = next(row.split("</article>")[0] for row in rows if "管理题库" in row.split("</article>")[0])
    # 配置与"管理题库"都是只有图标的按钮,文字在 aria-label 里
    assert 'aria-label="配置 ' in guess and f'href="{BASE}"' in guess
    weather = next(row.split("</article>")[0] for row in rows if "天气" in row.split("</article>")[0])
    assert "管理题库" not in weather
    response = client.get(BASE)
    assert response.status_code == 200 and "共 0 道题" in response.text
    # 空题库可以说明“管理员添加题目后显示”，只读权限应核对实际操作入口而非说明文字。
    assert 'aria-label="添加题目"' not in response.text and "data-gi-create" not in response.text
    # 新增只存在于题库弹窗，旧独立 GET 页面已删除；写接口继续逐项执行服务端权限检查。
    assert client.get(BASE + "/new").status_code == 405
    assert 'id="gi-question-dialog"' not in response.text
    assert client.post(BASE + "/new").status_code == 403
    assert client.get(BASE + "/export").status_code == 403
    assert client.get(BASE + "/audit").status_code == 403
    assert client.get(BASE + "/questions/" + ids["other_question"]).status_code == 403
    assert client.post(BASE + "/imports/preview").status_code == 403
    assert client.post(BASE + "/imports/confirm").status_code == 403
    from app.web.tenant_navigation import build_tenant_navigation
    assert next(item for item in build_tenant_navigation(BASE)["nav_items"] if item["label"] == "插件")["active"]


def test_crud_csrf_media_and_cross_tenant_ids(pages):
    """弹窗使用同一组 JSON 接口，权限、原文答案、媒体隔离与审计均保持有效。"""
    client, ids = pages
    csrf = _login(client, "operator")
    assert client.post(BASE + "/new", data={"answer": "x"}).status_code == 403
    response = client.post(BASE + "/new", data={"csrf_token": csrf, "answer": "苹果", "category": "水果", "status": "enabled", "source": "https://example.org", "license": "CC0"}, files={"image": ("anything.png", png(), "image/png")})
    assert response.status_code == 200 and response.json()["notice"] == "题目已保存"
    with tenant_scope(ids["alpha"]), tenant_session(ids["factory"]) as db:
        row = db.scalar(select(GuessImageQuestion))
        identifier, asset = row.question_id, row.asset_id
    detail = client.get(BASE + "/questions/" + identifier)
    assert detail.status_code == 200 and detail.json()["answer"] == "苹果"
    assert detail.json()["image_url"] == BASE + "/media/" + asset
    assert detail.headers["cache-control"] == "private, no-store"
    assert client.get(BASE + "/media/" + asset).status_code == 200
    assert client.get(BASE + "/media/" + asset + "?original=true").headers["content-type"] == "image/png"
    assert client.get(BASE + "/media/" + ids["other_asset"]).status_code == 404
    assert client.get(BASE + "/questions/" + ids["other_question"]).status_code == 404
    assert client.post(BASE + "/questions/" + ids["other_question"], data={"csrf_token": csrf, "answer": "attack"}).status_code == 404
    response = client.post(BASE + "/questions/" + identifier, data={"csrf_token": csrf, "answer": " Apple ", "status": "disabled", "source": "<script>alert(1)</script>"})
    assert response.status_code == 200
    assert client.get(BASE + "/questions/" + identifier).json()["answer"] == " Apple "
    html = client.get(BASE + "?q=Apple&status=disabled").text
    assert "&lt;script&gt;" in html and "<script>alert(1)</script>" not in html
    assert "共 1 道题" in html
    assert client.post(BASE + "/questions/" + identifier + "/delete").status_code == 403
    assert client.post(BASE + "/questions/" + identifier + "/delete", data={"csrf_token": csrf}).status_code == 200
    assert client.get(BASE + "/media/" + asset).status_code == 404
    with tenant_scope(ids["alpha"]), tenant_session(ids["factory"]) as db:
        assert bank.asset_path(asset).exists()  # immutable files survive a game snapshot
        assert {row.action for row in db.scalars(select(AuditLog)).all()} >= {"guess-image.create", "guess-image.update", "guess-image.delete"}


def test_single_bank_page_and_modal_validation_errors(pages):
    """主页面只输出一套编辑表单；保存错误返回 JSON，失败更新不能污染已保存的题目。"""
    client, ids = pages
    csrf = _login(client, "operator")
    html = client.get(BASE).text
    assert html.count("<h1") == 1
    for dialog_id in ("gi-question-dialog", "gi-import-dialog", "gi-audit-dialog"):
        assert f'id="{dialog_id}"' in html and f'data-modal-open="{dialog_id}"' in html
    assert html.count('id="gi-question-form"') == 1
    assert 'class="gi-card"' not in html and 'class="gi-import disclosure"' not in html
    assert 'class="compact-filter-form"' in html
    assert '<script src="/static/js/guess-image.js" defer></script>' in html
    missing_image = client.post(BASE + "/new", data={"csrf_token": csrf, "answer": "苹果"})
    assert missing_image.status_code == 400 and "图片" in missing_image.json()["detail"]
    assert "text/html" not in missing_image.headers["content-type"]
    assert client.post(BASE + "/new", data={"csrf_token": csrf, "answer": "苹果"}, files={"image": ("a.png", png(), "image/png")}).status_code == 200
    with tenant_scope(ids["alpha"]), tenant_session(ids["factory"]) as db:
        identifier = db.scalar(select(GuessImageQuestion)).question_id
    invalid = client.post(BASE + "/questions/" + identifier, data={"csrf_token": csrf, "answer": "改名", "status": "enabled"})
    assert invalid.status_code == 400 and "来源及授权" in invalid.json()["detail"]
    saved = client.get(BASE + "/questions/" + identifier).json()
    assert saved["answer"] == "苹果" and saved["status"] == "draft"
    assert "共 1 道题" in client.get(BASE).text


def test_zip_preview_confirmation_export_and_audit(pages):
    """导入预览/确认与检查都在弹窗消费 JSON，ZIP 导出继续返回真实文件。"""
    client, ids = pages
    csrf = _login(client, "admin")
    invalid = client.post(BASE + "/imports/preview", data={"csrf_token": csrf}, files={"archive": ("bad.zip", b"not a zip", "application/zip")})
    assert invalid.status_code == 400 and invalid.json()["detail"]
    response = client.post(BASE + "/imports/preview", data={"csrf_token": csrf}, files={"archive": ("questions.zip", pack(), "application/zip")})
    assert response.status_code == 200 and response.json()["report"]["total"] == 1
    assert response.json()["report"]["samples"][0]["answer"] == "苹果"
    token = response.json()["token"]
    assert "共 0 道题" in client.get(BASE).text
    assert client.post(BASE + "/imports/confirm", data={"token": token}).status_code == 403
    assert client.post(BASE + "/imports/confirm", data={"csrf_token": csrf, "token": "invalid"}).status_code == 400
    confirmed = client.post(BASE + "/imports/confirm", data={"csrf_token": csrf, "token": token})
    assert confirmed.status_code == 200 and confirmed.json()["report"]["imported"] == 1
    assert client.post(BASE + "/imports/confirm", data={"csrf_token": csrf, "token": token}).json()["report"]["imported"] == 1
    assert "共 1 道题" in client.get(BASE).text
    exported = client.get(BASE + "/export")
    assert exported.status_code == 200 and len(bank.read_archive(exported.content)[0]) == 1
    audit = client.get(BASE + "/audit")
    assert audit.status_code == 200 and audit.json() == {"referenced": 1, "stored": 1, "missing": 0, "retained": 0}
    assert audit.headers["cache-control"] == "private, no-store"


def test_platform_unavailability_is_enforced_on_every_bank_route(pages):
    client, ids = pages
    csrf = _login(client, "admin")
    with ids["factory"]() as db:
        row = db.scalar(select(Plugin).where(Plugin.code == bank.CODE))
        row.platform_status = "disabled"
        db.commit()
    for path in (BASE, BASE + "/questions/" + ids["other_question"], BASE + "/export", BASE + "/audit", BASE + "/media/" + ids["other_asset"]):
        assert client.get(path).status_code == 404
    assert client.post(BASE + "/new", data={"csrf_token": csrf}).status_code == 404
