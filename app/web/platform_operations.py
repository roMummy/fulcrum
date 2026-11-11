"""SaaS 平台全局运维、告警和日志页面。"""

from __future__ import annotations

import subprocess
import time as time_module
from datetime import datetime
from pathlib import Path
from urllib.parse import quote, urlencode

from fastapi import APIRouter, File, Form, HTTPException, Request, UploadFile
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates

from app.core.db import SessionLocal
from app.core.settings import settings
from app.core.time import format_shanghai_time, now_shanghai
from app.models.entities import AlertRecord
from app.models.saas import PlatformAuditLog
from app.services.log_view import LEVEL_RANK, read_page
from app.services.ops import build_monitoring_snapshot
from app.services.request_metrics import summarize as summarize_request_metrics
from app.services.system_info import build_database_info, build_server_info
from app.services.login_proxy_pool import (
    LoginProxyPoolError,
    build_platform_proxy_summary,
    check_platform_proxy,
    import_platform_proxies_csv,
    list_platform_proxies,
    save_platform_proxy,
    set_platform_proxy_enabled,
)
from app.services.platform_protocol_services import (
    PlatformProtocolServiceError,
    list_platform_protocol_services,
    save_platform_protocol_service,
)
from app.web.auth_common import (
    PLATFORM_CSRF_COOKIE,
    PLATFORM_LOGOUT_PATH,
    PLATFORM_PASSWORD_PATH,
    require_platform_csrf,
)
from app.web.platform_navigation import (
    build_platform_operations_navigation,
    build_platform_primary_navigation,
)


router = APIRouter(prefix="/platform", tags=["platform-operations"])
templates = Jinja2Templates(directory="app/templates")
templates.env.filters["bj_time"] = format_shanghai_time
APP_ROOT = Path(__file__).resolve().parents[2]


def _require_platform_principal(request: Request):
    """只使用平台会话恢复的员工身份，不接受租户 Cookie 或表单角色。"""

    principal = getattr(request.state, "platform_principal", None)
    if principal is None:
        raise HTTPException(status_code=401, detail="Platform authentication required")
    return principal


def _platform_context(
    request: Request,
    *,
    page_title: str,
    active: str,
    notice: str = "",
    notice_tone: str = "",
    **extra,
) -> dict[str, object]:
    """构造平台运维导航和平台身份操作上下文。"""

    principal = _require_platform_principal(request)
    context: dict[str, object] = {
        "request": request,
        "page_title": page_title,
        "current_user": principal,
        "primary_nav_items": build_platform_primary_navigation("operations"),
        "secondary_nav_items": [],
        "secondary_nav_active": False,
        "section_nav_items": build_platform_operations_navigation(active),
        "module_links": {},
        "notice": notice,
        "notice_tone": notice_tone or ("danger" if notice and "成功" not in notice and "已" not in notice else "info"),
        "csrf_token": request.cookies.get(PLATFORM_CSRF_COOKIE, ""),
        "password_action": PLATFORM_PASSWORD_PATH,
        "logout_action": PLATFORM_LOGOUT_PATH,
    }
    context.update(extra)
    return context


def _redirect(path: str, notice: str, *, danger: bool = False) -> RedirectResponse:
    """生成不携带敏感参数的平台运维跳转。"""

    suffix = f"?notice={quote(notice)}"
    if danger:
        suffix += "&tone=danger"
    return RedirectResponse(url=path + suffix, status_code=303)


def _protocol_service_error_message(error: PlatformProtocolServiceError) -> str:
    """把协议服务内部错误转换为平台运维人员可直接处理的配置提示。"""

    messages = {
        "platform_protocol_service_url_invalid": "请输入有效的 HTTP 或 HTTPS 服务地址",
        "platform_protocol_service_type_not_managed": "该渠道不使用平台托管协议服务",
    }
    return messages.get(str(error), "协议服务配置保存失败")


def _proxy_pool_error_message(error: LoginProxyPoolError) -> str:
    """把代理池内部错误转换为不包含端点和凭证的平台操作提示。"""

    messages = {
        "proxy_not_found": "代理不存在或已被移除",
        "proxy_duplicate": "相同主机、端口和用户名的代理已存在",
        "proxy_name_required": "请输入代理名称",
        "proxy_province_required": "请选择代理所在省份",
        "proxy_city_required": "请选择代理所在城市",
        "proxy_host_required": "请输入代理主机",
        "proxy_port_invalid": "请输入 1 到 65535 之间的有效端口",
        "proxy_value_too_long": "代理信息长度超出限制",
        "proxy_csv_encoding_invalid": "CSV 文件必须使用 UTF-8 编码",
        "proxy_csv_headers_invalid": "CSV 表头必须为：名称、省、市、主机、端口、用户名、密码",
    }
    return messages.get(str(error), "代理池操作失败")


def _resolve_tab_log_paths(tab: str) -> list[Path]:
    """只枚举配置日志目录内当前类型的主文件和轮转文件。"""

    active_tab = "info" if str(tab or "").strip().lower() == "info" else "error"
    log_dir = Path(settings.log_dir)
    base_name = "app.log" if active_tab == "info" else "error.log"
    base_path = log_dir / base_name
    rotated_paths = sorted(log_dir.glob(f"{base_name}.*"), key=lambda path: path.name)
    return [path for path in [base_path, *rotated_paths] if path.exists()]


def _resolve_selected_log_path(tab: str, selected_file_name: str) -> Path:
    """从已枚举文件中选择日志，拒绝客户端提交任意文件系统路径。"""

    available_paths = _resolve_tab_log_paths(tab)
    if not available_paths:
        base_name = "app.log" if str(tab or "").strip().lower() == "info" else "error.log"
        return Path(settings.log_dir) / base_name
    if selected_file_name:
        for path in available_paths:
            if path.name == selected_file_name:
                return path
    return available_paths[0]


def _record_platform_operation(
    db,
    *,
    actor_user_id: int,
    action: str,
    target_type: str,
    target_id: int | None,
    target_tenant_id: int | None,
    detail: str,
) -> None:
    """记录平台运维动作，避免把平台员工写入租户审计身份。"""

    db.add(
        PlatformAuditLog(
            actor_user_id=actor_user_id,
            action=action,
            target_type=target_type,
            target_id=target_id,
            target_tenant_id=target_tenant_id,
            detail=detail,
        )
    )


@router.get("/operations", response_class=HTMLResponse)
async def platform_operations_page(request: Request):
    """展示全平台健康检查、未关闭告警和租户审计快照。"""

    _require_platform_principal(request)
    with SessionLocal() as db:
        context = _platform_context(
            request,
            page_title="系统运维",
            active="operations",
            notice=request.query_params.get("notice", ""),
            notice_tone=request.query_params.get("tone", ""),
            **build_monitoring_snapshot(db),
        )
        return templates.TemplateResponse(request, "platform_monitoring.html", context)


@router.get("/system", response_class=HTMLResponse)
async def platform_system_page(request: Request):
    """展示服务器配置、数据库容量与表条目数、接口响应耗时。"""

    _require_platform_principal(request)
    with SessionLocal() as db:
        context = _platform_context(
            request,
            page_title="服务器与数据库",
            active="system",
            server=build_server_info(),
            database=build_database_info(db),
            metrics=summarize_request_metrics(),
        )
        return templates.TemplateResponse(request, "platform_system.html", context)


@router.get("/protocol-services", response_class=HTMLResponse)
async def platform_protocol_services_page(request: Request):
    """在系统运维二级入口展示平台统一维护的协议服务配置。"""

    with SessionLocal() as db:
        context = _platform_context(
            request,
            page_title="协议服务配置",
            active="protocol_services",
            notice=request.query_params.get("notice", ""),
            notice_tone=request.query_params.get("tone", ""),
            protocol_services=list_platform_protocol_services(db),
        )
        return templates.TemplateResponse(request, "platform_protocol_services.html", context)


@router.post("/protocol-services/{channel_type}")
async def platform_protocol_service_save_action(
    channel_type: str,
    request: Request,
    api_base: str = Form(""),
    is_enabled: bool = Form(False),
    csrf_token: str = Form(""),
):
    """保存平台协议服务配置并同步已有渠道，不接受任何租户级覆盖参数。"""

    principal = _require_platform_principal(request)
    with SessionLocal() as db:
        require_platform_csrf(db, request, csrf_token)
        try:
            save_platform_protocol_service(
                db,
                channel_type=channel_type,
                api_base=api_base,
                is_enabled=is_enabled,
                actor_user_id=principal.user_id,
            )
        except PlatformProtocolServiceError as exc:
            return _redirect(
                "/platform/protocol-services",
                _protocol_service_error_message(exc),
                danger=True,
            )
    return _redirect("/platform/protocol-services", "协议服务配置已更新")


@router.get("/proxy-pool", response_class=HTMLResponse)
async def platform_proxy_pool_page(request: Request):
    """展示平台代理摘要、脱敏代理清单和受控维护入口。"""

    with SessionLocal() as db:
        context = _platform_context(
            request,
            page_title="代理池",
            active="proxy_pool",
            notice=request.query_params.get("notice", ""),
            notice_tone=request.query_params.get("tone", ""),
            proxy_summary=build_platform_proxy_summary(db),
            proxies=list_platform_proxies(db),
        )
        return templates.TemplateResponse(request, "platform_proxy_pool.html", context)


@router.post("/proxy-pool")
async def platform_proxy_save_action(
    request: Request,
    proxy_id: int = Form(0),
    name: str = Form(""),
    province: str = Form(""),
    city: str = Form(""),
    host: str = Form(""),
    port: str = Form(""),
    username: str = Form(""),
    password: str = Form(""),
    csrf_token: str = Form(""),
):
    """新增或编辑平台代理，审计只记录资源主键和地区。"""

    principal = _require_platform_principal(request)
    with SessionLocal() as db:
        require_platform_csrf(db, request, csrf_token)
        try:
            proxy = save_platform_proxy(
                db,
                proxy_id=int(proxy_id) or None,
                values={
                    "name": name,
                    "province": province,
                    "city": city,
                    "host": host,
                    "port": port,
                    "username": username,
                    "password": password,
                },
            )
        except LoginProxyPoolError as exc:
            return _redirect("/platform/proxy-pool", _proxy_pool_error_message(exc), danger=True)
        _record_platform_operation(
            db,
            actor_user_id=principal.user_id,
            action="platform.proxy.save",
            target_type="platform_proxy",
            target_id=int(proxy.id),
            target_tenant_id=None,
            detail=f"province={proxy.province};city={proxy.city};is_new={str(not bool(proxy_id)).lower()}",
        )
        db.commit()
    return _redirect("/platform/proxy-pool", "代理配置已保存")


@router.post("/proxy-pool/import")
async def platform_proxy_import_action(
    request: Request,
    csv_file: UploadFile = File(...),
    csrf_token: str = Form(""),
):
    """批量导入固定格式 CSV，上传文件和逐行凭证都不进入审计详情。"""

    principal = _require_platform_principal(request)
    with SessionLocal() as db:
        require_platform_csrf(db, request, csrf_token)
        try:
            result = import_platform_proxies_csv(db, content=await csv_file.read())
        except LoginProxyPoolError as exc:
            return _redirect("/platform/proxy-pool", _proxy_pool_error_message(exc), danger=True)
        _record_platform_operation(
            db,
            actor_user_id=principal.user_id,
            action="platform.proxy.import",
            target_type="platform_proxy",
            target_id=None,
            target_tenant_id=None,
            detail=(
                f"success={int(result['success'])};skipped={int(result['skipped'])};"
                f"failed={int(result['failed'])}"
            ),
        )
        db.commit()
    notice = f"导入完成：成功 {result['success']}，跳过 {result['skipped']}，失败 {result['failed']}"
    return _redirect("/platform/proxy-pool", notice, danger=bool(result["failed"]))


@router.post("/proxy-pool/{proxy_id}/toggle")
async def platform_proxy_toggle_action(
    proxy_id: int,
    request: Request,
    is_enabled: bool = Form(False),
    csrf_token: str = Form(""),
):
    """启停平台代理；当前有效扫码会话保持原租约直到自然结束。"""

    principal = _require_platform_principal(request)
    with SessionLocal() as db:
        require_platform_csrf(db, request, csrf_token)
        try:
            proxy = set_platform_proxy_enabled(db, proxy_id=proxy_id, is_enabled=is_enabled)
        except LoginProxyPoolError as exc:
            return _redirect("/platform/proxy-pool", _proxy_pool_error_message(exc), danger=True)
        _record_platform_operation(
            db,
            actor_user_id=principal.user_id,
            action="platform.proxy.toggle",
            target_type="platform_proxy",
            target_id=int(proxy.id),
            target_tenant_id=None,
            detail=f"is_enabled={str(bool(proxy.is_enabled)).lower()}",
        )
        db.commit()
    return _redirect("/platform/proxy-pool", "代理状态已更新")


@router.post("/proxy-pool/{proxy_id}/check")
async def platform_proxy_check_action(
    proxy_id: int,
    request: Request,
    csrf_token: str = Form(""),
):
    """由平台员工主动执行 SOCKS 端口检查并更新健康状态。"""

    principal = _require_platform_principal(request)
    with SessionLocal() as db:
        require_platform_csrf(db, request, csrf_token)
        try:
            proxy = check_platform_proxy(db, proxy_id=proxy_id)
        except LoginProxyPoolError as exc:
            return _redirect("/platform/proxy-pool", _proxy_pool_error_message(exc), danger=True)
        _record_platform_operation(
            db,
            actor_user_id=principal.user_id,
            action="platform.proxy.check",
            target_type="platform_proxy",
            target_id=int(proxy.id),
            target_tenant_id=None,
            detail=f"health_status={proxy.health_status}",
        )
        db.commit()
        # 提交后仍在当前 Session 内读取并固化普通布尔值，离开上下文后不再访问
        # 已脱离会话的 ORM 对象，避免跳转阶段触发 DetachedInstanceError。
        is_healthy = proxy.health_status == "healthy"
    if is_healthy:
        return _redirect("/platform/proxy-pool", "代理连接正常")
    return _redirect("/platform/proxy-pool", "代理连接失败，已停止参与分配", danger=True)


@router.post("/operations/alerts/{alert_id}/resolve")
async def platform_alert_resolve_action(
    alert_id: int,
    request: Request,
    csrf_token: str = Form(""),
):
    """由平台员工处理任意租户告警，并在同一事务写入平台审计。"""

    principal = _require_platform_principal(request)
    with SessionLocal() as db:
        require_platform_csrf(db, request, csrf_token)
        alert = db.get(AlertRecord, int(alert_id))
        if alert is None:
            return _redirect("/platform/operations", "告警不存在", danger=True)
        if str(alert.status or "").strip().lower() != "open":
            return _redirect("/platform/operations", "该告警已处理")
        alert.status = "resolved"
        alert.resolved_at = now_shanghai()
        _record_platform_operation(
            db,
            actor_user_id=principal.user_id,
            action="platform.alert.resolve",
            target_type="alert",
            target_id=int(alert.id),
            target_tenant_id=int(alert.tenant_id),
            detail=f"source={alert.source};title={alert.title}",
        )
        db.commit()
    return _redirect("/platform/operations", "告警已标记为已处理")


@router.post("/operations/restart-app")
async def platform_restart_app_action(
    request: Request,
    csrf_token: str = Form(""),
):
    """校验平台 CSRF 后异步触发受控部署重启脚本。"""

    principal = _require_platform_principal(request)
    with SessionLocal() as db:
        require_platform_csrf(db, request, csrf_token)

    script_path = APP_ROOT / "scripts" / "deploy_restart.sh"
    if not script_path.exists():
        return _redirect("/platform/operations", "重启脚本不存在", danger=True)
    run_dir = APP_ROOT / ".run"
    run_dir.mkdir(parents=True, exist_ok=True)
    restart_log = run_dir / "deploy-restart-trigger.log"

    try:
        with restart_log.open("a", encoding="utf-8") as log_handle:
            log_handle.write(
                f"\n[{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}] trigger restart by platform user "
                f"{principal.username}; cwd={APP_ROOT}; script={script_path}\n"
            )
            log_handle.flush()
            process = subprocess.Popen(
                ["bash", str(script_path)],
                cwd=APP_ROOT,
                stdout=log_handle,
                stderr=subprocess.STDOUT,
                stdin=subprocess.DEVNULL,
                start_new_session=True,
            )
            # 短暂等待只用于识别脚本立即失败，不等待完整部署过程阻塞当前 HTTP 请求。
            time_module.sleep(0.6)
            quick_exit_code = process.poll()
        if quick_exit_code not in (None, 0):
            tail_lines = restart_log.read_text(encoding="utf-8", errors="ignore").splitlines()[-20:]
            tail_preview = " | ".join(line.strip() for line in tail_lines if line.strip())
            reason = f"脚本快速退出（exit={quick_exit_code}）"
            if tail_preview:
                reason = f"{reason}：{tail_preview[-320:]}"
            return _redirect("/platform/operations", f"触发重启失败：{reason}", danger=True)
    except Exception as exc:
        return _redirect("/platform/operations", f"触发重启失败：{exc}", danger=True)

    with SessionLocal() as db:
        _record_platform_operation(
            db,
            actor_user_id=principal.user_id,
            action="platform.system.restart",
            target_type="system",
            target_id=None,
            target_tenant_id=None,
            detail=f"script={script_path.name};triggered=true",
        )
        db.commit()
    return _redirect("/platform/operations", "已触发重启脚本，页面可能短暂中断")


@router.get("/logs", response_class=HTMLResponse)
async def platform_logs_page(
    request: Request,
    tab: str = "error",
    source_file: str = "",
    keyword: str = "",
    level: str = "",
    page: int = 1,
):
    """读取平台服务器日志,解析为带中文标题和着色级别的条目，并按类型、文件和关键字分页展示。"""

    _require_platform_principal(request)
    active_tab = "info" if str(tab or "").strip().lower() == "info" else "error"
    normalized_keyword = str(keyword or "").strip().lower()
    available_log_files = _resolve_tab_log_paths(active_tab)
    selected_path = _resolve_selected_log_path(active_tab, str(source_file or "").strip())
    # 从文件末尾往前读,凑够一页即停;大文件也只解析本页需要的部分。
    page_size = 100
    current_page = max(int(page or 1), 1)
    min_rank = LEVEL_RANK.get(str(level or "").strip().upper(), 0)
    paged_lines, has_next = read_page(
        selected_path, page=current_page, page_size=page_size, min_rank=min_rank, keyword=normalized_keyword
    )
    start_index = (current_page - 1) * page_size
    filter_query = urlencode(
        {
            key: value
            for key, value in {
                "tab": active_tab,
                "source_file": selected_path.name,
                "keyword": keyword.strip(),
                "level": str(level or "").strip().upper(),
            }.items()
            if value
        }
    )
    context = _platform_context(
        request,
        page_title="全局日志",
        active="logs",
        log_lines=paged_lines,
        overview={
            "displayed_lines": len(paged_lines),
            "active_file": selected_path.name,
            "active_file_path": str(selected_path),
        },
        pagination={
            "page": current_page,
            "has_prev": current_page > 1,
            "has_next": has_next,
            "prev_page": max(current_page - 1, 1),
            "next_page": current_page + 1,
            "from_index": start_index + 1 if paged_lines else 0,
            "to_index": start_index + len(paged_lines),
            "query": filter_query,
        },
        active_tab=active_tab,
        source_file=selected_path.name,
        keyword=keyword,
        level=str(level or "").strip().upper(),
        available_log_files=[str(path.name) for path in available_log_files],
    )
    return templates.TemplateResponse(request, "platform_logs.html", context)
