"""群级触发回复的管理页面:规则列表、新建/编辑(含素材上传)、启停、删除。

页面面向普通用户:每条规则展示成「当有人拍机器人时 → 回复:文字、图片」这样的一句话,
新建时先选「什么时候」再加「回复什么」,表单字段全部由 trigger_rules.catalog 的注册信息生成。
"""

from __future__ import annotations

from typing import Any
from urllib.parse import quote

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse
from fastapi.templating import Jinja2Templates
from starlette.datastructures import FormData, UploadFile

from app.core.db import TenantSessionLocal
from app.core.tenant_db import open_tenant_session
from app.core.time import format_shanghai_time
from app.models.entities import Channel, Chatroom
from app.services.group_features import store as group_feature_store
from app.services.ops import record_audit
from app.services.saas_auth import has_tenant_role
from app.services.trigger_rules import engine, media
from app.services.trigger_rules.catalog import all_actions, all_triggers, get_action, get_trigger, variables_of
from app.web.auth_common import TENANT_CSRF_COOKIE, TENANT_LOGOUT_PATH, TENANT_PASSWORD_PATH, require_tenant_csrf
from app.web.tenant_navigation import build_tenant_navigation

router = APIRouter(prefix="/channels/{channel_id}/chatrooms/{chatroom_id}/triggers", tags=["group-triggers"])
templates = Jinja2Templates(directory="app/templates")


def _principal(request: Request, *, edit: bool):
    """查看需要登录;新建、修改、删除需要运营及以上角色。"""

    principal = getattr(request.state, "current_user", None)
    if principal is None:
        raise HTTPException(status_code=401, detail="Authentication required")
    if edit and not has_tenant_role(principal, "operator"):
        raise HTTPException(status_code=403, detail="Permission denied")
    return principal


def _load_group(db, channel_id: int, chatroom_id: int) -> tuple[Channel, Chatroom]:
    channel = db.get(Channel, channel_id)
    chatroom = db.get(Chatroom, chatroom_id)
    if channel is None or channel.deleted_at is not None or chatroom is None or chatroom.channel_id != channel.id:
        raise HTTPException(status_code=404, detail="Group not found")
    return channel, chatroom


def _base_url(channel_id: int, chatroom_id: int) -> str:
    return f"/channels/{channel_id}/chatrooms/{chatroom_id}"


def _context(request: Request, channel: Channel, chatroom: Chatroom, *, title: str, **extra) -> dict[str, Any]:
    notice = request.query_params.get("notice", "")
    context: dict[str, Any] = {
        "request": request,
        "page_title": title,
        "current_user": _principal(request, edit=False),
        "module_links": {},
        "notice": notice,
        "notice_tone": request.query_params.get("tone", "") or ("danger" if notice and "已" not in notice else "info"),
        "csrf_token": request.cookies.get(TENANT_CSRF_COOKIE, ""),
        "password_action": TENANT_PASSWORD_PATH,
        "logout_action": TENANT_LOGOUT_PATH,
        "format_time": format_shanghai_time,
        "channel": channel,
        "chatroom": chatroom,
        "group_url": _base_url(channel.id, chatroom.id),
        "triggers_url": _base_url(channel.id, chatroom.id) + "/triggers",
        "can_edit": has_tenant_role(_principal(request, edit=False), "operator"),
    }
    context.update(build_tenant_navigation(f"/channels/{channel.id}"))
    context.update(extra)
    return context


def _redirect(path: str, notice: str, *, danger: bool = False) -> RedirectResponse:
    return RedirectResponse(url=f"{path}?notice={quote(notice)}" + ("&tone=danger" if danger else ""), status_code=303)


# ---------------------------------------------------------------- 表单 ↔ 草稿

def _draft_view(rule=None) -> dict[str, Any]:
    """表单渲染用的规则快照:新建时是默认值,编辑时来自已保存的规则。"""

    if rule is None:
        first = all_triggers()[0]
        return {"name": "", "trigger_type": first.key, "trigger_params": {}, "actions": [{"type": all_actions()[0].key}], "pick_mode": "all", "cooldown_seconds": 0, "is_enabled": True}
    return {
        "name": rule.name,
        "trigger_type": rule.trigger_type,
        "trigger_params": engine.rule_params(rule),
        "actions": engine.rule_actions(rule),
        "pick_mode": rule.pick_mode,
        "cooldown_seconds": int(rule.cooldown_seconds or 0),
        "is_enabled": bool(rule.is_enabled),
    }


async def _read_form(form: FormData, *, tenant_id: int) -> tuple[engine.RuleDraft, list[str]]:
    """把提交的表单变成规则草稿,同时保存新上传的素材;返回 (草稿, 本次新存的素材文件名)。

    表单字段命名:触发参数 t__<触发类型>__<字段>,动作 a<序号>__<动作类型>__<字段>,
    文件字段另有 ..__keep(沿用已保存的文件)和 ..__remove(勾选则去掉)。只读取被选中的类型,其他类型的字段忽略。
    """

    stored_now: list[str] = []

    async def read_file(name: str, kind: str, label: str) -> str:
        upload = form.get(name)
        keep = str(form.get(name + "__keep") or "").strip()
        if isinstance(upload, UploadFile) and upload.filename:
            data = await upload.read(media.MEDIA_KINDS[kind][1] + 1)
            try:
                stored = media.save_upload(tenant_id, kind, upload.filename, data)
            except media.MediaError as exc:
                raise engine.RuleError(f"{label}:{exc}") from exc
            stored_now.append(stored)
            return stored
        return "" if form.get(name + "__remove") else keep

    trigger_type = str(form.get("trigger_type") or "")
    trigger = get_trigger(trigger_type)
    params: dict[str, Any] = {}
    if trigger is not None:
        for item in trigger.fields:
            params[item.name] = str(form.get(f"t__{trigger.key}__{item.name}") or "").strip()
    actions: list[dict[str, Any]] = []
    try:
        for index in form.getlist("action_idx"):
            action = get_action(str(form.get(f"a{index}__type") or ""))
            if action is None:
                actions.append({"type": ""})
                continue
            entry: dict[str, Any] = {"type": action.key}
            for item in action.fields:
                name = f"a{index}__{action.key}__{item.name}"
                if item.kind == "file":
                    entry[item.name] = await read_file(name, item.media_kind, f"第 {len(actions) + 1} 条回复的「{item.label}」")
                else:
                    entry[item.name] = str(form.get(name) or "").strip()
            actions.append(entry)
    except engine.RuleError:
        for name in stored_now:
            media.delete(tenant_id, name)
        raise
    try:
        cooldown = int(str(form.get("cooldown_seconds") or "0").strip() or 0)
    except ValueError:
        cooldown = 0
    draft = engine.RuleDraft(
        name=str(form.get("name") or ""),
        trigger_type=trigger_type,
        trigger_params=params,
        actions=actions,
        pick_mode=str(form.get("pick_mode") or "all"),
        cooldown_seconds=cooldown,
        is_enabled="is_enabled" in form,
    )
    return draft, stored_now


def _draft_to_view(draft: engine.RuleDraft) -> dict[str, Any]:
    return {
        "name": draft.name,
        "trigger_type": draft.trigger_type or all_triggers()[0].key,
        "trigger_params": draft.trigger_params,
        "actions": draft.actions or [{"type": all_actions()[0].key}],
        "pick_mode": draft.pick_mode,
        "cooldown_seconds": draft.cooldown_seconds,
        "is_enabled": draft.is_enabled,
    }


# ---------------------------------------------------------------- 页面

@router.get("", response_class=HTMLResponse)
async def trigger_rules_page(request: Request, channel_id: int, chatroom_id: int):
    _principal(request, edit=False)
    db = open_tenant_session(TenantSessionLocal)
    try:
        channel, chatroom = _load_group(db, channel_id, chatroom_id)
        rules = [engine.rule_summary(rule) for rule in engine.list_rules(db, chatroom.id)]
        context = _context(
            request, channel, chatroom, title=f"触发回复 · {chatroom.name or '群聊'}", rules=rules,
            feature_on=group_feature_store.is_enabled(chatroom, "trigger_reply"),
        )
        return templates.TemplateResponse(request, "group_triggers.html", context)
    finally:
        db.close()


def _form_response(request: Request, channel: Channel, chatroom: Chatroom, *, rule_id: int | None, draft: dict[str, Any], error: str = "") -> HTMLResponse:
    context = _context(
        request, channel, chatroom,
        title="新建触发规则" if rule_id is None else "编辑触发规则",
        rule_id=rule_id, draft=draft, form_error=error, triggers=all_triggers(), actions=all_actions(), pick_modes=engine.PICK_MODES,
        trigger_vars={item.key: variables_of(item) for item in all_triggers()},
    )
    return templates.TemplateResponse(request, "group_trigger_form.html", context, status_code=422 if error else 200)


@router.get("/new", response_class=HTMLResponse)
async def trigger_rule_new_page(request: Request, channel_id: int, chatroom_id: int):
    _principal(request, edit=True)
    db = open_tenant_session(TenantSessionLocal)
    try:
        channel, chatroom = _load_group(db, channel_id, chatroom_id)
        return _form_response(request, channel, chatroom, rule_id=None, draft=_draft_view())
    finally:
        db.close()


@router.get("/{rule_id}", response_class=HTMLResponse)
async def trigger_rule_edit_page(request: Request, channel_id: int, chatroom_id: int, rule_id: int):
    _principal(request, edit=True)
    db = open_tenant_session(TenantSessionLocal)
    try:
        channel, chatroom = _load_group(db, channel_id, chatroom_id)
        rule = engine.get_rule(db, chatroom.id, rule_id)
        if rule is None:
            raise HTTPException(status_code=404, detail="Rule not found")
        return _form_response(request, channel, chatroom, rule_id=rule.id, draft=_draft_view(rule))
    finally:
        db.close()


# ---------------------------------------------------------------- 动作

async def _save(request: Request, channel_id: int, chatroom_id: int, rule_id: int | None):
    principal = _principal(request, edit=True)
    form = await request.form()
    db = open_tenant_session(TenantSessionLocal)
    stored: list[str] = []
    draft = None
    try:
        require_tenant_csrf(db, request, str(form.get("csrf_token") or ""))
        channel, chatroom = _load_group(db, channel_id, chatroom_id)
        rule = None
        if rule_id is not None:
            rule = engine.get_rule(db, chatroom.id, rule_id)
            if rule is None:
                raise HTTPException(status_code=404, detail="Rule not found")
        try:
            draft, stored = await _read_form(form, tenant_id=int(principal.tenant_id))
            if rule is None:
                rule = engine.create_rule(db, tenant_id=int(principal.tenant_id), chatroom_id=chatroom.id, draft=draft)
                action = "create"
            else:
                engine.update_rule(db, rule, draft)
                action = "update"
            db.commit()
        except engine.RuleError as exc:
            db.rollback()
            for name in stored:
                media.delete(int(principal.tenant_id), name)
            view = _draft_to_view(draft) if draft is not None else _draft_view(rule)
            return _form_response(request, channel, chatroom, rule_id=rule_id, draft=view, error=str(exc))
        record_audit(db, f"group-trigger.{action}", "group_trigger_rule", rule.id, f"chatroom={chatroom.id};trigger={rule.trigger_type}")
        return _redirect(_base_url(channel.id, chatroom.id) + "/triggers", "规则已保存")
    finally:
        db.close()


@router.post("")
async def trigger_rule_create_action(request: Request, channel_id: int, chatroom_id: int):
    return await _save(request, channel_id, chatroom_id, None)


@router.post("/{rule_id}")
async def trigger_rule_update_action(request: Request, channel_id: int, chatroom_id: int, rule_id: int):
    return await _save(request, channel_id, chatroom_id, rule_id)


@router.post("/{rule_id}/delete")
async def trigger_rule_delete_action(request: Request, channel_id: int, chatroom_id: int, rule_id: int):
    _principal(request, edit=True)
    form = await request.form()
    db = open_tenant_session(TenantSessionLocal)
    try:
        require_tenant_csrf(db, request, str(form.get("csrf_token") or ""))
        channel, chatroom = _load_group(db, channel_id, chatroom_id)
        rule = engine.get_rule(db, chatroom.id, rule_id)
        if rule is None:
            raise HTTPException(status_code=404, detail="Rule not found")
        engine.delete_rule(db, rule)
        db.commit()
        record_audit(db, "group-trigger.delete", "group_trigger_rule", rule_id, f"chatroom={chatroom.id}")
        return _redirect(_base_url(channel.id, chatroom.id) + "/triggers", "规则已删除")
    finally:
        db.close()


@router.post("/{rule_id}/toggle")
async def trigger_rule_toggle_action(request: Request, channel_id: int, chatroom_id: int, rule_id: int):
    """列表里的开关:点击即保存,请求头带 CSRF 令牌,返回 JSON。"""

    _principal(request, edit=True)
    try:
        body = await request.json()
    except ValueError:
        return JSONResponse({"ok": False, "notice": "请求格式不正确"}, status_code=400)
    value = (body or {}).get("value")
    if not isinstance(value, bool):
        return JSONResponse({"ok": False, "notice": "请求格式不正确"}, status_code=400)
    db = open_tenant_session(TenantSessionLocal)
    try:
        require_tenant_csrf(db, request, request.headers.get("X-CSRF-Token", ""))
        _channel, chatroom = _load_group(db, channel_id, chatroom_id)
        rule = engine.get_rule(db, chatroom.id, rule_id)
        if rule is None:
            raise HTTPException(status_code=404, detail="Rule not found")
        rule.is_enabled = value
        db.commit()
        record_audit(db, "group-trigger.toggle", "group_trigger_rule", rule.id, f"enabled={value}")
        return JSONResponse({"ok": True, "notice": f"规则「{rule.name}」已{'启用' if value else '停用'}"})
    finally:
        db.close()
