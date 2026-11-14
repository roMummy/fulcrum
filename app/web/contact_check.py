"""好友检测页面与持久进度接口，数据库操作统一在线程池路由内执行。"""

from __future__ import annotations

from contextlib import contextmanager
from typing import Any

from fastapi import APIRouter, Body, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse
from fastapi.templating import Jinja2Templates

from app.core.db import TenantSessionLocal
from app.core.tenant_db import open_tenant_session
from app.models.entities import Channel
from app.services.saas_auth import has_tenant_role
from app.services.wechat_webridge import friend_check, friend_check_runs
from app.web.auth_common import (
    TENANT_CSRF_COOKIE,
    TENANT_LOGOUT_PATH,
    TENANT_PASSWORD_PATH,
    build_auth_page_context,
    require_tenant_csrf,
)
from app.web.tenant_navigation import build_tenant_navigation


router = APIRouter(prefix="/contacts/check", tags=["friend-check"])
templates = Jinja2Templates(directory="app/templates")
NO_STORE = {"Cache-Control": "no-store"}


def _principal(request: Request, *, edit: bool = False):
    principal = getattr(request.state, "current_user", None)
    if principal is None:
        raise HTTPException(status_code=401, detail="请先登录")
    if not has_tenant_role(principal, "operator" if edit else "readonly"):
        raise HTTPException(status_code=403, detail="当前账号没有此操作权限")
    return principal


@contextmanager
def _session(request: Request, *, edit: bool = False):
    _principal(request, edit=edit)
    db = open_tenant_session(TenantSessionLocal)
    try:
        if edit:
            require_tenant_csrf(db, request, request.headers.get("X-CSRF-Token", ""))
        yield db
    except friend_check.FriendCheckError as exc:
        db.rollback()
        raise HTTPException(status_code=exc.status_code, detail=exc.message) from exc
    finally:
        db.close()


def _channel(db, channel_id: int) -> Channel:
    channel = db.get(Channel, channel_id)
    if channel is None or channel.channel_type != "wechat_webridge" or channel.deleted_at is not None:
        raise HTTPException(status_code=404, detail="未找到可检测的微桥账号")
    return channel


def _generation(body: dict) -> str:
    # 所有页面写入与轮询都必须声明当前代次，清空后的旧页面不得继续消费旧任务。
    value = body.get("generation")
    if not isinstance(value, str) or not value:
        raise HTTPException(status_code=409, detail="检测页面已更新，请重新打开页面读取已保存的进度")
    return value


@router.get("", response_class=HTMLResponse)
def contact_check_page(request: Request, channel_id: int = 0):
    principal = _principal(request)
    with _session(request) as db:
        channels = friend_check.list_check_channels(db)
        selected = next((item["id"] for item in channels if item["id"] == channel_id), 0)
        if not selected and channels:
            selected = channels[0]["id"]
        context = build_auth_page_context(
            request,
            page_title="好友检测",
            current_user=principal,
            channels=channels,
            selected_channel_id=selected,
            can_edit=has_tenant_role(principal, "operator"),
            csrf_token=request.cookies.get(TENANT_CSRF_COOKIE, ""),
            password_action=TENANT_PASSWORD_PATH,
            logout_action=TENANT_LOGOUT_PATH,
        )
        context.update(build_tenant_navigation(request.url.path))
        return templates.TemplateResponse(request, "contact_check.html", context, headers=NO_STORE)


@router.get("/{channel_id}/friends")
def contact_check_friends(request: Request, channel_id: int):
    with _session(request) as db:
        contacts = friend_check.list_check_contacts(db, _channel(db, channel_id))
        return JSONResponse({"contacts": contacts, "total": len(contacts)}, headers=NO_STORE)


@router.get("/{channel_id}/state")
def contact_check_state(request: Request, channel_id: int):
    with _session(request) as db:
        state = friend_check_runs.get_state(db, _channel(db, channel_id))
        return JSONResponse(state, headers=NO_STORE)


@router.post("/{channel_id}/run")
def contact_check_run(request: Request, channel_id: int, body: dict[str, Any] = Body(default={})):
    with _session(request, edit=True) as db:
        generation = _generation(body)
        interval = body.get("interval_seconds", 1)
        if type(interval) not in (int, float) or not 0 <= interval <= 60:
            raise HTTPException(status_code=400, detail="检测间隔必须为 0 到 60 秒之间的数字")
        state = friend_check_runs.start_run(
            db, _channel(db, channel_id), generation=generation, interval_seconds=float(interval),
        )
        return JSONResponse(state, headers=NO_STORE)


@router.post("/{channel_id}/pause")
def contact_check_pause(request: Request, channel_id: int, body: dict[str, Any] = Body(default={})):
    with _session(request, edit=True) as db:
        state = friend_check_runs.pause_run(db, _channel(db, channel_id), generation=_generation(body))
        return JSONResponse(state, headers=NO_STORE)


@router.post("/{channel_id}/clear")
def contact_check_clear(request: Request, channel_id: int, body: dict[str, Any] = Body(default={})):
    with _session(request, edit=True) as db:
        if body.get("confirmed") is not True:
            raise HTTPException(status_code=400, detail="请先确认清空检测结果")
        state = friend_check_runs.clear_run(
            db, _channel(db, channel_id), generation=_generation(body), confirmed=True,
        )
        return JSONResponse(state, headers=NO_STORE)


@router.post("/{channel_id}/friends/{contact_id}")
def contact_check_start(request: Request, channel_id: int, contact_id: int, body: dict[str, Any] = Body(default={})):
    with _session(request, edit=True) as db:
        scan = body.get("scan", False)
        if type(scan) is not bool:
            raise HTTPException(status_code=400, detail="检测类型格式不正确")
        result = friend_check_runs.start_item(
            db, _channel(db, channel_id), contact_id, generation=_generation(body), scan=scan,
        )
        return JSONResponse(result, headers=NO_STORE)


@router.post("/{channel_id}/jobs/{job_id}/poll")
def contact_check_poll(request: Request, channel_id: int, job_id: int, body: dict[str, Any] = Body(default={})):
    with _session(request, edit=True) as db:
        # 轮询可能结束超时任务并保存结果，沿用带 CSRF 的 POST。
        result = friend_check_runs.poll_item(
            db, _channel(db, channel_id), job_id, generation=_generation(body),
        )
        return JSONResponse(result, headers=NO_STORE)


@router.post("/{channel_id}/friends/{contact_id}/remark")
@router.post("/{channel_id}/friends/{contact_id}/delete")
def contact_check_action_start(request: Request, channel_id: int, contact_id: int, body: dict[str, Any] = Body(default={})):
    with _session(request, edit=True) as db:
        action = "remark" if request.url.path.endswith("/remark") else "delete"
        suffix = body.get("suffix", "- 单删")
        if action == "delete" and body.get("confirmed") is not True:
            raise HTTPException(status_code=400, detail="请先确认删除好友")
        if action == "remark" and not isinstance(suffix, str):
            raise HTTPException(status_code=400, detail="备注后缀格式不正确")
        result = friend_check_runs.start_action(
            db, _channel(db, channel_id), contact_id, generation=_generation(body), action=action, suffix=suffix,
        )
        return JSONResponse(result, headers=NO_STORE)


@router.post("/{channel_id}/actions/{job_id}/poll")
def contact_check_action_poll(request: Request, channel_id: int, job_id: int, body: dict[str, Any] = Body(default={})):
    with _session(request, edit=True) as db:
        result = friend_check_runs.poll_action(
            db, _channel(db, channel_id), job_id, generation=_generation(body),
        )
        return JSONResponse(result, headers=NO_STORE)
