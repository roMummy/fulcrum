"""租户企业微信客户端、激活码和账号状态管理页面。"""

from __future__ import annotations

from urllib.parse import quote

from fastapi import APIRouter, Form, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy import func, select

from app.core.db import TenantSessionLocal
from app.core.tenant_db import open_tenant_session
from app.core.time import format_shanghai_time
from app.models.entities import Channel
from app.models.saas import ProviderHeartbeat
from app.services.ops import record_audit
from app.services.provider_nodes import (
    ProviderNodeError,
    create_provider_node,
    disable_provider_node,
    get_provider_node,
    get_provider_node_quota,
    issue_activation_code,
    list_provider_nodes,
    reactivate_provider_node,
    revoke_provider_node_credentials,
)
from app.services.saas_auth import has_tenant_role
from app.web.auth_common import (
    TENANT_CSRF_COOKIE,
    TENANT_LOGOUT_PATH,
    TENANT_PASSWORD_PATH,
    require_tenant_csrf,
)
from app.web.tenant_navigation import build_tenant_navigation


router = APIRouter(prefix="/provider-nodes", tags=["provider-node-pages"])
templates = Jinja2Templates(directory="app/templates")


def _require_principal(request: Request):
    """只使用认证中间件恢复的当前租户员工身份。"""

    principal = getattr(request.state, "current_user", None)
    if principal is None:
        raise HTTPException(status_code=401, detail="Authentication required")
    return principal


def _require_tenant_admin(request: Request):
    """节点生命周期和绑定变更只允许租户管理员执行。"""

    principal = _require_principal(request)
    if not has_tenant_role(principal, "tenant_admin"):
        raise HTTPException(status_code=403, detail="Permission denied")
    return principal


def _page_context(
    request: Request,
    *,
    page_title: str,
    notice: str = "",
    notice_tone: str = "",
    **extra,
) -> dict[str, object]:
    """构造企业微信客户端页面共用身份上下文。"""

    principal = _require_principal(request)
    context: dict[str, object] = {
        "request": request,
        "page_title": page_title,
        # 节点名可能包含设备编号或连续英文标识，页面标题必须允许在移动端任意断行。
        "page_title_class": "provider-node-breakable",
        "current_user": principal,
        "module_links": {},
        "notice": notice,
        "notice_tone": notice_tone or ("danger" if notice and "成功" not in notice and "已" not in notice else "info"),
        "csrf_token": request.cookies.get(TENANT_CSRF_COOKIE, ""),
        "password_action": TENANT_PASSWORD_PATH,
        "logout_action": TENANT_LOGOUT_PATH,
        "format_time": format_shanghai_time,
        "can_manage_provider_nodes": has_tenant_role(principal, "tenant_admin"),
    }
    context.update(build_tenant_navigation(request.url.path))
    context.update(extra)
    return context


def _redirect(path: str, notice: str, *, danger: bool = False) -> RedirectResponse:
    """节点管理跳转只携带普通提示，不携带激活码或设备身份。"""

    suffix = f"?notice={quote(notice)}"
    if danger:
        suffix += "&tone=danger"
    return RedirectResponse(url=path + suffix, status_code=303)


def _node_detail_data(db, node_id: int) -> dict[str, object]:
    """读取当前租户节点详情、绑定渠道和最近心跳。"""

    node = get_provider_node(db, node_id)
    if node is None:
        raise HTTPException(status_code=404, detail="Provider node not found")
    channels = db.scalars(
        select(Channel).where(Channel.provider_node_id == node.id, Channel.deleted_at.is_(None)).order_by(Channel.id.desc())
    ).all()
    latest_heartbeat = db.scalar(
        select(ProviderHeartbeat)
        .where(ProviderHeartbeat.provider_node_id == node.id)
        .order_by(ProviderHeartbeat.reported_at.desc(), ProviderHeartbeat.id.desc())
        .limit(1)
    )
    return {
        "node": node,
        "bound_channels": channels,
        "latest_heartbeat": latest_heartbeat,
        "bound_channel_count": len(channels),
    }


@router.get("", response_class=HTMLResponse)
async def provider_node_list_page(request: Request):
    """所有租户角色都可查看节点状态和套餐额度。"""

    db = open_tenant_session(TenantSessionLocal)
    try:
        nodes = list_provider_nodes(db)
        bound_counts = {
            int(node_id): int(count)
            for node_id, count in db.execute(
                select(Channel.provider_node_id, func.count(Channel.id))
                .where(Channel.provider_node_id.is_not(None), Channel.deleted_at.is_(None))
                .group_by(Channel.provider_node_id)
            ).all()
        }
        context = _page_context(
            request,
            page_title="企业微信客户端",
            notice=request.query_params.get("notice", ""),
            notice_tone=request.query_params.get("tone", ""),
            nodes=nodes,
            quota=get_provider_node_quota(db),
            bound_counts=bound_counts,
        )
        return templates.TemplateResponse(request, "provider_nodes.html", context)
    finally:
        db.close()


@router.post("", response_class=HTMLResponse)
async def provider_node_create_action(
    request: Request,
    name: str = Form(""),
    notes: str = Form(""),
    csrf_token: str = Form(""),
):
    """创建节点并在当前成功响应中一次性展示首个激活码。"""

    principal = _require_tenant_admin(request)
    db = open_tenant_session(TenantSessionLocal)
    try:
        require_tenant_csrf(db, request, csrf_token)
        try:
            node = create_provider_node(db, principal.tenant_id, name=name, notes=notes)
            issued = issue_activation_code(db, node.id, created_by_user_id=principal.user_id)
            record_audit(db, "provider_node.create", "provider_node", node.id, f"创建 Provider 节点 {node.name}")
        except ProviderNodeError as exc:
            return _redirect("/provider-nodes", f"节点创建失败：{exc}", danger=True)
        context = _page_context(
            request,
            page_title=f"企业微信客户端 · {node.name}",
            notice="节点创建成功。激活码只显示本次，请立即安全交付。",
            activation_code=issued.raw_code,
            activation_expires_at=issued.expires_at,
            **_node_detail_data(db, node.id),
        )
        return templates.TemplateResponse(request, "provider_node_detail.html", context, status_code=201)
    finally:
        db.close()


@router.get("/{node_id}", response_class=HTMLResponse)
async def provider_node_detail_page(node_id: int, request: Request):
    """展示节点账号、绑定渠道、心跳和管理动作。"""

    db = open_tenant_session(TenantSessionLocal)
    try:
        data = _node_detail_data(db, node_id)
        context = _page_context(
            request,
            page_title=f"企业微信客户端 · {data['node'].name}",
            notice=request.query_params.get("notice", ""),
            notice_tone=request.query_params.get("tone", ""),
            activation_code="",
            activation_expires_at=None,
            **data,
        )
        return templates.TemplateResponse(request, "provider_node_detail.html", context)
    finally:
        db.close()


@router.post("/{node_id}/activation-code", response_class=HTMLResponse)
async def provider_node_activation_code_action(
    node_id: int,
    request: Request,
    csrf_token: str = Form(""),
):
    """为待激活节点重新生成一次性激活码，并只在当前响应展示原文。"""

    principal = _require_tenant_admin(request)
    db = open_tenant_session(TenantSessionLocal)
    try:
        require_tenant_csrf(db, request, csrf_token)
        try:
            issued = issue_activation_code(db, node_id, created_by_user_id=principal.user_id)
            record_audit(db, "provider_node.activation_code.issue", "provider_node", node_id, "重新生成节点激活码")
        except ProviderNodeError as exc:
            return _redirect(f"/provider-nodes/{node_id}", f"激活码生成失败：{exc}", danger=True)
        context = _page_context(
            request,
            page_title=f"企业微信客户端 · {_node_detail_data(db, node_id)['node'].name}",
            notice="新的激活码已生成，旧未使用激活码已失效。",
            activation_code=issued.raw_code,
            activation_expires_at=issued.expires_at,
            **_node_detail_data(db, node_id),
        )
        return templates.TemplateResponse(request, "provider_node_detail.html", context)
    finally:
        db.close()


@router.post("/{node_id}/revoke")
async def provider_node_revoke_action(node_id: int, request: Request, csrf_token: str = Form("")):
    """吊销当前设备密钥并让节点回到待激活状态。"""

    _require_tenant_admin(request)
    db = open_tenant_session(TenantSessionLocal)
    try:
        require_tenant_csrf(db, request, csrf_token)
        try:
            node = revoke_provider_node_credentials(db, node_id)
            record_audit(db, "provider_node.revoke", "provider_node", node.id, f"吊销 Provider 节点 {node.name} 设备密钥")
        except ProviderNodeError as exc:
            return _redirect(f"/provider-nodes/{node_id}", f"吊销失败：{exc}", danger=True)
    finally:
        db.close()
    return _redirect(f"/provider-nodes/{node_id}", "设备密钥已吊销，节点等待重新激活")


@router.post("/{node_id}/reactivate", response_class=HTMLResponse)
async def provider_node_reactivate_action(
    node_id: int,
    request: Request,
    csrf_token: str = Form(""),
):
    """吊销旧密钥并在当前响应一次性展示新的重新激活码。"""

    principal = _require_tenant_admin(request)
    db = open_tenant_session(TenantSessionLocal)
    try:
        require_tenant_csrf(db, request, csrf_token)
        try:
            issued = reactivate_provider_node(db, node_id, created_by_user_id=principal.user_id)
            record_audit(db, "provider_node.reactivate", "provider_node", node_id, "吊销旧密钥并生成重新激活码")
        except ProviderNodeError as exc:
            return _redirect(f"/provider-nodes/{node_id}", f"重新激活失败：{exc}", danger=True)
        data = _node_detail_data(db, node_id)
        context = _page_context(
            request,
            page_title=f"企业微信客户端 · {data['node'].name}",
            notice="旧设备密钥已吊销。新的激活码只显示本次。",
            activation_code=issued.raw_code,
            activation_expires_at=issued.expires_at,
            **data,
        )
        return templates.TemplateResponse(request, "provider_node_detail.html", context)
    finally:
        db.close()


@router.post("/{node_id}/disable")
async def provider_node_disable_action(node_id: int, request: Request, csrf_token: str = Form("")):
    """停用节点并立即吊销凭证，绑定渠道保留供管理员后续逐一处理。"""

    _require_tenant_admin(request)
    db = open_tenant_session(TenantSessionLocal)
    try:
        require_tenant_csrf(db, request, csrf_token)
        try:
            node = disable_provider_node(db, node_id)
            record_audit(db, "provider_node.disable", "provider_node", node.id, f"停用 Provider 节点 {node.name}")
        except ProviderNodeError as exc:
            return _redirect(f"/provider-nodes/{node_id}", f"停用失败：{exc}", danger=True)
    finally:
        db.close()
    return _redirect(f"/provider-nodes/{node_id}", "企业微信客户端已停用")
