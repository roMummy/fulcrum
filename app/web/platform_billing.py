"""平台付款订单审核、凭证下载和订阅履约页面。"""

from __future__ import annotations

from decimal import Decimal
from urllib.parse import quote

from fastapi import APIRouter, Form, HTTPException, Request
from fastapi.responses import FileResponse, HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates

from app.core.db import SessionLocal
from app.core.time import format_shanghai_time
from app.services.billing import (
    BillingError,
    approve_order,
    get_billing_order,
    list_billing_orders,
    reject_order,
)
from app.services.bot_entitlements import (
    BotEntitlementError,
    get_bot_binding_for_platform,
    list_all_bot_bindings,
    release_bot_binding_by_platform,
)
from app.web.auth_common import (
    PLATFORM_CSRF_COOKIE,
    PLATFORM_LOGOUT_PATH,
    PLATFORM_PASSWORD_PATH,
    require_platform_csrf,
)
from app.web.billing import (
    ORDER_STATUS_LABELS,
    PROOF_CONTENT_TYPES,
    PaymentProofError,
    resolve_order_proof_path,
)
from app.web.platform_navigation import build_platform_primary_navigation


router = APIRouter(prefix="/platform", tags=["platform-billing"])
templates = Jinja2Templates(directory="app/templates")


def _require_platform_principal(request: Request):
    """只接受中间件恢复的平台管理员会话，不读取租户 Cookie。"""

    principal = getattr(request.state, "platform_principal", None)
    if principal is None:
        raise HTTPException(status_code=401, detail="Platform authentication required")
    return principal


def _format_yuan(cents: int) -> str:
    """把订单整数分格式化为两位小数。"""

    return f"{Decimal(int(cents)) / Decimal(100):.2f}"


def _platform_context(
    request: Request,
    *,
    page_title: str,
    active: str = "orders",
    notice: str = "",
    notice_tone: str = "",
    **extra,
) -> dict[str, object]:
    """构造平台订单页面上下文，并保持平台 Cookie 与导航边界。"""

    principal = _require_platform_principal(request)
    context: dict[str, object] = {
        "request": request,
        "page_title": page_title,
        "current_user": principal,
        "primary_nav_items": build_platform_primary_navigation("billing"),
        "secondary_nav_items": [],
        "secondary_nav_active": False,
        "section_nav_items": [
            {"label": "订单审核", "href": "/platform/billing/orders", "active": active == "orders"},
            {"label": "机器人绑定", "href": "/platform/bot-bindings", "active": active == "bindings"},
        ],
        "module_links": {},
        "notice": notice,
        "notice_tone": notice_tone or ("danger" if notice and "成功" not in notice else "info"),
        "csrf_token": request.cookies.get(PLATFORM_CSRF_COOKIE, ""),
        "password_action": PLATFORM_PASSWORD_PATH,
        "logout_action": PLATFORM_LOGOUT_PATH,
        "format_time": format_shanghai_time,
        "format_yuan": _format_yuan,
        "status_labels": ORDER_STATUS_LABELS,
    }
    context.update(extra)
    return context


def _redirect(path: str, notice: str, *, danger: bool = False) -> RedirectResponse:
    """平台审核跳转不携带凭证路径或付款内容。"""

    suffix = f"?notice={quote(notice)}"
    if danger:
        suffix += "&tone=danger"
    return RedirectResponse(url=path + suffix, status_code=303)


@router.get("/billing/orders", response_class=HTMLResponse)
async def platform_billing_orders_page(request: Request):
    """展示平台订单清单，并把待审核订单排在最前。"""

    with SessionLocal() as db:
        context = _platform_context(
            request,
            page_title="订单审核",
            notice=request.query_params.get("notice", ""),
            notice_tone=request.query_params.get("tone", ""),
            orders=list_billing_orders(db),
        )
        return templates.TemplateResponse(request, "platform_billing_orders.html", context)


@router.get("/billing/orders/{order_id}", response_class=HTMLResponse)
async def platform_billing_order_detail_page(order_id: int, request: Request):
    """展示企业、套餐、付款流水和审核动作，不传递内部存储路径。"""

    with SessionLocal() as db:
        try:
            order = get_billing_order(db, order_id=order_id)
        except BillingError:
            raise HTTPException(status_code=404, detail="Order not found")
        context = _platform_context(
            request,
            page_title=f"审核订单 {order.order_no}",
            notice=request.query_params.get("notice", ""),
            notice_tone=request.query_params.get("tone", ""),
            order=order,
            has_payment_proof=bool(order.payment_proof_path),
        )
        return templates.TemplateResponse(request, "platform_billing_order_detail.html", context)


@router.post("/billing/orders/{order_id}/approve")
async def platform_billing_order_approve_action(
    order_id: int,
    request: Request,
    csrf_token: str = Form(""),
):
    """校验平台 CSRF 后幂等批准订单并履约订阅。"""

    principal = _require_platform_principal(request)
    with SessionLocal() as db:
        require_platform_csrf(db, request, csrf_token)
        try:
            approve_order(db, order_id=order_id, reviewer_user_id=principal.user_id)
        except BillingError as exc:
            return _redirect(
                f"/platform/billing/orders/{order_id}",
                f"订单批准失败：{exc}",
                danger=True,
            )
    return _redirect(f"/platform/billing/orders/{order_id}", "订单已审核并完成订阅开通")


@router.post("/billing/orders/{order_id}/reject")
async def platform_billing_order_reject_action(
    order_id: int,
    request: Request,
    reason: str = Form(""),
    csrf_token: str = Form(""),
):
    """校验平台 CSRF 后驳回付款凭证，原因写入订单和平台审计。"""

    principal = _require_platform_principal(request)
    with SessionLocal() as db:
        require_platform_csrf(db, request, csrf_token)
        try:
            reject_order(
                db,
                order_id=order_id,
                reviewer_user_id=principal.user_id,
                reason=reason,
            )
        except BillingError as exc:
            return _redirect(
                f"/platform/billing/orders/{order_id}",
                f"订单驳回失败：{exc}",
                danger=True,
            )
    return _redirect(f"/platform/billing/orders/{order_id}", "订单已驳回，租户可重新提交凭证")


@router.get("/billing/orders/{order_id}/proof")
async def platform_billing_order_proof_download(order_id: int, request: Request):
    """通过平台身份下载私有凭证，响应不暴露服务器路径。"""

    _require_platform_principal(request)
    with SessionLocal() as db:
        try:
            order = get_billing_order(db, order_id=order_id)
            proof_path = resolve_order_proof_path(order)
        except (BillingError, PaymentProofError):
            raise HTTPException(status_code=404, detail="Payment proof not found")
        media_type = PROOF_CONTENT_TYPES.get(proof_path.suffix.lower(), "application/octet-stream")
        return FileResponse(
            proof_path,
            media_type=media_type,
            filename=f"{order.order_no}-proof{proof_path.suffix.lower()}",
            headers={"X-Content-Type-Options": "nosniff", "Cache-Control": "private, no-store"},
        )


@router.get("/bot-bindings", response_class=HTMLResponse)
async def platform_bot_bindings_page(request: Request):
    """展示全平台机器人绑定和释放状态。"""

    with SessionLocal() as db:
        context = _platform_context(
            request,
            page_title="机器人绑定管理",
            active="bindings",
            notice=request.query_params.get("notice", ""),
            notice_tone=request.query_params.get("tone", ""),
            bindings=list_all_bot_bindings(db),
        )
        return templates.TemplateResponse(request, "platform_bot_bindings.html", context)


@router.get("/bot-bindings/{binding_id}", response_class=HTMLResponse)
async def platform_bot_binding_detail_page(binding_id: int, request: Request):
    """展示单条绑定、租户归属和受控释放入口。"""

    with SessionLocal() as db:
        try:
            binding = get_bot_binding_for_platform(db, binding_id=binding_id)
        except BotEntitlementError:
            raise HTTPException(status_code=404, detail="Bot binding not found")
        context = _platform_context(
            request,
            page_title=f"机器人绑定 #{binding.id}",
            active="bindings",
            notice=request.query_params.get("notice", ""),
            notice_tone=request.query_params.get("tone", ""),
            binding=binding,
            device_hash_preview=f"{binding.device_fingerprint_hash[:8]}...{binding.device_fingerprint_hash[-6:]}",
        )
        return templates.TemplateResponse(request, "platform_bot_binding_detail.html", context)


@router.post("/bot-bindings/{binding_id}/release")
async def platform_bot_binding_release_action(
    binding_id: int,
    request: Request,
    reason: str = Form(""),
    csrf_token: str = Form(""),
):
    """平台管理员带原因释放绑定，租户端没有对应动作。"""

    principal = _require_platform_principal(request)
    with SessionLocal() as db:
        require_platform_csrf(db, request, csrf_token)
        try:
            release_bot_binding_by_platform(
                db,
                binding_id=binding_id,
                reviewer_user_id=principal.user_id,
                reason=reason,
            )
        except BotEntitlementError as exc:
            return _redirect(
                f"/platform/bot-bindings/{binding_id}",
                f"绑定释放失败：{exc}",
                danger=True,
            )
    return _redirect(f"/platform/bot-bindings/{binding_id}", "机器人绑定已受控释放")
