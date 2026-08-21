"""SaaS 平台全局运维、告警和日志页面。"""

from __future__ import annotations

import subprocess
import time as time_module
from datetime import datetime
from pathlib import Path
from urllib.parse import quote, urlencode

from fastapi import APIRouter, Form, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates

from app.core.db import SessionLocal
from app.core.settings import settings
from app.core.time import format_shanghai_time, now_shanghai
from app.models.entities import AlertRecord
from app.models.saas import PlatformAuditLog
from app.services.ops import build_monitoring_snapshot
from app.web.auth_common import (
    PLATFORM_CSRF_COOKIE,
    PLATFORM_LOGOUT_PATH,
    PLATFORM_PASSWORD_PATH,
    require_platform_csrf,
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
    nav_items = [
        {"label": "平台概览", "href": "/platform/dashboard", "active": False},
        {"label": "租户管理", "href": "/platform/tenants", "active": False},
        {"label": "套餐管理", "href": "/platform/plans", "active": False},
        {"label": "订单审核", "href": "/platform/billing/orders", "active": False},
        {"label": "平台员工", "href": "/platform/users", "active": False},
        {"label": "系统运维", "href": "/platform/operations", "active": True},
    ]
    context: dict[str, object] = {
        "request": request,
        "page_title": page_title,
        "current_user": principal,
        "primary_nav_items": nav_items,
        "secondary_nav_items": [],
        "secondary_nav_active": False,
        "section_nav_items": [
            {"label": "运行监控", "href": "/platform/operations", "active": active == "operations"},
            {"label": "全局日志", "href": "/platform/logs", "active": active == "logs"},
        ],
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

    script_path = APP_ROOT / "scripts" / "deploy_restart_mba.sh"
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
    page: int = 1,
):
    """读取平台服务器日志原文，并按类型、文件和关键字分页展示。"""

    _require_platform_principal(request)
    active_tab = "info" if str(tab or "").strip().lower() == "info" else "error"
    normalized_keyword = str(keyword or "").strip().lower()
    available_log_files = _resolve_tab_log_paths(active_tab)
    selected_path = _resolve_selected_log_path(active_tab, str(source_file or "").strip())
    try:
        raw_lines = selected_path.read_text(encoding="utf-8", errors="ignore").splitlines()
    except OSError:
        raw_lines = []
    filtered_lines = [line for line in raw_lines if normalized_keyword in line.lower()] if normalized_keyword else raw_lines

    page_size = 1200
    current_page = max(int(page or 1), 1)
    total = len(filtered_lines)
    total_pages = max((total + page_size - 1) // page_size, 1)
    current_page = min(current_page, total_pages)
    start_index = (current_page - 1) * page_size
    end_index = min(start_index + page_size, total)
    paged_lines = list(reversed(filtered_lines))[start_index:end_index]
    filter_query = urlencode(
        {
            key: value
            for key, value in {
                "tab": active_tab,
                "source_file": selected_path.name,
                "keyword": keyword.strip(),
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
            "total_lines": total,
            "displayed_lines": len(paged_lines),
            "active_file": selected_path.name,
            "active_file_path": str(selected_path),
            "order_label": "最新在前",
        },
        pagination={
            "page": current_page,
            "total": total,
            "total_pages": total_pages,
            "has_prev": current_page > 1,
            "has_next": current_page < total_pages,
            "prev_page": current_page - 1 if current_page > 1 else 1,
            "next_page": current_page + 1 if current_page < total_pages else total_pages,
            "from_index": start_index + 1 if total else 0,
            "to_index": end_index,
            "query": filter_query,
        },
        active_tab=active_tab,
        source_file=selected_path.name,
        keyword=keyword,
        available_log_files=[str(path.name) for path in available_log_files],
    )
    return templates.TemplateResponse(request, "platform_logs.html", context)
