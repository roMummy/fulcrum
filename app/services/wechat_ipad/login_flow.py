"""微信 iPad 扫码登录与通用登录代理会话之间的唯一编排层。"""

from __future__ import annotations

from datetime import datetime
from typing import Any

from sqlalchemy.orm import Session

from app.core.time import ensure_shanghai_datetime
from app.models.entities import Channel
from app.services.login_proxy_pool import (
    LoginProxyPoolError,
    finish_login_proxy_session,
    get_active_login_proxy_session,
    mark_login_proxy_qrcode_ready,
    record_login_proxy_failure,
    record_login_proxy_success,
    resolve_login_proxy_credentials,
    rotate_login_proxy_before_qrcode,
)
from app.services.wechat_ipad.client import WechatIpadApiClient
from app.services.wechat_ipad.service import (
    build_wechat_ipad_overview,
    poll_wechat_ipad_login_status,
    start_wechat_ipad_login,
)


class WechatIpadLoginFlowError(ValueError):
    """扫码登录缺少有效会话或进入非法状态时使用的稳定业务错误。"""


def _clear_local_connector_proxy(
    db: Session,
    channel: Channel,
    session: Any,
    overview: dict[str, Any],
    *,
    client: WechatIpadApiClient,
) -> bool:
    """登录成功后先清空 874 保存的 Proxy，成功后才能释放本地连接器隧道。"""

    if session.mode != "local_connector":
        return True
    wxid = str(overview.get("wxid") or "").strip()
    if not wxid:
        session.proxy_cleanup_state = "failed"
        session.proxy_cleanup_error = "wxid_missing_after_login"
        db.commit()
        return False
    try:
        result = client.set_proxy(channel, wxid=wxid, proxy={})
    except Exception:
        session.proxy_cleanup_state = "failed"
        session.proxy_cleanup_error = "setproxy_request_failed"
        db.commit()
        return False
    if not client.is_success(result):
        session.proxy_cleanup_state = "failed"
        session.proxy_cleanup_error = "setproxy_rejected"
        db.commit()
        return False
    session.proxy_cleanup_state = "completed"
    session.proxy_cleanup_error = None
    db.commit()
    return True


def _parse_expiry(value: Any) -> datetime | None:
    """把登录总览中的 ISO 时间转换为项目统一时区时间。"""

    text = str(value or "").strip()
    if not text:
        return None
    try:
        return ensure_shanghai_datetime(datetime.fromisoformat(text.replace("Z", "+00:00")))
    except ValueError:
        return None


def _qrcode_ready(overview: dict[str, Any]) -> bool:
    """二维码必须同时进入等待扫码状态并具有可展示地址才算生成成功。"""

    return bool(
        overview.get("qrcode_status") == "waiting_scan"
        and overview.get("qrcode_image_url")
    )


def start_login_with_proxy_session(
    db: Session,
    channel: Channel,
    *,
    client: WechatIpadApiClient | None = None,
) -> dict[str, Any]:
    """按当前服务端代理会话取码，池代理失败时在同地区最多轮换三次。"""

    session = get_active_login_proxy_session(db, channel_id=int(channel.id))
    if session is None:
        raise WechatIpadLoginFlowError("login_proxy_session_missing")
    if session.state == "qrcode_ready":
        # 二维码已经生成后严禁重复取码或换代理；页面刷新只读取当前业务总览。
        return build_wechat_ipad_overview(channel)


    while True:
        proxy = resolve_login_proxy_credentials(db, session)
        overview = start_wechat_ipad_login(
            db,
            channel,
            login_profile="ipad",
            proxy=proxy,
            client=client,
        )
        if _qrcode_ready(overview):
            mark_login_proxy_qrcode_ready(
                db,
                session,
                qrcode_expires_at=_parse_expiry(overview.get("qrcode_expires_at")),
            )
            return overview

        error_code = str(overview.get("last_error") or "qrcode_prepare_failed").strip()
        if session.mode != "platform_pool":
            finish_login_proxy_session(
                db,
                session,
                state="failed",
                error_code=error_code,
                summary="二维码准备失败",
            )
            return overview
        try:
            session = rotate_login_proxy_before_qrcode(db, session, error_code=error_code)
        except LoginProxyPoolError as exc:
            if str(exc) != "proxy_retry_exhausted":
                raise
            return overview


def poll_login_with_proxy_session(
    db: Session,
    channel: Channel,
    *,
    client: WechatIpadApiClient | None = None,
) -> dict[str, Any]:
    """轮询二维码业务状态，并在成功、失败或过期时释放本次登录代理。"""

    session = get_active_login_proxy_session(db, channel_id=int(channel.id))
    if session is None:
        # 登录完成后的最后一次页面轮询允许直接读取在线结果；其他状态仍显式拒绝。
        overview = build_wechat_ipad_overview(channel)
        if (
            overview.get("login_state") == "online"
            or overview.get("qrcode_status") == "success"
            or bool(overview.get("qrcode_is_expired"))
        ):
            return overview
        raise WechatIpadLoginFlowError("login_proxy_session_missing")

    overview = poll_wechat_ipad_login_status(db, channel, client=client)
    login_state = str(overview.get("login_state") or "")
    qrcode_status = str(overview.get("qrcode_status") or "")
    if login_state == "online" or qrcode_status == "success":
        active_client = client or WechatIpadApiClient()
        if not _clear_local_connector_proxy(
            db,
            channel,
            session,
            overview,
            client=active_client,
        ):
            # 874 登录已经完成，但本地代理仍绑定在登录数据中；保持会话和连接器在线，
            # 页面可以重试清理，不能提前告诉用户可以关闭连接器。
            overview["login_state"] = "proxy_cleanup_required"
            overview["last_error"] = "proxy_cleanup_required"
            return overview
        record_login_proxy_success(db, session, summary="扫码登录成功")
        finish_login_proxy_session(db, session, state="completed", summary="扫码登录成功")
    elif qrcode_status == "expired" or bool(overview.get("qrcode_is_expired")):
        finish_login_proxy_session(db, session, state="expired", summary="二维码已过期")
    elif qrcode_status == "failed" or login_state in {"login_failed", "login_init_failed"}:
        record_login_proxy_failure(db, session, summary="扫码登录失败")
        finish_login_proxy_session(
            db,
            session,
            state="failed",
            error_code=str(overview.get("last_error") or "login_failed"),
            summary="扫码登录失败",
        )
    return overview


def cancel_login_with_proxy_session(db: Session, channel: Channel) -> None:
    """取消当前扫码会话并释放代理；新的登录会话会生成新二维码覆盖旧状态。"""

    session = get_active_login_proxy_session(db, channel_id=int(channel.id))
    if session is None:
        return
    finish_login_proxy_session(db, session, state="cancelled", summary="用户重新开始登录")
