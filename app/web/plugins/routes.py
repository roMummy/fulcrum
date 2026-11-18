"""插件目录、详情、绑定配置和手动执行入口。"""

from typing import Mapping
from urllib.parse import quote
import time as time_module

from fastapi import APIRouter, Form, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.core.tenant_context import require_tenant_id
from app.models.entities import Bot, BotPlugin, Conversation, Plugin
from app.schemas.forms import BotPluginBindCreate
from app.services.crud import (
    bind_plugin,
    get_binding,
    get_bot,
    toggle_binding,
    unbind_plugin,
    update_binding_config,
)
from app.services.ops import record_audit
from app.services.plugin_directory import sync_plugins_from_directory
from app.services.plugin_governance import blocked_plugin_codes, tenant_visible_plugins
from app.services.plugin_runtime import (
    PluginExecutionContext,
    execute_binding_message_action,
    get_plugin_runtime_meta,
    get_plugin_runtime_meta_map,
)
from app.services.plugin_runtime.proxy import PluginProxyConfigError, parse_proxy_url
from app.services.plugin_schemas import build_plugin_config_view, merge_schema_config
from app.services.plugin_secrets import decrypt_plugin_secret_config
from app.services.runtime import compute_binding_runtime, persist_binding_execution, reload_binding
from app.web.auth_common import require_tenant_csrf
from app.web.plugins.views import (
    build_binding_schema_cards_with_dispatch,
    build_plugin_filter_options,
    build_plugin_runtime_summary,
    plugin_matches_filters,
)
from app.web.shared.access import enforce_role, get_session, require_user
from app.web.shared.operations import (
    _completed_operation_response,
    _csrf_from_header,
    _require_operation_csrf,
)
from app.web.shared.presentation import render
from app.web.shared.queries import build_bot_name_map


router = APIRouter()


@router.get("/plugins", response_class=HTMLResponse)
async def plugins_page(request: Request):
    """插件目录用功能卡片展示名称、版本和完整介绍；按机器人启停、绑定和配置由详情页承接。"""

    db = get_session()
    try:
        sync_plugins_from_directory(db)
        plugins = tenant_visible_plugins(db, _current_tenant_id())
        plugin_runtime_map = get_plugin_runtime_meta_map(plugin.code for plugin in plugins)
        search_query = request.query_params.get("q", "").strip()
        plugin_type_filter = request.query_params.get("plugin_type", "").strip()
        category_filter = request.query_params.get("category", "").strip()
        migration_filter = request.query_params.get("migration_state", "").strip()
        execution_filter = request.query_params.get("execution_state", "").strip()
        manifest_filter = request.query_params.get("manifest_state", "").strip()
        filtered_plugins = [
            plugin
            for plugin in plugins
            if plugin_matches_filters(
                plugin,
                plugin_runtime_map.get(plugin.code, {}),
                keyword=search_query,
                plugin_type=plugin_type_filter,
                category=category_filter,
                migration_state=migration_filter,
                execution_state=execution_filter,
                manifest_state=manifest_filter,
            )
        ]
        return render(
            request,
            "plugins.html",
            page_title="插件",
            plugins=filtered_plugins,
            plugin_runtime_map=plugin_runtime_map,
            plugin_filters={
                "q": search_query,
                "plugin_type": plugin_type_filter,
                "category": category_filter,
                "migration_state": migration_filter,
                "execution_state": execution_filter,
                "manifest_state": manifest_filter,
            },
            plugin_filter_options=build_plugin_filter_options(plugins, plugin_runtime_map),
            filtered_plugin_count=len(filtered_plugins),
            total_plugin_count=len(plugins),
            notice=request.query_params.get("notice", ""),
        )
    finally:
        db.close()


@router.get('/plugins/usage')
async def plugin_usage_data(request: Request, period: str = '24h'):
    """企业卡片一次读取全部可见插件，租户身份只能来自认证会话。"""

    from app.services.plugin_usage import build_plugin_usage

    require_user(request)
    db = get_session()
    try:
        plugins = tenant_visible_plugins(db, _current_tenant_id())
        return JSONResponse(build_plugin_usage(db, (plugin.code for plugin in plugins), period=period),
            headers={'Cache-Control': 'no-store'})
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    finally:
        db.close()


@router.post("/plugins/refresh")
async def refresh_plugins_action(request: Request):
    enforce_role(request, "ops")
    db = get_session()
    try:
        await _require_operation_csrf(db, request)
        rows = sync_plugins_from_directory(db, force=True)
        record_audit(db, "plugin.refresh-directory", "plugin", None, f"count={len(rows)}")
        return _completed_operation_response(request, f"已刷新插件目录，共发现 {len(rows)} 个插件", "/plugins")
    finally:
        db.close()


@router.get("/plugins/runtime", response_class=HTMLResponse)
async def plugin_runtime_page(request: Request):
    db = get_session()
    try:
        sync_plugins_from_directory(db)
        plugins = tenant_visible_plugins(db, _current_tenant_id())
        visible_plugin_ids = {plugin.id for plugin in plugins}
        bindings = [
            item
            for item in db.scalars(select(BotPlugin).where(BotPlugin.status != "unbound", BotPlugin.bot_id.in_(select(Bot.id).where(Bot.deleted_at.is_(None)))).order_by(BotPlugin.id.desc())).all()
            if item.plugin_id in visible_plugin_ids
        ]
        bots = db.scalars(select(Bot).where(Bot.deleted_at.is_(None)).order_by(Bot.id.desc())).all()
        bot_name_map = build_bot_name_map(bots)
        plugin_runtime_map = get_plugin_runtime_meta_map(plugin.code for plugin in plugins)
        for binding in bindings:
            binding.runtime_status = compute_binding_runtime(binding)
        return render(
            request,
            "plugin_runtime.html",
            page_title="插件运行状态",
            bindings=bindings,
            binding_cards=build_binding_schema_cards_with_dispatch(db, bindings),
            bot_name_map=bot_name_map,
            plugins=plugins,
            plugin_runtime_map=plugin_runtime_map,
            runtime_summary=build_plugin_runtime_summary(plugins, bindings, runtime_map=plugin_runtime_map),
            notice=request.query_params.get("notice", ""),
        )
    finally:
        db.close()


@router.get("/plugins/{plugin_id}", response_class=HTMLResponse)
async def plugin_detail_page(request: Request, plugin_id: int):
    db = get_session()
    try:
        sync_plugins_from_directory(db)
        plugin = _get_tenant_usable_plugin(db, plugin_id)
        bindings = db.scalars(select(BotPlugin).where(BotPlugin.status != "unbound", BotPlugin.plugin_id == plugin_id, BotPlugin.bot_id.in_(select(Bot.id).where(Bot.deleted_at.is_(None)))).order_by(BotPlugin.id.desc())).all()
        bots = db.scalars(select(Bot).where(Bot.deleted_at.is_(None)).order_by(Bot.id.desc())).all()
        bot_name_map = build_bot_name_map(bots)
        plugin_runtime_info = get_plugin_runtime_meta(plugin.code)
        for binding in bindings:
            binding.runtime_status = compute_binding_runtime(binding)
        return render(
            request,
            "plugin_detail.html",
            page_title=plugin.display_name,
            plugin=plugin,
            bindings=bindings,
            binding_cards=build_binding_schema_cards_with_dispatch(db, bindings),
            plugin_config_view=build_plugin_config_view(plugin.code, None),
            plugin_runtime_info=plugin_runtime_info,
            bots=bots,
            bindable_bots=[bot for bot in bots if bot.id not in {binding.bot_id for binding in bindings}],
            bot_name_map=bot_name_map,
            runtime_summary=build_plugin_runtime_summary([plugin], bindings, runtime_map={plugin.code: plugin_runtime_info}),
            notice=request.query_params.get("notice", ""),
        )
    finally:
        db.close()


def _current_tenant_id() -> int:
    """当前请求所属租户(租户页面里的插件可见性都按它判断)。"""

    return int(require_tenant_id())


def _get_tenant_usable_plugin(db, plugin_id: int) -> Plugin:
    """取一个当前租户可以使用的插件;不存在、目录里已缺失、被平台停用或对本租户不可见都按 404 处理。"""

    plugin = db.get(Plugin, plugin_id)
    if not plugin or plugin.missing_at is not None or plugin.code in blocked_plugin_codes(db, _current_tenant_id()):
        raise HTTPException(status_code=404, detail="Plugin not found")
    return plugin


def _ensure_binding_plugin_usable(db, binding) -> None:
    """启用或手动执行绑定前确认插件没有被平台停用:绑定数据保留,但此时不能让它跑起来。"""

    if binding.plugin is not None and binding.plugin.code in blocked_plugin_codes(db, _current_tenant_id()):
        raise HTTPException(status_code=403, detail="该插件已被平台停用或对当前租户不可用")


def _safe_plugin_next(value: str, default: str) -> str:
    """表单里的"保存后跳回哪里":只允许本站的 /plugins 路径(防开放重定向),否则用默认页。"""

    candidate = str(value or "").strip()
    if candidate.startswith("/plugins") and "//" not in candidate and "\\" not in candidate and "?" not in candidate and "\n" not in candidate:
        return candidate
    return default


@router.post("/plugins/bind")
async def bind_plugin_action(
    request: Request,
    bot_id: int = Form(...),
    plugin_id: int = Form(...),
    status: str = Form("enabled"),
    config_json: str = Form(""),
    group_default_enabled: bool = Form(False),
    csrf_token: str = Form(""),
):
    enforce_role(request, "ops")
    db = get_session()
    try:
        require_tenant_csrf(db, request, csrf_token or _csrf_from_header(request))
        plugin = _get_tenant_usable_plugin(db, plugin_id)
        if get_bot(db, bot_id) is None:
            raise HTTPException(status_code=404, detail="机器人不存在")
        form = await request.form()
        try:
            merged_config = merge_schema_config(
                plugin.code,
                form,
                fallback_config_json=config_json or None,
            )
        except ValueError as exc:
            # 公共发送策略必须在创建绑定之前完整校验，不把无效区间存成半成品。
            return JSONResponse({"ok": False, "notice": str(exc)}, status_code=400)
        payload = BotPluginBindCreate(
            bot_id=bot_id,
            plugin_id=plugin_id,
            status=status,
            config_json=merged_config.config_json,
            group_default_enabled=group_default_enabled,
        )
        binding = bind_plugin(
            db,
            payload,
            secret_config_patch=merged_config.secret_patch,
            clear_secret_fields=merged_config.clear_secret_fields,
        )
        record_audit(db, "plugin.bind", "bot_plugin", binding.id, f"bot={binding.bot_id}, plugin={binding.plugin_id}")
        return RedirectResponse(url=f"/plugins/{plugin_id}?notice=" + quote("插件绑定已保存"), status_code=303)
    finally:
        db.close()


@router.post("/plugins/{binding_id}/unbind")
async def unbind_plugin_action(request: Request, binding_id: int, csrf_token: str = Form("")):
    enforce_role(request, "ops")
    db = get_session()
    try:
        require_tenant_csrf(db, request, csrf_token or _csrf_from_header(request))
        binding = get_binding(db, binding_id)
        if binding is None:
            raise HTTPException(status_code=404, detail="插件绑定不存在")
        plugin_id = binding.plugin_id
        unbind_plugin(db, binding)
        record_audit(db, "plugin.unbind", "bot_plugin", binding_id, f"bot={binding.bot_id}, plugin={plugin_id}")
        return RedirectResponse(url=f"/plugins/{plugin_id}?notice=" + quote("插件已从机器人解绑，历史数据和配置已保留"), status_code=303)
    finally:
        db.close()


@router.post("/plugins/{binding_id}/toggle")
async def toggle_plugin_binding_action(request: Request, binding_id: int, enabled: bool = Form(...), csrf_token: str = Form("")):
    """插件运行页里某个绑定的启用/停用开关:点击即保存,提交目标状态(幂等),返回 JSON,由页面脚本弹出提示。"""

    enforce_role(request, "ops")
    db = get_session()
    try:
        require_tenant_csrf(db, request, csrf_token or _csrf_from_header(request))
        binding = get_binding(db, binding_id)
        if not binding:
            raise HTTPException(status_code=404, detail="Binding not found")
        if enabled:
            _ensure_binding_plugin_usable(db, binding)
        if (binding.status == "enabled") != enabled:
            toggle_binding(db, binding)
            record_audit(db, "plugin.toggle", "bot_plugin", binding.id, f"status={binding.status}")
        return JSONResponse({"ok": True, "notice": f"插件绑定已{'启用' if binding.status == 'enabled' else '停用'}", "is_enabled": binding.status == "enabled"})
    finally:
        db.close()


@router.post("/plugins/toggle-direct")
async def toggle_plugin_direct_action(
    request: Request,
    bot_id: int = Form(...),
    plugin_id: int = Form(...),
    enabled: bool = Form(...),
    group_default_enabled: bool = Form(False),
    csrf_token: str = Form(""),
):
    """插件页里按机器人启停插件的开关:点击即保存,提交目标状态(幂等),返回 JSON 供页面脚本更新状态标签并弹出提示。

    还没绑定过的插件第一次打开时会自动创建绑定并启用;关闭一个没绑定过的插件什么也不用做。
    """

    enforce_role(request, "ops")
    db = get_session()
    try:
        require_tenant_csrf(db, request, csrf_token or _csrf_from_header(request))
        plugin = db.get(Plugin, plugin_id)
        bot = get_bot(db, bot_id)
        if not plugin or not bot:
            raise HTTPException(status_code=404, detail="Bot or plugin not found")
        if enabled and plugin.code in blocked_plugin_codes(db, _current_tenant_id()):
            raise HTTPException(status_code=403, detail="该插件已被平台停用或对当前租户不可用")
        binding = db.scalar(select(BotPlugin).where(BotPlugin.bot_id == bot_id, BotPlugin.plugin_id == plugin_id))
        if binding and binding.status != "unbound":
            if (binding.status == "enabled") != enabled:
                toggle_binding(db, binding)
                record_audit(db, "plugin.toggle-direct", "bot_plugin", binding.id, f"status={binding.status}")
        elif enabled:
            binding = bind_plugin(db, BotPluginBindCreate(bot_id=bot_id, plugin_id=plugin_id, status="enabled", config_json=binding.config_json if binding else None, group_default_enabled=group_default_enabled))
            record_audit(db, "plugin.toggle-direct", "bot_plugin", binding.id, "created=enabled")
        elif binding and binding.status == "unbound":
            binding = None
        is_enabled = bool(binding and str(binding.status or "").strip() == "enabled")
        enabled_count = db.scalar(select(func.count(BotPlugin.id)).where(BotPlugin.bot_id == bot_id, BotPlugin.status == "enabled")) or 0
        total_count = db.scalar(select(func.count(Plugin.id))) or 0
        return JSONResponse(
            {
                "ok": True,
                "notice": f"{bot.name} 的 {plugin.display_name} 已{'启用' if is_enabled else '停用'}",
                "bot_id": bot_id,
                "plugin_id": plugin_id,
                "binding_id": binding.id if binding else None,
                "is_bound": binding is not None,
                "is_enabled": is_enabled,
                "status_label": "已启用" if is_enabled else "已停用",
                "status_tone": "success" if is_enabled else "muted",
                "enabled_count": int(enabled_count),
                "total_count": int(total_count),
            }
        )
    finally:
        db.close()


@router.post("/plugins/{binding_id}/edit")
async def edit_plugin_binding_action(
    request: Request,
    binding_id: int,
    status: str | None = Form(None),
    config_json: str = Form(""),
    csrf_token: str = Form(""),
    next: str = Form(""),
):
    """保存绑定配置。启用/停用由单独的开关负责,表单里没带 status 时保持原状态;
    页面脚本用 fetch 提交时返回 JSON(弹出提示),普通表单提交则跳回 next(只允许本站 /plugins 路径)。"""

    enforce_role(request, "ops")
    db = get_session()
    try:
        require_tenant_csrf(db, request, csrf_token or _csrf_from_header(request))
        binding = get_binding(db, binding_id)
        if not binding:
            raise HTTPException(status_code=404, detail="Binding not found")
        form = await request.form()
        plugin_code = binding.plugin.code if binding.plugin else None
        secret_config = decrypt_plugin_secret_config(
            tenant_id=int(binding.tenant_id),
            binding_id=int(binding.id),
            encrypted=binding.secret_config_encrypted,
        )
        existing_proxy_password = str(secret_config.get("proxy_password") or "")
        if not existing_proxy_password:
            legacy_proxy_url = str(secret_config.get("proxy_url") or "").strip()
            if legacy_proxy_url:
                try:
                    legacy_endpoint = parse_proxy_url(legacy_proxy_url)
                except PluginProxyConfigError:
                    legacy_endpoint = None
                existing_proxy_password = legacy_endpoint.password if legacy_endpoint else ""
        try:
            merged_config = merge_schema_config(
                plugin_code,
                form,
                existing_config_json=binding.config_json,
                fallback_config_json=config_json or None,
                existing_proxy_password=existing_proxy_password,
            )
        except ValueError as exc:
            # 与本群设置返回相同的可读错误，fetch 保存失败时保留用户输入继续修改。
            return JSONResponse({"ok": False, "notice": str(exc)}, status_code=400)
        update_binding_config(
            db,
            binding,
            status=status or binding.status,
            config_json=merged_config.config_json,
            secret_config_patch=merged_config.secret_patch,
            clear_secret_fields=merged_config.clear_secret_fields,
        )
        record_audit(db, "plugin.update", "bot_plugin", binding.id, f"status={binding.status}")
        notice = "插件绑定配置已保存"
        if request.headers.get("x-requested-with") == "fetch":
            return JSONResponse({"ok": True, "notice": notice})
        return RedirectResponse(url=f"{_safe_plugin_next(next, '/plugins/runtime')}?notice={quote(notice)}", status_code=303)
    finally:
        db.close()


@router.post("/plugins/{binding_id}/reload")
async def reload_plugin_binding_action(request: Request, binding_id: int, csrf_token: str = Form(""), next: str = Form("")):
    enforce_role(request, "ops")
    db = get_session()
    try:
        require_tenant_csrf(db, request, csrf_token or _csrf_from_header(request))
        binding = get_binding(db, binding_id)
        if not binding:
            raise HTTPException(status_code=404, detail="Binding not found")
        status, message = reload_binding(db, binding)
        record_audit(db, "plugin.reload", "bot_plugin", binding.id, message)
        return _completed_operation_response(request, message, _safe_plugin_next(next, "/plugins/runtime"), ok=status == "running")
    finally:
        db.close()


@router.post("/plugins/{binding_id}/execute")
async def execute_plugin_binding_action(request: Request, binding_id: int):
    enforce_role(request, "ops")
    db = get_session()
    try:
        require_tenant_csrf(db, request, _csrf_from_header(request))
        binding = get_binding(db, binding_id)
        if not binding:
            raise HTTPException(status_code=404, detail="Binding not found")
        _ensure_binding_plugin_usable(db, binding)
        payload = await request.json() if request.headers.get("content-type", "").startswith("application/json") else {}
        context = _build_manual_plugin_execution_context(db, binding, payload)
        from app.services.analytics import record_binding_execution_analytics

        started = time_module.perf_counter()
        try:
            result = execute_binding_message_action(db, binding, 'execute', payload=payload, context=context)
        except Exception as exc:
            # 回滚失败动作未提交的业务写入，再独立提交异常统计，避免请求结束时
            # 日志也跟着被回滚；底层消息桥接不记数，因此不会与分发调用重复。
            db.rollback()
            record_binding_execution_analytics(db, binding, context=context, error=exc,
                duration_ms=int((time_module.perf_counter() - started) * 1000), trigger_source='manual')
            db.commit()
            raise
        record_binding_execution_analytics(db, binding, context=context, result=result,
            duration_ms=int((time_module.perf_counter() - started) * 1000), trigger_source='manual')
        if result is None:
            db.commit()
            raise HTTPException(status_code=400, detail="Plugin execution not supported")
        persist_binding_execution(db, binding, result, action="execute")
        record_audit(db, "plugin.execute", "bot_plugin", binding.id, result.message)
        return JSONResponse(
            {
                "ok": result.ok,
                "binding_id": binding.id,
                "plugin_code": binding.plugin.code if binding.plugin else None,
                "status": result.status,
                "summary": result.message,
                "output": dict(result.output),
            }
        )
    finally:
        db.close()


def _build_manual_plugin_execution_context(
    db: Session,
    binding: BotPlugin,
    payload: Mapping[str, object],
) -> PluginExecutionContext | None:
    """把员工手工选择的会话校验为当前绑定资源，再构造可信插件上下文。"""

    raw_conversation_id = payload.get("conversation_id")
    if raw_conversation_id in (None, ""):
        return None
    try:
        conversation_id = int(raw_conversation_id)
    except (TypeError, ValueError) as exc:
        raise HTTPException(status_code=400, detail="会话编号不合法") from exc
    conversation = db.get(Conversation, conversation_id)
    if conversation is None or int(conversation.bot_id) != int(binding.bot_id):
        raise HTTPException(status_code=400, detail="会话不属于当前插件机器人")
    return PluginExecutionContext(
        tenant_id=int(binding.tenant_id),
        binding_id=int(binding.id),
        bot_id=int(binding.bot_id),
        channel_id=int(conversation.channel_id) if conversation.channel_id else None,
        conversation_id=int(conversation.id),
        channel_type=str(conversation.channel_type or "").strip() or None,
        chat_type=str(conversation.chat_type or "").strip() or None,
    )
