import os
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy import select
from sqlalchemy.orm import sessionmaker


PROJECT_ROOT = Path(__file__).resolve().parents[1]
TEST_RUNTIME = PROJECT_ROOT / ".run" / "test-runtime" / "simplified-bot-lifecycle"

# 本文件会直接导入全局数据库引擎和 FastAPI 应用，因此必须在任何 app 模块导入前
# 把运行目录锁定到一次性测试路径，避免契约测试误连开发库或未来的生产数据库。
os.environ["APP_ENV"] = "test"
os.environ["DATA_DIR"] = str(TEST_RUNTIME)
os.environ["DATABASE_URL"] = f"sqlite:///{TEST_RUNTIME / 'test.db'}"
os.environ["LOG_DIR"] = str(TEST_RUNTIME / "logs")
os.environ["AUTH_SEED_DEMO_USERS"] = "false"

from app.core.db import Base, SessionLocal, engine as app_engine
from app.main import app
from app.models.entities import AdminUser, Bot, MessageActivityHourly, PluginActivityHourly
from app.schemas.forms import BotCreate, BotUpdate
from app.services.auth import hash_password
from app.services.crud import create_bot, update_bot
from app.services.ops import build_monitoring_snapshot


DELETED_TABLES = {"bot_groups", "config_templates", "bot_tags", "bot_tag_links"}
DELETED_BOT_FIELDS = {"slug", "command_prefix", "group_id", "config_template_id", "environment"}
DELETED_PATHS = {
    "/groups",
    "/groups/{group_id}",
    "/groups/{group_id}/edit",
    "/templates",
    "/templates/{template_id}",
    "/templates/{template_id}/edit",
    "/bots/{bot_id}/clone",
}


@pytest.fixture()
def db_session():
    """为服务层契约提供完全独立的内存数据库，并在测试结束后主动释放连接。"""

    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(bind=engine)
    session = sessionmaker(autocommit=False, autoflush=False, bind=engine)()
    try:
        yield session
    finally:
        session.close()
        engine.dispose()


@pytest.fixture()
def authenticated_client():
    """重建应用测试库并登录管理员，用真实表单链路验证机器人创建行为。"""

    Base.metadata.drop_all(bind=app_engine)
    Base.metadata.create_all(bind=app_engine)
    with SessionLocal() as db:
        db.add(
            AdminUser(
                username="admin",
                display_name="管理员",
                password_hash=hash_password("admin123"),
                role="admin",
                is_enabled=True,
                auth_version=1,
            )
        )
        db.commit()

    with TestClient(app) as client:
        login = client.post(
            "/login",
            data={"username": "admin", "password": "admin123"},
            follow_redirects=False,
        )
        assert login.status_code == 303
        yield client


def test_removed_bot_metadata_is_absent_from_database_contract() -> None:
    """新 SaaS 数据库不能再生成机器人分组、模板、标签及其关联结构。"""

    assert DELETED_TABLES.isdisjoint(Base.metadata.tables)
    assert DELETED_BOT_FIELDS.isdisjoint(Bot.__table__.columns.keys())


def test_activity_tables_do_not_split_metrics_by_environment() -> None:
    """单一 SaaS 运行域直接按租户统计，不再维护无实际用途的环境维度。"""

    assert "environment" not in MessageActivityHourly.__table__.columns
    assert "environment" not in PluginActivityHourly.__table__.columns


def test_bot_forms_only_expose_business_fields() -> None:
    """创建只填写名称，编辑只允许维护名称和说明。"""

    assert set(BotCreate.model_fields) == {"name"}
    assert set(BotUpdate.model_fields) == {"name", "description"}


def test_removed_business_routes_are_not_registered() -> None:
    """已删除模块不能留下可绕过导航访问的旧 HTTP 接口。"""

    registered_paths = {str(route.path) for route in app.routes if getattr(route, "path", None)}
    assert DELETED_PATHS.isdisjoint(registered_paths)


def test_minimal_bot_create_flow_and_duplicate_error(authenticated_client) -> None:
    """真实 Web 创建流程只接收名称，并对重复名称给出可理解的错误。"""

    created = authenticated_client.post(
        "/bots",
        data={"name": " 客服机器人 "},
        follow_redirects=False,
    )

    assert created.status_code == 303
    assert created.headers["location"].startswith("/bots/")
    assert "notice=" in created.headers["location"]
    with SessionLocal() as db:
        bot = db.scalar(select(Bot).where(Bot.name == "客服机器人"))
        assert bot is not None
        assert bot.is_enabled is True
        assert bot.status == "running"
        bot_id = bot.id

    # 创建成功后继续走真实页面渲染，防止精简字段后列表、详情或总览仍访问旧模板上下文。
    list_page = authenticated_client.get("/bots")
    detail_page = authenticated_client.get(f"/bots/{bot_id}")
    dashboard_page = authenticated_client.get("/")
    assert list_page.status_code == 200
    assert detail_page.status_code == 200
    assert dashboard_page.status_code == 200
    assert "客服机器人" in list_page.text
    assert "客服机器人" in detail_page.text
    assert "客服机器人" in dashboard_page.text

    duplicate = authenticated_client.post(
        "/bots",
        data={"name": "客服机器人"},
        follow_redirects=False,
    )

    assert duplicate.status_code == 303
    assert duplicate.headers["location"].startswith("/bots?notice=")
    assert "tone=danger" in duplicate.headers["location"]


def test_robot_templates_only_expose_the_minimal_workflow() -> None:
    """模板源码不得继续渲染已删除字段或机器人复制入口。"""

    bots_source = (PROJECT_ROOT / "app/templates/bots.html").read_text(encoding="utf-8")
    detail_source = (PROJECT_ROOT / "app/templates/bot_detail.html").read_text(encoding="utf-8")
    dashboard_source = (PROJECT_ROOT / "app/templates/dashboard.html").read_text(encoding="utf-8")
    removed_form_names = (
        "group_id",
        "config_template_id",
        "tags",
        "slug",
        "environment",
        "command_prefix",
    )

    assert 'name="name"' in bots_source
    for field_name in removed_form_names:
        assert f'name="{field_name}"' not in bots_source
        assert f'name="{field_name}"' not in detail_source
    assert "/clone" not in detail_source
    assert "复制机器人" not in detail_source
    assert "分组 / 环境" not in dashboard_source
    assert "模板 / 标签" not in dashboard_source

    for filename in ("groups.html", "group_detail.html", "templates.html", "template_detail.html"):
        assert not (PROJECT_ROOT / "app/templates" / filename).exists()


def test_create_and_update_bot_use_minimal_contract(db_session) -> None:
    """服务层必须为创建动作补齐固定状态，编辑动作只维护业务资料。"""

    bot = create_bot(db_session, BotCreate(name=" 客服机器人 "))

    assert bot.name == "客服机器人"
    assert bot.description is None
    assert bot.is_enabled is True
    assert bot.status == "running"

    updated = update_bot(
        db_session,
        bot,
        BotUpdate(name=" 售后机器人 ", description=" 处理售后咨询 "),
    )

    assert updated.name == "售后机器人"
    assert updated.description == "处理售后咨询"
    assert updated.is_enabled is True
    assert updated.status == "running"


def test_plugin_dispatch_uses_fixed_command_prefix() -> None:
    """企业微信 SaaS 统一使用斜杠命令，不再从机器人读取可配置前缀。"""

    source = (PROJECT_ROOT / "app/services/plugin_directory.py").read_text(encoding="utf-8")
    assert 'getattr(channel.bot, "command_prefix"' not in source
    assert 'command_prefix="/"' in source


def test_monitoring_snapshot_no_longer_requires_environment(db_session) -> None:
    """空数据库也应直接生成全租户监控快照，不再依赖环境解析缓存。"""

    snapshot = build_monitoring_snapshot(db_session)

    assert "environment" not in snapshot
    assert snapshot["stats"] == {
        "health_checks": 0,
        "open_alerts": 0,
        "audit_logs": 0,
        "messages_24h": 0,
        "webhook_failures_24h": 0,
    }
