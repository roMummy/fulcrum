"""平台代理池和单次扫码登录代理会话的通用业务服务。"""

from __future__ import annotations

import csv
import hashlib
import io
import json
import secrets
import socket
from datetime import datetime, timedelta
from typing import Any

from sqlalchemy import case, func, or_, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.core.tenant_context import require_tenant_id
from app.core.time import ensure_shanghai_datetime, now_shanghai
from app.core.settings import settings
from app.models.entities import Channel
from app.models.saas import (
    LOGIN_PROXY_MODES,
    LOGIN_PROXY_SESSION_STATUSES,
    LoginProxySession,
    ProxyConnector,
    PlatformProxy,
)
from app.services.security import decrypt_secret, encrypt_secret, mask_secret
from app.services.local_proxy_connector import CONNECTOR_OFFLINE_AFTER


ACTIVE_LOGIN_PROXY_STATES = frozenset({"preparing", "qrcode_ready"})
TERMINAL_LOGIN_PROXY_STATES = LOGIN_PROXY_SESSION_STATUSES - ACTIVE_LOGIN_PROXY_STATES
MAX_POOL_PROXY_ATTEMPTS = 3
LOGIN_PROXY_SESSION_TTL = timedelta(minutes=10)
CSV_HEADERS = ("名称", "省", "市", "主机", "端口", "用户名", "密码")


class LoginProxyPoolError(ValueError):
    """代理池配置、分配和登录会话状态错误。"""


def _normalize_text(value: Any, *, error_code: str, max_length: int) -> str:
    """清理普通文本，并在写库前限制长度，避免把数据库异常暴露给页面。"""

    normalized = str(value or "").strip()
    if not normalized or len(normalized) > max_length:
        raise LoginProxyPoolError(error_code)
    return normalized


def _normalize_optional_text(value: Any, *, max_length: int) -> str:
    """清理允许为空的字段，超长输入仍按非法配置拒绝。"""

    normalized = str(value or "").strip()
    if len(normalized) > max_length:
        raise LoginProxyPoolError("proxy_value_too_long")
    return normalized


def _normalize_port(value: Any) -> int:
    """把表单或 CSV 端口统一转换为整数并限制到有效网络端口。"""

    try:
        port = int(str(value or "").strip())
    except (TypeError, ValueError) as exc:
        raise LoginProxyPoolError("proxy_port_invalid") from exc
    if port < 1 or port > 65535:
        raise LoginProxyPoolError("proxy_port_invalid")
    return port


def _mask_proxy_host(host: str) -> str:
    """平台代理列表只显示可识别的脱敏地址，不向租户或日志暴露完整端点。"""

    normalized = str(host or "").strip()
    if not normalized:
        return "-"
    parts = normalized.split(".")
    if len(parts) == 4 and all(part.isdigit() for part in parts):
        return f"{parts[0]}.{parts[1]}.*.*"
    return mask_secret(normalized)


def _parse_attempted_proxy_ids(session: LoginProxySession) -> list[int]:
    """读取本次会话已经尝试的代理主键，非法历史值直接视为空列表。"""

    try:
        payload = json.loads(session.attempted_proxy_ids_json or "[]")
    except (TypeError, ValueError):
        return []
    if not isinstance(payload, list):
        return []
    return [int(item) for item in payload if isinstance(item, int) and item > 0]


def _proxy_values(values: dict[str, object]) -> dict[str, object]:
    """将页面和 CSV 的平台代理字段转换为唯一标准结构。"""

    return {
        "name": _normalize_text(values.get("name"), error_code="proxy_name_required", max_length=100),
        "province": _normalize_text(
            values.get("province"),
            error_code="proxy_province_required",
            max_length=64,
        ),
        "city": _normalize_text(values.get("city"), error_code="proxy_city_required", max_length=64),
        "host": _normalize_text(values.get("host"), error_code="proxy_host_required", max_length=255),
        "port": _normalize_port(values.get("port")),
        "username": _normalize_optional_text(values.get("username"), max_length=191),
        "password": _normalize_optional_text(values.get("password"), max_length=500),
    }


def save_platform_proxy(
    db: Session,
    *,
    proxy_id: int | None,
    values: dict[str, object],
) -> PlatformProxy:
    """新增或编辑平台代理，密码留空时保持已有密文不变。"""

    normalized = _proxy_values(values)
    proxy = db.get(PlatformProxy, int(proxy_id)) if proxy_id else None
    if proxy_id and proxy is None:
        raise LoginProxyPoolError("proxy_not_found")

    duplicate = db.scalar(
        select(PlatformProxy).where(
            PlatformProxy.host == normalized["host"],
            PlatformProxy.port == normalized["port"],
            PlatformProxy.username == normalized["username"],
            PlatformProxy.id != int(proxy.id) if proxy is not None else True,
        )
    )
    if duplicate is not None:
        raise LoginProxyPoolError("proxy_duplicate")

    if proxy is None:
        proxy = PlatformProxy(
            name=str(normalized["name"]),
            province=str(normalized["province"]),
            city=str(normalized["city"]),
            host=str(normalized["host"]),
            port=int(normalized["port"]),
            username=str(normalized["username"]),
            password_encrypted=encrypt_secret(str(normalized["password"])),
            is_enabled=True,
            health_status="healthy",
        )
        db.add(proxy)
    else:
        proxy.name = str(normalized["name"])
        proxy.province = str(normalized["province"])
        proxy.city = str(normalized["city"])
        proxy.host = str(normalized["host"])
        proxy.port = int(normalized["port"])
        proxy.username = str(normalized["username"])
        if normalized["password"]:
            proxy.password_encrypted = encrypt_secret(str(normalized["password"]))

    try:
        db.commit()
    except IntegrityError as exc:
        db.rollback()
        raise LoginProxyPoolError("proxy_duplicate") from exc
    db.refresh(proxy)
    return proxy


def import_platform_proxies_csv(db: Session, *, content: bytes) -> dict[str, object]:
    """按固定中文表头批量导入代理，并逐行返回脱敏结果。"""

    try:
        text = bytes(content or b"").decode("utf-8-sig")
    except UnicodeDecodeError as exc:
        raise LoginProxyPoolError("proxy_csv_encoding_invalid") from exc
    reader = csv.DictReader(io.StringIO(text))
    if tuple(reader.fieldnames or ()) != CSV_HEADERS:
        raise LoginProxyPoolError("proxy_csv_headers_invalid")

    result: dict[str, object] = {"success": 0, "skipped": 0, "failed": 0, "errors": []}
    seen_endpoints: set[tuple[str, int, str]] = set()
    for row_number, row in enumerate(reader, start=2):
        try:
            normalized = _proxy_values(
                {
                    "name": row.get("名称"),
                    "province": row.get("省"),
                    "city": row.get("市"),
                    "host": row.get("主机"),
                    "port": row.get("端口"),
                    "username": row.get("用户名"),
                    "password": row.get("密码"),
                }
            )
            endpoint = (
                str(normalized["host"]),
                int(normalized["port"]),
                str(normalized["username"]),
            )
            duplicate = endpoint in seen_endpoints or db.scalar(
                select(PlatformProxy.id).where(
                    PlatformProxy.host == endpoint[0],
                    PlatformProxy.port == endpoint[1],
                    PlatformProxy.username == endpoint[2],
                )
            )
            if duplicate:
                result["skipped"] = int(result["skipped"]) + 1
                continue
            seen_endpoints.add(endpoint)
            # 每一行使用独立保存点。单行数据库约束失败时只回滚这一行，不能把
            # 同一文件中此前已经校验成功的代理一并撤销。
            with db.begin_nested():
                db.add(
                    PlatformProxy(
                        name=str(normalized["name"]),
                        province=str(normalized["province"]),
                        city=str(normalized["city"]),
                        host=endpoint[0],
                        port=endpoint[1],
                        username=endpoint[2],
                        password_encrypted=encrypt_secret(str(normalized["password"])),
                        is_enabled=True,
                        health_status="healthy",
                    )
                )
                db.flush()
            result["success"] = int(result["success"]) + 1
        except (IntegrityError, LoginProxyPoolError) as exc:
            # 失败明细只返回行号和稳定错误码，不拼接可能包含代理凭证的原始 CSV 行。
            result["failed"] = int(result["failed"]) + 1
            errors = list(result["errors"])
            error_code = "proxy_duplicate" if isinstance(exc, IntegrityError) else str(exc)
            errors.append({"row": row_number, "code": error_code or "proxy_csv_row_invalid"})
            result["errors"] = errors
    db.commit()
    return result


def list_platform_proxies(db: Session) -> list[dict[str, object]]:
    """返回平台页面可展示的脱敏代理列表。"""

    now = now_shanghai()
    rows = db.scalars(select(PlatformProxy).order_by(PlatformProxy.id.desc())).all()
    return [
        {
            "id": int(proxy.id),
            "name": proxy.name,
            "province": proxy.province,
            "city": proxy.city,
            # 完整主机只供平台员工编辑弹窗复用；清单主体仍固定使用 masked_host。
            "host": proxy.host,
            "masked_host": _mask_proxy_host(proxy.host),
            "port": int(proxy.port),
            "username": proxy.username,
            "has_password": bool(proxy.password_encrypted),
            "is_enabled": bool(proxy.is_enabled),
            "health_status": proxy.health_status,
            "is_leased": bool(
                proxy.lease_key
                and proxy.lease_expires_at
                and ensure_shanghai_datetime(proxy.lease_expires_at) > now
            ),
            "last_checked_at": proxy.last_checked_at,
            "last_assigned_at": proxy.last_assigned_at,
            "last_result_summary": proxy.last_result_summary or "",
        }
        for proxy in rows
    ]


def build_platform_proxy_summary(db: Session) -> dict[str, int]:
    """汇总代理总数、健康、异常和当前有效租约数量。"""

    now = now_shanghai()
    return {
        "total": int(db.scalar(select(func.count(PlatformProxy.id))) or 0),
        "healthy": int(
            db.scalar(
                select(func.count(PlatformProxy.id)).where(
                    PlatformProxy.is_enabled.is_(True),
                    PlatformProxy.health_status == "healthy",
                )
            )
            or 0
        ),
        "unhealthy": int(
            db.scalar(
                select(func.count(PlatformProxy.id)).where(
                    or_(PlatformProxy.is_enabled.is_(False), PlatformProxy.health_status == "unhealthy")
                )
            )
            or 0
        ),
        "leased": int(
            db.scalar(
                select(func.count(PlatformProxy.id)).where(
                    PlatformProxy.lease_key.is_not(None),
                    PlatformProxy.lease_expires_at > now,
                )
            )
            or 0
        ),
    }


def set_platform_proxy_enabled(db: Session, *, proxy_id: int, is_enabled: bool) -> PlatformProxy:
    """启停平台代理；停用不强行中断已经生成二维码的当前会话。"""

    proxy = db.get(PlatformProxy, int(proxy_id))
    if proxy is None:
        raise LoginProxyPoolError("proxy_not_found")
    proxy.is_enabled = bool(is_enabled)
    db.commit()
    db.refresh(proxy)
    return proxy


def check_platform_proxy(
    db: Session,
    *,
    proxy_id: int,
    timeout_seconds: float = 3.0,
) -> PlatformProxy:
    """检查 SOCKS 端口连通性；真实登录可用性仍以 874 取码结果为准。"""

    proxy = db.get(PlatformProxy, int(proxy_id))
    if proxy is None:
        raise LoginProxyPoolError("proxy_not_found")
    checked_at = now_shanghai()
    try:
        with socket.create_connection((proxy.host, int(proxy.port)), timeout=max(float(timeout_seconds), 0.1)):
            pass
    except OSError:
        proxy.health_status = "unhealthy"
        proxy.consecutive_failures = max(int(proxy.consecutive_failures or 0), MAX_POOL_PROXY_ATTEMPTS)
        proxy.last_failure_at = checked_at
        proxy.last_result_summary = "端口连接失败"
    else:
        proxy.health_status = "healthy"
        proxy.consecutive_failures = 0
        proxy.last_success_at = checked_at
        proxy.last_result_summary = "端口连接正常"
    proxy.last_checked_at = checked_at
    db.commit()
    db.refresh(proxy)
    return proxy


def list_available_proxy_regions(db: Session) -> list[dict[str, object]]:
    """只向租户返回有可分配代理的省市及数量，不泄露平台代理清单。"""

    now = now_shanghai()
    rows = db.execute(
        select(PlatformProxy.province, PlatformProxy.city, func.count(PlatformProxy.id))
        .where(
            PlatformProxy.is_enabled.is_(True),
            PlatformProxy.health_status == "healthy",
            or_(
                PlatformProxy.lease_key.is_(None),
                PlatformProxy.lease_expires_at.is_(None),
                PlatformProxy.lease_expires_at <= now,
            ),
        )
        .group_by(PlatformProxy.province, PlatformProxy.city)
        .order_by(PlatformProxy.province.asc(), PlatformProxy.city.asc())
    ).all()
    return [
        {"province": str(province), "city": str(city), "available": int(count)}
        for province, city, count in rows
    ]


def _release_platform_proxy(db: Session, session: LoginProxySession) -> None:
    """只释放仍由当前会话持有的租约，避免旧终态覆盖其他会话的新租约。"""

    if not session.platform_proxy_id:
        return
    proxy = db.get(PlatformProxy, int(session.platform_proxy_id))
    if proxy is not None and proxy.lease_key == session.session_key:
        proxy.lease_key = None
        proxy.lease_expires_at = None


def _allocate_platform_proxy(db: Session, session: LoginProxySession) -> PlatformProxy:
    """在指定省市内锁定最久未使用的可用代理并写入服务端租约。"""

    now = now_shanghai()
    attempted_ids = _parse_attempted_proxy_ids(session)
    query = (
        select(PlatformProxy)
        .where(
            PlatformProxy.province == session.province,
            PlatformProxy.city == session.city,
            PlatformProxy.is_enabled.is_(True),
            PlatformProxy.health_status == "healthy",
            or_(
                PlatformProxy.lease_key.is_(None),
                PlatformProxy.lease_expires_at.is_(None),
                PlatformProxy.lease_expires_at <= now,
            ),
        )
        .order_by(
            case((PlatformProxy.last_assigned_at.is_(None), 0), else_=1),
            PlatformProxy.last_assigned_at.asc(),
            PlatformProxy.id.asc(),
        )
        .with_for_update()
    )
    if attempted_ids:
        query = query.where(PlatformProxy.id.not_in(attempted_ids))
    proxy = db.scalar(query)
    if proxy is None:
        raise LoginProxyPoolError("proxy_region_unavailable")

    proxy.lease_key = session.session_key
    proxy.lease_expires_at = session.expires_at
    proxy.last_assigned_at = now
    session.platform_proxy_id = int(proxy.id)
    session.attempt_count = int(session.attempt_count or 0) + 1
    attempted_ids.append(int(proxy.id))
    session.attempted_proxy_ids_json = json.dumps(attempted_ids, ensure_ascii=True)
    return proxy


def _finish_login_proxy_session_no_commit(
    db: Session,
    session: LoginProxySession,
    *,
    state: str,
    error_code: str = "",
    summary: str = "",
) -> None:
    """在同一事务中结束会话、释放租约并清除自有代理密码。"""

    if state not in TERMINAL_LOGIN_PROXY_STATES:
        raise LoginProxyPoolError("proxy_session_terminal_state_invalid")
    _release_platform_proxy(db, session)
    if session.mode == "local_connector":
        # 临时中继凭证不随终态记录保留；连接器可以继续在线，但本次扫码隧道必须失效。
        from app.services.local_proxy_connector_ws import local_proxy_connector_ws_registry

        local_proxy_connector_ws_registry.remove_session_credentials(session.session_key)
    session.state = state
    session.last_error_code = str(error_code or "").strip() or None
    session.result_summary = str(summary or "").strip()[:255] or None
    session.custom_password_encrypted = None


def create_login_proxy_session(
    db: Session,
    *,
    channel: Channel,
    mode: str,
    values: dict[str, object],
) -> LoginProxySession:
    """为当前可信租户创建新的扫码代理会话，并结束同渠道旧会话。"""

    tenant_id = require_tenant_id()
    if int(channel.tenant_id) != tenant_id:
        raise LoginProxyPoolError("proxy_channel_tenant_mismatch")
    normalized_mode = str(mode or "").strip()
    if normalized_mode not in LOGIN_PROXY_MODES:
        raise LoginProxyPoolError("proxy_mode_invalid")

    now = now_shanghai()
    active_sessions = db.scalars(
        select(LoginProxySession).where(
            LoginProxySession.channel_id == int(channel.id),
            LoginProxySession.state.in_(ACTIVE_LOGIN_PROXY_STATES),
        )
    ).all()
    for active in active_sessions:
        _finish_login_proxy_session_no_commit(
            db,
            active,
            state="cancelled",
            summary="已由新的扫码登录替换",
        )

    session = LoginProxySession(
        tenant_id=tenant_id,
        channel_id=int(channel.id),
        session_key=secrets.token_hex(24),
        mode=normalized_mode,
        state="preparing",
        attempt_count=0,
        attempted_proxy_ids_json="[]",
        expires_at=now + LOGIN_PROXY_SESSION_TTL,
    )
    if normalized_mode == "platform_pool":
        session.province = _normalize_text(
            values.get("province"),
            error_code="proxy_province_required",
            max_length=64,
        )
        session.city = _normalize_text(
            values.get("city"),
            error_code="proxy_city_required",
            max_length=64,
        )
    elif normalized_mode == "custom":
        session.custom_host = _normalize_text(
            values.get("host"),
            error_code="proxy_host_required",
            max_length=255,
        )
        session.custom_port = _normalize_port(values.get("port"))
        session.custom_username = _normalize_optional_text(values.get("username"), max_length=191)
        session.custom_password_encrypted = encrypt_secret(
            _normalize_optional_text(values.get("password"), max_length=500)
        )
    elif normalized_mode == "local_connector":
        # 本地连接器只绑定可信租户下的在线设备；本地 SOCKS 地址和密码不会进入此表。
        try:
            connector_id = int(str(values.get("connector_id") or "").strip())
        except (TypeError, ValueError) as exc:
            raise LoginProxyPoolError("proxy_connector_required") from exc
        connector = db.scalar(
            select(ProxyConnector).where(
                ProxyConnector.id == connector_id,
                ProxyConnector.tenant_id == tenant_id,
                ProxyConnector.status == "active",
            )
        )
        if connector is None or connector.last_seen_at is None or now - ensure_shanghai_datetime(connector.last_seen_at) > CONNECTOR_OFFLINE_AFTER:
            raise LoginProxyPoolError("proxy_connector_unavailable")
        session.proxy_connector_id = connector.id
        session.proxy_cleanup_state = "pending"
        # 中继口令只由内存中的 WS 注册表持有，数据库仅保存摘要用于会话绑定校验。
        relay_username = secrets.token_urlsafe(18)
        relay_password = secrets.token_urlsafe(32)
        session.relay_username_hash = hashlib.sha256(relay_username.encode("utf-8")).hexdigest()
        session.relay_password_hash = hashlib.sha256(relay_password.encode("utf-8")).hexdigest()
        session.relay_expires_at = session.expires_at
        session._relay_username = relay_username
        session._relay_password = relay_password

    try:
        db.add(session)
        db.flush()
        if normalized_mode == "platform_pool":
            _allocate_platform_proxy(db, session)
        db.commit()
    except BaseException:
        # 分配失败时必须回滚新会话以及对旧会话的临时修改，否则调用方捕获业务
        # 错误后继续复用 Session，可能把没有代理的半成品会话意外提交。
        db.rollback()
        raise
    db.refresh(session)
    if normalized_mode == "local_connector":
        # 874 使用的是服务端临时 SOCKS 监听器；明文只交给当前进程的 WS 注册表。
        from app.services.local_proxy_connector_ws import local_proxy_connector_ws_registry

        local_proxy_connector_ws_registry.register_session_credentials(
            session.session_key,
            str(getattr(session, "_relay_username", "")),
            str(getattr(session, "_relay_password", "")),
        )
    return session


def get_active_login_proxy_session(db: Session, *, channel_id: int) -> LoginProxySession | None:
    """读取渠道当前有效会话；已超时记录会在返回前立即结束并释放租约。"""

    session = db.scalar(
        select(LoginProxySession)
        .where(
            LoginProxySession.channel_id == int(channel_id),
            LoginProxySession.state.in_(ACTIVE_LOGIN_PROXY_STATES),
        )
        .order_by(LoginProxySession.id.desc())
    )
    if session is None:
        return None
    if ensure_shanghai_datetime(session.expires_at) <= now_shanghai():
        _finish_login_proxy_session_no_commit(db, session, state="expired", summary="登录会话已超时")
        db.commit()
        return None
    return session


def build_login_proxy_session_summary(db: Session, *, channel_id: int) -> dict[str, object]:
    """返回租户页面可展示的最近登录网络摘要，严格排除代理端点和凭证明文。"""

    session = db.scalar(
        select(LoginProxySession)
        .where(LoginProxySession.channel_id == int(channel_id))
        .order_by(LoginProxySession.id.desc())
    )
    if session is None:
        return {
            "mode": "",
            "state": "",
            "network_label": "",
        }
    if session.mode == "platform_pool":
        # 智能选择只向租户回显其主动选择的省市，绝不返回平台代理 ID、IP 或账号。
        network_label = " ".join(
            item for item in (str(session.province or "").strip(), str(session.city or "").strip()) if item
        ) or "智能选择"
    elif session.mode == "custom":
        network_label = "自有代理"
    else:
        network_label = "直接登录"
    return {
        "mode": session.mode,
        "state": session.state,
        "network_label": network_label,
    }


def resolve_login_proxy_credentials(
    db: Session,
    session: LoginProxySession,
) -> dict[str, str] | None:
    """将三种登录模式解析为 874 唯一接受的统一代理字典。"""

    if session.state not in ACTIVE_LOGIN_PROXY_STATES:
        raise LoginProxyPoolError("proxy_session_inactive")
    if session.mode == "direct":
        return None
    if session.mode == "custom":
        return {
            "host": str(session.custom_host or ""),
            "port": str(session.custom_port or ""),
            "username": str(session.custom_username or ""),
            "password": str(decrypt_secret(session.custom_password_encrypted) or ""),
        }
    if session.mode == "local_connector":
        from app.services.local_proxy_connector_ws import local_proxy_connector_ws_registry

        credentials = local_proxy_connector_ws_registry.get_session_credentials(session.session_key)
        if credentials is None:
            raise LoginProxyPoolError("proxy_connector_offline")
        return {
            "host": settings.local_proxy_relay_host,
            "port": str(settings.local_proxy_relay_port),
            "username": credentials[0],
            "password": credentials[1],
        }
    proxy = db.get(PlatformProxy, int(session.platform_proxy_id or 0))
    if proxy is None or proxy.lease_key != session.session_key:
        raise LoginProxyPoolError("proxy_lease_lost")
    return {
        "host": proxy.host,
        "port": str(proxy.port),
        "username": proxy.username,
        "password": str(decrypt_secret(proxy.password_encrypted) or ""),
    }


def record_login_proxy_success(db: Session, session: LoginProxySession, *, summary: str) -> None:
    """用真实取码或登录成功结果恢复池代理健康状态。"""

    if session.mode != "platform_pool" or not session.platform_proxy_id:
        return
    proxy = db.get(PlatformProxy, int(session.platform_proxy_id))
    if proxy is None:
        return
    proxy.health_status = "healthy"
    proxy.consecutive_failures = 0
    proxy.last_success_at = now_shanghai()
    proxy.last_result_summary = str(summary or "登录成功").strip()[:255]


def record_login_proxy_failure(db: Session, session: LoginProxySession, *, summary: str) -> None:
    """记录真实取码失败，连续三次失败后停止向新会话分配该代理。"""

    if session.mode != "platform_pool" or not session.platform_proxy_id:
        return
    proxy = db.get(PlatformProxy, int(session.platform_proxy_id))
    if proxy is None:
        return
    proxy.consecutive_failures = int(proxy.consecutive_failures or 0) + 1
    if proxy.consecutive_failures >= MAX_POOL_PROXY_ATTEMPTS:
        proxy.health_status = "unhealthy"
    proxy.last_failure_at = now_shanghai()
    proxy.last_result_summary = str(summary or "登录失败").strip()[:255]


def rotate_login_proxy_before_qrcode(
    db: Session,
    session: LoginProxySession,
    *,
    error_code: str,
) -> LoginProxySession:
    """二维码生成前释放失败代理并在同一地区分配下一个，最多尝试三次。"""

    if session.state != "preparing":
        raise LoginProxyPoolError("proxy_session_locked")
    if session.mode != "platform_pool":
        raise LoginProxyPoolError("proxy_rotation_not_supported")
    record_login_proxy_failure(db, session, summary="二维码生成失败")
    _release_platform_proxy(db, session)
    session.platform_proxy_id = None
    session.last_error_code = str(error_code or "proxy_login_failed").strip()
    if int(session.attempt_count or 0) >= MAX_POOL_PROXY_ATTEMPTS:
        _finish_login_proxy_session_no_commit(
            db,
            session,
            state="failed",
            error_code=session.last_error_code,
            summary="同地区代理尝试次数已用完",
        )
        db.commit()
        raise LoginProxyPoolError("proxy_retry_exhausted")
    _allocate_platform_proxy(db, session)
    db.commit()
    db.refresh(session)
    return session


def mark_login_proxy_qrcode_ready(
    db: Session,
    session: LoginProxySession,
    *,
    qrcode_expires_at: datetime | None,
) -> None:
    """二维码生成后锁定当前代理，并把会话期限收紧到二维码有效期。"""

    if session.state != "preparing":
        raise LoginProxyPoolError("proxy_session_state_invalid")
    session.state = "qrcode_ready"
    if qrcode_expires_at is not None:
        resolved_expiry = ensure_shanghai_datetime(qrcode_expires_at)
        session.qrcode_expires_at = resolved_expiry
        if resolved_expiry < ensure_shanghai_datetime(session.expires_at):
            session.expires_at = resolved_expiry
            if session.mode == "platform_pool" and session.platform_proxy_id:
                proxy = db.get(PlatformProxy, int(session.platform_proxy_id))
                if proxy is not None and proxy.lease_key == session.session_key:
                    proxy.lease_expires_at = resolved_expiry
    record_login_proxy_success(db, session, summary="二维码生成成功")
    db.commit()


def finish_login_proxy_session(
    db: Session,
    session: LoginProxySession,
    *,
    state: str,
    error_code: str = "",
    summary: str = "",
) -> None:
    """结束登录会话并提交租约释放和敏感凭证清理。"""

    _finish_login_proxy_session_no_commit(
        db,
        session,
        state=state,
        error_code=error_code,
        summary=summary,
    )
    db.commit()


def expire_login_proxy_sessions(db: Session) -> int:
    """批量结束超时会话，供页面访问和后台维护任务复用。"""

    now = now_shanghai()
    sessions = db.scalars(
        select(LoginProxySession).where(
            LoginProxySession.state.in_(ACTIVE_LOGIN_PROXY_STATES),
            LoginProxySession.expires_at <= now,
        )
    ).all()
    for session in sessions:
        _finish_login_proxy_session_no_commit(db, session, state="expired", summary="登录会话已超时")
    if sessions:
        db.commit()
    return len(sessions)


def fail_login_proxy_session_by_key(
    db: Session,
    *,
    session_key: str,
    tenant_id: int,
    error_code: str = "proxy_connector_disconnected",
) -> bool:
    """连接器断开时立即终止绑定的扫码会话并清理临时中继凭证。"""

    session = db.scalar(
        select(LoginProxySession).where(
            LoginProxySession.session_key == str(session_key or "").strip(),
            LoginProxySession.tenant_id == int(tenant_id),
            LoginProxySession.state.in_(ACTIVE_LOGIN_PROXY_STATES),
        )
    )
    if session is None:
        return False
    _finish_login_proxy_session_no_commit(
        db,
        session,
        state="failed",
        error_code=error_code,
        summary="本地代理连接器已断开",
    )
    db.commit()
    return True
