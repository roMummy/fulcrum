from __future__ import annotations

from datetime import timedelta

import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session

from app.core.db import Base
from app.core.tenant_context import tenant_scope
from app.core.time import now_shanghai
from app.models.entities import Bot, Channel
from app.models.saas import LoginProxySession, PlatformProxy, Tenant
from app.services.login_proxy_pool import (
    LoginProxyPoolError,
    create_login_proxy_session,
    finish_login_proxy_session,
    import_platform_proxies_csv,
    list_platform_proxies,
    mark_login_proxy_qrcode_ready,
    resolve_login_proxy_credentials,
    rotate_login_proxy_before_qrcode,
    save_platform_proxy,
)
from app.services.security import decrypt_secret


@pytest.fixture()
def db(tmp_path):
    """代理池测试使用独立数据库，避免租约和唯一端点在用例之间相互污染。"""

    engine = create_engine(f"sqlite:///{tmp_path / 'login-proxy-pool.db'}")
    Base.metadata.create_all(bind=engine)
    with Session(engine) as session:
        yield session
    Base.metadata.drop_all(bind=engine)
    engine.dispose()


def _seed_channel(db: Session, *, code: str = "proxy-tenant") -> tuple[Tenant, Channel]:
    """创建最小租户、机器人和微信 iPad 渠道，供登录会话测试复用。"""

    tenant = Tenant(code=code, name=f"{code} 企业", status="active")
    db.add(tenant)
    db.flush()
    bot = Bot(tenant_id=tenant.id, name=f"{code} 机器人", status="stopped", is_enabled=False)
    db.add(bot)
    db.flush()
    channel = Channel(
        tenant_id=tenant.id,
        bot_id=bot.id,
        name=f"{code} 微信",
        channel_type="wechat_ipad",
        api_base="http://127.0.0.1:9064",
        status="draft",
        is_enabled=False,
    )
    db.add(channel)
    db.commit()
    db.refresh(channel)
    return tenant, channel


def _add_proxy(
    db: Session,
    *,
    name: str,
    host: str,
    province: str = "浙江省",
    city: str = "杭州市",
) -> PlatformProxy:
    """通过正式服务新增代理，确保测试覆盖密码加密和字段规范化。"""

    return save_platform_proxy(
        db,
        proxy_id=None,
        values={
            "name": name,
            "province": province,
            "city": city,
            "host": host,
            "port": 1080,
            "username": "proxy-user",
            "password": "proxy-password",
        },
    )


def test_platform_proxy_save_encrypts_password_and_list_masks_host(db: Session) -> None:
    """平台可以解密调用所需凭证，但列表不得返回完整地址或密码原文。"""

    proxy = _add_proxy(db, name="杭州一号", host="192.168.10.25")
    original_cipher = proxy.password_encrypted
    assert original_cipher != "proxy-password"
    assert decrypt_secret(original_cipher) == "proxy-password"

    updated = save_platform_proxy(
        db,
        proxy_id=proxy.id,
        values={
            "name": "杭州一号更新",
            "province": "浙江省",
            "city": "杭州市",
            "host": "192.168.10.25",
            "port": 1080,
            "username": "proxy-user",
            "password": "",
        },
    )
    rows = list_platform_proxies(db)

    assert updated.password_encrypted == original_cipher
    assert rows[0]["masked_host"] == "192.168.*.*"
    assert "password" not in rows[0]
    assert "proxy-password" not in str(rows)


def test_csv_import_skips_duplicate_and_never_returns_credentials(db: Session) -> None:
    """CSV 重复端点只跳过，导入结果不能回传整行敏感数据。"""

    content = (
        "名称,省,市,主机,端口,用户名,密码\n"
        "杭州一号,浙江省,杭州市,10.0.0.1,1080,user-a,secret-a\n"
        "杭州重复,浙江省,杭州市,10.0.0.1,1080,user-a,secret-b\n"
        "上海一号,上海市,上海市,10.0.0.2,1081,user-b,secret-c\n"
    ).encode("utf-8")

    result = import_platform_proxies_csv(db, content=content)

    assert result == {"success": 2, "skipped": 1, "failed": 0, "errors": []}
    assert db.scalar(select(PlatformProxy).where(PlatformProxy.host == "10.0.0.1")) is not None
    assert "secret-a" not in str(result)


def test_csv_invalid_row_does_not_rollback_previous_success(db: Session) -> None:
    """CSV 后续行校验失败时，前面已经成功的代理仍应保留。"""

    content = (
        "名称,省,市,主机,端口,用户名,密码\n"
        "杭州有效,浙江省,杭州市,10.0.0.8,1080,user-a,secret-a\n"
        "杭州无效,浙江省,杭州市,10.0.0.9,70000,user-b,secret-b\n"
    ).encode("utf-8")

    result = import_platform_proxies_csv(db, content=content)

    assert result["success"] == 1
    assert result["failed"] == 1
    assert db.scalar(select(PlatformProxy).where(PlatformProxy.host == "10.0.0.8")) is not None


def test_pool_proxy_is_exclusive_until_session_finishes(db: Session) -> None:
    """一个平台代理同一时间只能分配给一个登录会话，终态后才允许再次使用。"""

    first_tenant, first_channel = _seed_channel(db, code="first-proxy")
    second_tenant, second_channel = _seed_channel(db, code="second-proxy")
    proxy = _add_proxy(db, name="杭州独占", host="10.0.0.10")

    with tenant_scope(first_tenant.id):
        first = create_login_proxy_session(
            db,
            channel=first_channel,
            mode="platform_pool",
            values={"province": "浙江省", "city": "杭州市"},
        )
    with tenant_scope(second_tenant.id):
        with pytest.raises(LoginProxyPoolError, match="proxy_region_unavailable"):
            create_login_proxy_session(
                db,
                channel=second_channel,
                mode="platform_pool",
                values={"province": "浙江省", "city": "杭州市"},
            )
    assert db.scalar(select(LoginProxySession).where(LoginProxySession.channel_id == second_channel.id)) is None

    with tenant_scope(first_tenant.id):
        finish_login_proxy_session(db, first, state="completed", summary="登录完成")
    with tenant_scope(second_tenant.id):
        second = create_login_proxy_session(
            db,
            channel=second_channel,
            mode="platform_pool",
            values={"province": "浙江省", "city": "杭州市"},
        )

    assert first.platform_proxy_id == proxy.id
    assert second.platform_proxy_id == proxy.id


def test_custom_proxy_credentials_are_cleared_when_session_finishes(db: Session) -> None:
    """自有代理只在当前扫码会话存放密文，成功或失败后都必须主动清空。"""

    tenant, channel = _seed_channel(db, code="custom-proxy")
    with tenant_scope(tenant.id):
        session = create_login_proxy_session(
            db,
            channel=channel,
            mode="custom",
            values={
                "host": "127.0.0.1",
                "port": "1080",
                "username": "custom-user",
                "password": "custom-password",
            },
        )
        credentials = resolve_login_proxy_credentials(db, session)
        finish_login_proxy_session(db, session, state="completed", summary="登录完成")

    db.refresh(session)
    assert credentials == {
        "host": "127.0.0.1",
        "port": "1080",
        "username": "custom-user",
        "password": "custom-password",
    }
    assert session.custom_password_encrypted is None


def test_pool_proxy_rotates_three_times_before_terminal_failure(db: Session) -> None:
    """二维码生成前只在同地区尝试三个不同代理，第三次失败后结束会话。"""

    tenant, channel = _seed_channel(db, code="rotate-proxy")
    proxies = [
        _add_proxy(db, name=f"杭州代理{index}", host=f"10.0.1.{index}")
        for index in range(1, 4)
    ]
    with tenant_scope(tenant.id):
        session = create_login_proxy_session(
            db,
            channel=channel,
            mode="platform_pool",
            values={"province": "浙江省", "city": "杭州市"},
        )
        assert session.platform_proxy_id == proxies[0].id
        rotate_login_proxy_before_qrcode(db, session, error_code="first_failed")
        assert session.platform_proxy_id == proxies[1].id
        rotate_login_proxy_before_qrcode(db, session, error_code="second_failed")
        assert session.platform_proxy_id == proxies[2].id
        with pytest.raises(LoginProxyPoolError, match="proxy_retry_exhausted"):
            rotate_login_proxy_before_qrcode(db, session, error_code="third_failed")

    db.refresh(session)
    assert session.state == "failed"
    assert session.attempt_count == 3
    assert all(proxy.lease_key is None for proxy in proxies)


def test_qrcode_ready_session_rejects_proxy_rotation(db: Session) -> None:
    """二维码生成后必须锁定当前代理，不能让旧二维码继续使用新 IP。"""

    tenant, channel = _seed_channel(db, code="locked-proxy")
    _add_proxy(db, name="杭州锁定", host="10.0.2.1")
    with tenant_scope(tenant.id):
        session = create_login_proxy_session(
            db,
            channel=channel,
            mode="platform_pool",
            values={"province": "浙江省", "city": "杭州市"},
        )
        mark_login_proxy_qrcode_ready(
            db,
            session,
            qrcode_expires_at=now_shanghai() + timedelta(minutes=3),
        )
        with pytest.raises(LoginProxyPoolError, match="proxy_session_locked"):
            rotate_login_proxy_before_qrcode(db, session, error_code="late_failure")

    assert session.state == "qrcode_ready"
