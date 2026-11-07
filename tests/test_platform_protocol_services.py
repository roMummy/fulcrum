from __future__ import annotations

import pytest
from fastapi import FastAPI, Request
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session, sessionmaker

import app.web.platform_operations as platform_operations_web
from app.core.db import Base
from app.models.entities import Bot, Channel
from app.models.saas import PlatformAuditLog, PlatformProtocolService, PlatformUser, Tenant
from app.services.channel_catalog import get_channel_type_definition
from app.services.platform_protocol_services import (
    PlatformProtocolServiceError,
    list_platform_protocol_services,
    require_enabled_platform_protocol_service,
    save_platform_protocol_service,
)
from app.services.saas_auth import create_platform_session, hash_password, load_platform_principal
from app.web.auth_common import PLATFORM_CSRF_COOKIE, PLATFORM_SESSION_COOKIE


@pytest.fixture()
def db(tmp_path):
    """平台协议服务测试使用独立数据库，允许同时准备多个租户渠道。"""

    engine = create_engine(f"sqlite:///{tmp_path / 'platform-protocol-services.db'}")
    Base.metadata.create_all(bind=engine)
    with Session(engine) as session:
        yield session
    Base.metadata.drop_all(bind=engine)
    engine.dispose()


def _seed_channel(db: Session, *, tenant_code: str, channel_type: str, api_base: str) -> Channel:
    """创建最小租户、机器人和渠道，用于验证平台地址跨租户同步。"""

    tenant = Tenant(code=tenant_code, name=f"{tenant_code} 企业", status="active")
    db.add(tenant)
    db.flush()
    bot = Bot(tenant_id=tenant.id, name=f"{tenant_code} 机器人", status="stopped", is_enabled=False)
    db.add(bot)
    db.flush()
    channel = Channel(
        tenant_id=tenant.id,
        bot_id=bot.id,
        name=f"{tenant_code} 渠道",
        channel_type=channel_type,
        api_base=api_base,
        status="draft",
        is_enabled=False,
    )
    db.add(channel)
    db.commit()
    db.refresh(channel)
    return channel


def test_only_ipad_uses_platform_managed_endpoint() -> None:
    """首期只有 iPad 服务地址由平台托管，账号级 Telegram Token 不得被错误收归平台。"""

    assert get_channel_type_definition("wechat_ipad").platform_managed_endpoint is True
    assert get_channel_type_definition("wechat_winauto").platform_managed_endpoint is False
    assert get_channel_type_definition("telegram").platform_managed_endpoint is False


def test_platform_service_save_normalizes_and_synchronizes_all_ipad_channels(db: Session) -> None:
    """平台保存地址后必须同步全部租户的 iPad 渠道，同时保持其他渠道不变。"""

    alpha = _seed_channel(
        db,
        tenant_code="alpha",
        channel_type="wechat_ipad",
        api_base="http://old-alpha:9064",
    )
    beta = _seed_channel(
        db,
        tenant_code="beta",
        channel_type="wechat_ipad",
        api_base="http://old-beta:9064",
    )
    telegram = _seed_channel(
        db,
        tenant_code="gamma",
        channel_type="telegram",
        api_base="https://api.telegram.org",
    )

    service = save_platform_protocol_service(
        db,
        channel_type="wechat_ipad",
        api_base="http://ipad.internal:9064/",
        is_enabled=True,
        actor_user_id=None,
    )

    db.refresh(alpha)
    db.refresh(beta)
    db.refresh(telegram)
    audit = db.scalar(
        select(PlatformAuditLog).where(PlatformAuditLog.action == "protocol_service.save")
    )
    assert service.api_base == "http://ipad.internal:9064"
    assert service.is_enabled is True
    assert alpha.api_base == service.api_base
    assert beta.api_base == service.api_base
    assert telegram.api_base == "https://api.telegram.org"
    assert "synchronized_channels=2" in (audit.detail or "")
    assert service.api_base not in (audit.detail or "")

    rows = list_platform_protocol_services(db)
    assert rows == [
        {
            "channel_type": "wechat_ipad",
            "label": "微信 iPad 协议",
            "summary": "通过微信协议服务接入个人微信，支持登录、消息、联系人和群聊。",
            "api_base": "http://ipad.internal:9064",
            "is_enabled": True,
            "is_configured": True,
            "channel_count": 2,
        }
    ]


def test_platform_service_requires_valid_enabled_managed_configuration(db: Session) -> None:
    """未配置、停用、非法 URL 和非托管渠道都必须返回稳定业务错误。"""

    with pytest.raises(PlatformProtocolServiceError, match="platform_protocol_service_missing"):
        require_enabled_platform_protocol_service(db, "wechat_ipad")

    for invalid_url in ("", "127.0.0.1:9064", "ftp://ipad.internal", "http://user:pass@ipad.internal"):
        with pytest.raises(PlatformProtocolServiceError, match="platform_protocol_service_url_invalid"):
            save_platform_protocol_service(
                db,
                channel_type="wechat_ipad",
                api_base=invalid_url,
                is_enabled=True,
                actor_user_id=None,
            )

    disabled = save_platform_protocol_service(
        db,
        channel_type="wechat_ipad",
        api_base="http://ipad.internal:9064",
        is_enabled=False,
        actor_user_id=None,
    )
    assert db.scalar(select(PlatformProtocolService).where(PlatformProtocolService.id == disabled.id)) is disabled
    with pytest.raises(PlatformProtocolServiceError, match="platform_protocol_service_disabled"):
        require_enabled_platform_protocol_service(db, "wechat_ipad")

    with pytest.raises(PlatformProtocolServiceError, match="platform_protocol_service_type_not_managed"):
        save_platform_protocol_service(
            db,
            channel_type="telegram",
            api_base="https://api.telegram.org",
            is_enabled=True,
            actor_user_id=None,
        )


def test_platform_protocol_service_page_saves_with_platform_auth_and_csrf(tmp_path, monkeypatch) -> None:
    """协议服务页面只能由平台会话访问，并使用平台 CSRF 保存统一地址。"""

    engine = create_engine(f"sqlite:///{tmp_path / 'platform-protocol-page.db'}")
    factory = sessionmaker(bind=engine, autoflush=False, autocommit=False)
    Base.metadata.create_all(bind=engine)
    with factory() as db:
        platform_user = PlatformUser(
            username="protocol-admin",
            display_name="协议管理员",
            password_hash=hash_password("Protocol-admin-123"),
            role="platform_admin",
            is_enabled=True,
            auth_version=1,
            must_change_password=False,
        )
        db.add(platform_user)
        db.commit()
        db.refresh(platform_user)
        session = create_platform_session(db, platform_user)

    monkeypatch.setattr(platform_operations_web, "SessionLocal", factory)
    test_app = FastAPI()

    @test_app.middleware("http")
    async def restore_platform_principal(request: Request, call_next):
        # 测试只恢复平台会话，不引入主应用 Worker、安装流程或租户 Cookie。
        with factory() as db:
            request.state.platform_principal = load_platform_principal(
                db,
                request.cookies.get(PLATFORM_SESSION_COOKIE),
            )
        return await call_next(request)

    test_app.include_router(platform_operations_web.router)
    with TestClient(test_app) as client:
        assert client.get("/platform/protocol-services").status_code == 401
        client.cookies.set(PLATFORM_SESSION_COOKIE, session.raw_token)
        client.cookies.set(PLATFORM_CSRF_COOKIE, session.raw_csrf_token)

        page = client.get("/platform/protocol-services")
        assert page.status_code == 200
        assert "微信 iPad 协议" in page.text
        assert 'name="api_base"' in page.text
        assert "已同步账号" in page.text
        primary_navigation = page.text.split('<nav class="app-primary-nav"', 1)[1].split("</nav>", 1)[0]
        operations_navigation = page.text.split('<nav class="section-tabs"', 1)[1].split("</nav>", 1)[0]
        assert ">协议服务</a>" not in primary_navigation
        assert 'href="/platform/operations" class="nav-link active"' in primary_navigation
        for label in ("运行监控", "协议服务", "全局日志"):
            assert label in operations_navigation
        assert 'class="section-tab active" href="/platform/protocol-services"' in operations_navigation

        rejected = client.post(
            "/platform/protocol-services/wechat_ipad",
            data={
                "csrf_token": "forged",
                "api_base": "http://forged.internal:9064",
                "is_enabled": "true",
            },
        )
        assert rejected.status_code == 403

        saved = client.post(
            "/platform/protocol-services/wechat_ipad",
            data={
                "csrf_token": session.raw_csrf_token,
                "api_base": "http://ipad.internal:9064/",
                "is_enabled": "true",
            },
            follow_redirects=False,
        )
        assert saved.status_code == 303
        assert saved.headers["location"].startswith("/platform/protocol-services?notice=")

    with factory() as db:
        service = db.scalar(
            select(PlatformProtocolService).where(
                PlatformProtocolService.channel_type == "wechat_ipad"
            )
        )
        assert service.api_base == "http://ipad.internal:9064"
        assert service.is_enabled is True
    Base.metadata.drop_all(bind=engine)
    engine.dispose()
