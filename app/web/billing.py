"""租户套餐、订单、人工付款凭证和私有下载页面。"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
from pathlib import Path
import secrets
from urllib.parse import quote

from fastapi import APIRouter, File, Form, HTTPException, Request, UploadFile
from fastapi.responses import FileResponse, HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy import func, select

from app.core.db import SessionLocal
from app.core.settings import settings
from app.core.time import format_shanghai_time
from app.models.saas import BotBinding, BillingOrder
from app.services.billing import (
    BillingError,
    cancel_order,
    create_billing_order,
    get_current_subscription,
    get_tenant_order,
    list_public_plans,
    list_tenant_orders,
    submit_payment_evidence,
)
from app.services.payment_gateways.manual import ManualPaymentGateway
from app.services.saas_auth import has_tenant_role
from app.web.auth_common import (
    TENANT_CSRF_COOKIE,
    TENANT_LOGOUT_PATH,
    TENANT_PASSWORD_PATH,
    require_tenant_csrf,
)
from app.web.tenant_navigation import build_tenant_navigation


router = APIRouter(prefix="/account", tags=["tenant-billing"])
templates = Jinja2Templates(directory="app/templates")

ORDER_STATUS_LABELS = {
    "pending_payment": "等待付款",
    "payment_submitted": "已提交付款凭证，等待审核",
    "approved": "已审核开通",
    "rejected": "付款凭证已驳回",
    "cancelled": "已取消",
}
PROOF_CONTENT_TYPES = {
    ".png": "image/png",
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
    ".webp": "image/webp",
    ".pdf": "application/pdf",
}


class PaymentProofError(ValueError):
    """付款凭证文件不满足私有存储规则。"""


@dataclass(frozen=True)
class StoredPaymentProof:
    """凭证落盘结果；页面和日志只能使用相对路径。"""

    relative_path: str
    absolute_path: Path


def _require_principal(request: Request):
    """只接受主应用中间件恢复出的可信租户身份。"""

    principal = getattr(request.state, "current_user", None)
    if principal is None:
        raise HTTPException(status_code=401, detail="Authentication required")
    return principal


def _can_manage_billing(principal) -> bool:
    """企业所有者和 tenant_admin 可以执行购买动作，其他员工保持只读。"""

    return bool(principal.is_owner or has_tenant_role(principal, "tenant_admin"))


def _require_billing_manager(request: Request):
    """拒绝普通员工创建订单、上传付款凭证或取消订单。"""

    principal = _require_principal(request)
    if not _can_manage_billing(principal):
        raise HTTPException(status_code=403, detail="Permission denied")
    return principal


def _format_yuan(cents: int) -> str:
    """把整数分格式化为两位小数，避免模板浮点金额误差。"""

    return f"{Decimal(int(cents)) / Decimal(100):.2f}"


def _tenant_context(
    request: Request,
    *,
    page_title: str,
    notice: str = "",
    notice_tone: str = "",
    **extra,
) -> dict[str, object]:
    """构造租户计费页的稳定导航、身份和格式化上下文。"""

    principal = _require_principal(request)
    context: dict[str, object] = {
        "request": request,
        "page_title": page_title,
        "current_user": principal,
        "module_links": {},
        "notice": notice,
        "notice_tone": notice_tone or ("danger" if notice and "成功" not in notice else "info"),
        "csrf_token": request.cookies.get(TENANT_CSRF_COOKIE, ""),
        "password_action": TENANT_PASSWORD_PATH,
        "logout_action": TENANT_LOGOUT_PATH,
        "format_time": format_shanghai_time,
        "format_yuan": _format_yuan,
        "status_labels": ORDER_STATUS_LABELS,
        "can_manage_billing": _can_manage_billing(principal),
    }
    context.update(build_tenant_navigation(request.url.path))
    context.update(extra)
    return context


def _redirect(path: str, notice: str, *, danger: bool = False) -> RedirectResponse:
    """生成不携带内部路径或付款内容的租户页面跳转。"""

    suffix = f"?notice={quote(notice)}"
    if danger:
        suffix += "&tone=danger"
    return RedirectResponse(url=path + suffix, status_code=303)


def _proof_signature_matches(suffix: str, content: bytes) -> bool:
    """用常见文件头阻止只修改扩展名和 MIME 的伪造上传。"""

    if suffix == ".png":
        return content.startswith(b"\x89PNG\r\n\x1a\n")
    if suffix in {".jpg", ".jpeg"}:
        return content.startswith(b"\xff\xd8\xff")
    if suffix == ".webp":
        return len(content) >= 12 and content.startswith(b"RIFF") and content[8:12] == b"WEBP"
    if suffix == ".pdf":
        return content.startswith(b"%PDF-")
    return False


def _store_payment_proof(
    order: BillingOrder,
    *,
    filename: str,
    content_type: str,
    content: bytes,
) -> StoredPaymentProof:
    """验证并把凭证写入当前租户订单的私有目录，完全丢弃客户原文件名。"""

    suffix = Path(filename or "").suffix.lower()
    expected_content_type = PROOF_CONTENT_TYPES.get(suffix)
    if expected_content_type is None or content_type != expected_content_type:
        raise PaymentProofError("payment_proof_type_invalid")
    max_bytes = max(1, int(settings.billing_payment_proof_max_bytes))
    if not content or len(content) > max_bytes:
        raise PaymentProofError("payment_proof_size_invalid")
    if not _proof_signature_matches(suffix, content):
        raise PaymentProofError("payment_proof_signature_invalid")

    data_root = Path(settings.data_dir).resolve()
    order_dir = (data_root / "tenant_billing" / str(order.tenant_id) / order.order_no).resolve()
    if data_root != order_dir and data_root not in order_dir.parents:
        raise PaymentProofError("payment_proof_path_invalid")
    order_dir.mkdir(parents=True, exist_ok=True)
    # 每次提交使用服务端随机文件名，先保留旧凭证；数据库提交成功后再清理旧文件。
    target = (order_dir / f"proof-{secrets.token_hex(8)}{suffix}").resolve()
    if order_dir not in target.parents:
        raise PaymentProofError("payment_proof_path_invalid")
    target.write_bytes(content)
    return StoredPaymentProof(
        relative_path=target.relative_to(data_root).as_posix(),
        absolute_path=target,
    )


def _resolve_order_proof_relative_path(order: BillingOrder, relative_path: str) -> Path:
    """把指定相对路径限制在当前租户和订单的私有目录内。"""

    if not relative_path:
        raise PaymentProofError("payment_proof_not_found")
    data_root = Path(settings.data_dir).resolve()
    candidate = (data_root / relative_path).resolve()
    expected_order_dir = (data_root / "tenant_billing" / str(order.tenant_id) / order.order_no).resolve()
    if expected_order_dir not in candidate.parents or data_root not in candidate.parents:
        raise PaymentProofError("payment_proof_path_invalid")
    if not candidate.is_file():
        raise PaymentProofError("payment_proof_not_found")
    return candidate


def resolve_order_proof_path(order: BillingOrder) -> Path:
    """解析数据库记录的付款凭证路径，供租户和平台受控下载复用。"""

    return _resolve_order_proof_relative_path(order, order.payment_proof_path or "")


def _render_order_detail(
    request: Request,
    *,
    order: BillingOrder,
    notice: str = "",
    status_code: int = 200,
):
    """统一渲染订单详情，模板只接收是否存在凭证，不接收内部存储路径。"""

    context = _tenant_context(
        request,
        # 高熵订单号可能包含无法自然断行的连续字符。页面标题保持稳定，完整订单号只在
        # 详情字段内展示并由局部样式处理换行，避免移动端页头按内容宽度撑开工作区。
        page_title="订单详情",
        notice=notice,
        notice_tone="danger" if status_code >= 400 else "",
        order=order,
        payment_instructions=ManualPaymentGateway().get_instructions(),
        has_payment_proof=bool(order.payment_proof_path),
    )
    return templates.TemplateResponse(
        request,
        "account_order_detail.html",
        context,
        status_code=status_code,
    )


@router.get("/onboarding", response_class=HTMLResponse)
async def account_onboarding_page(request: Request):
    """展示注册后的试用状态和三个主要下一步。"""

    principal = _require_principal(request)
    with SessionLocal() as db:
        subscription = get_current_subscription(db, tenant_id=principal.tenant_id)
        context = _tenant_context(
            request,
            page_title="开始使用 Fulcrum",
            notice=request.query_params.get("notice", ""),
            notice_tone=request.query_params.get("tone", ""),
            subscription=subscription,
        )
        return templates.TemplateResponse(request, "account_onboarding.html", context)


@router.get("/billing", response_class=HTMLResponse)
async def account_billing_page(request: Request):
    """展示当前订阅、机器人额度使用量和可购买公开套餐。"""

    principal = _require_principal(request)
    with SessionLocal() as db:
        subscription = get_current_subscription(db, tenant_id=principal.tenant_id)
        used_bot_count = int(
            db.scalar(
                select(func.count(BotBinding.id)).where(
                    BotBinding.tenant_id == principal.tenant_id,
                    BotBinding.status.in_(("active", "disabled")),
                )
            )
            or 0
        )
        context = _tenant_context(
            request,
            page_title="套餐与额度",
            notice=request.query_params.get("notice", ""),
            notice_tone=request.query_params.get("tone", ""),
            subscription=subscription,
            used_bot_count=used_bot_count,
            plans=list_public_plans(db),
        )
        return templates.TemplateResponse(request, "account_billing.html", context)


@router.get("/orders", response_class=HTMLResponse)
async def account_orders_page(request: Request):
    """展示当前租户订单历史。"""

    principal = _require_principal(request)
    with SessionLocal() as db:
        context = _tenant_context(
            request,
            page_title="订单",
            notice=request.query_params.get("notice", ""),
            notice_tone=request.query_params.get("tone", ""),
            orders=list_tenant_orders(db, tenant_id=principal.tenant_id),
        )
        return templates.TemplateResponse(request, "account_orders.html", context)


@router.post("/orders")
async def account_order_create_action(
    request: Request,
    plan_id: int = Form(0),
    billing_cycle: str = Form("monthly"),
    csrf_token: str = Form(""),
):
    """由企业管理角色创建服务端定价订单。"""

    principal = _require_billing_manager(request)
    with SessionLocal() as db:
        require_tenant_csrf(db, request, csrf_token)
        try:
            order = create_billing_order(
                db,
                tenant_id=principal.tenant_id,
                plan_id=plan_id,
                billing_cycle=billing_cycle,
            )
        except BillingError as exc:
            return _redirect("/account/billing", f"订单创建失败：{exc}", danger=True)
    return RedirectResponse(url=f"/account/orders/{order.id}", status_code=303)


@router.get("/orders/{order_id}", response_class=HTMLResponse)
async def account_order_detail_page(order_id: int, request: Request):
    """按可信租户范围展示订单详情和付款说明。"""

    principal = _require_principal(request)
    with SessionLocal() as db:
        try:
            order = get_tenant_order(db, tenant_id=principal.tenant_id, order_id=order_id)
        except BillingError:
            raise HTTPException(status_code=404, detail="Order not found")
        return _render_order_detail(
            request,
            order=order,
            notice=request.query_params.get("notice", ""),
        )


@router.post("/orders/{order_id}/pay")
async def account_order_payment_action(
    order_id: int,
    request: Request,
    payment_reference: str = Form(""),
    csrf_token: str = Form(""),
    payment_proof: UploadFile = File(...),
):
    """校验并私有保存付款凭证，再把订单推进到待审核状态。"""

    principal = _require_billing_manager(request)
    with SessionLocal() as db:
        require_tenant_csrf(db, request, csrf_token)
        try:
            order = get_tenant_order(db, tenant_id=principal.tenant_id, order_id=order_id)
        except BillingError:
            raise HTTPException(status_code=404, detail="Order not found")
        if order.status not in {"pending_payment", "rejected"}:
            return _render_order_detail(
                request,
                order=order,
                notice="付款凭证提交失败：order_payment_not_editable",
                status_code=400,
            )
        if not (payment_reference or "").strip() or len((payment_reference or "").strip()) > 255:
            return _render_order_detail(
                request,
                order=order,
                notice="付款凭证提交失败：payment_reference_invalid",
                status_code=400,
            )
        max_bytes = max(1, int(settings.billing_payment_proof_max_bytes))
        content = await payment_proof.read(max_bytes + 1)
        await payment_proof.close()
        previous_relative_path = order.payment_proof_path
        try:
            stored = _store_payment_proof(
                order,
                filename=payment_proof.filename or "",
                content_type=payment_proof.content_type or "",
                content=content,
            )
            submit_payment_evidence(
                db,
                tenant_id=principal.tenant_id,
                order_id=order.id,
                payment_reference=payment_reference,
                payment_proof_path=stored.relative_path,
            )
        except (BillingError, PaymentProofError) as exc:
            if "stored" in locals() and stored.absolute_path.is_file():
                stored.absolute_path.unlink()
            return _render_order_detail(
                request,
                order=order,
                notice=f"付款凭证提交失败：{exc}",
                status_code=400,
            )

        # 重新提交时，数据库提交成功后再清理旧文件，避免失败时丢失原凭证。
        if previous_relative_path and previous_relative_path != stored.relative_path:
            try:
                old_path = _resolve_order_proof_relative_path(order, previous_relative_path)
            except PaymentProofError:
                old_path = None
            if old_path is not None and old_path != stored.absolute_path:
                old_path.unlink()
    return _redirect(f"/account/orders/{order_id}", "付款凭证已提交，等待平台审核")


@router.post("/orders/{order_id}/cancel")
async def account_order_cancel_action(
    order_id: int,
    request: Request,
    csrf_token: str = Form(""),
):
    """校验租户 CSRF 后取消未进入审核的订单。"""

    principal = _require_billing_manager(request)
    with SessionLocal() as db:
        require_tenant_csrf(db, request, csrf_token)
        try:
            cancel_order(db, tenant_id=principal.tenant_id, order_id=order_id)
        except BillingError as exc:
            return _redirect(f"/account/orders/{order_id}", f"订单取消失败：{exc}", danger=True)
    return _redirect(f"/account/orders/{order_id}", "订单已取消")


@router.get("/orders/{order_id}/proof")
async def account_order_proof_download(order_id: int, request: Request):
    """经租户订单鉴权下载私有付款凭证，不通过 StaticFiles 暴露目录。"""

    principal = _require_principal(request)
    with SessionLocal() as db:
        try:
            order = get_tenant_order(db, tenant_id=principal.tenant_id, order_id=order_id)
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
