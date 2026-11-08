"""企业租户登录后的桥接接口开发文档页面。"""

from __future__ import annotations

from pathlib import Path

import markdown
from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates

from app.web.auth_common import TENANT_CSRF_COOKIE, TENANT_LOGOUT_PATH, TENANT_PASSWORD_PATH
from app.web.tenant_navigation import build_tenant_navigation


router = APIRouter(tags=["developer-docs"])
templates = Jinja2Templates(directory="app/templates")
DOCUMENT_PATH = Path(__file__).resolve().parents[2] / "docs" / "bridge-api.md"


def _load_document_html() -> str:
    """读取版本库内固定的 Markdown 文档并转换为页面 HTML。"""

    try:
        content = DOCUMENT_PATH.read_text(encoding="utf-8")
        # 文档内容来自受版本控制的固定文件，不接受请求参数拼接路径；模板只对
        # 这份受控内容使用 safe 输出，避免把 Markdown 标记原样显示给开发者。
        return markdown.markdown(content, extensions=["extra", "codehilite"])
    except (OSError, UnicodeError, ValueError) as exc:
        # 绝对路径和解析堆栈不应出现在租户页面或日志响应中，统一转换为稳定错误码。
        raise HTTPException(status_code=503, detail="developer_docs_unavailable") from exc


@router.get("/developer/docs", response_class=HTMLResponse)
async def developer_docs_page(request: Request):
    """展示已登录租户员工可阅读的桥接接口文档。"""

    principal = getattr(request.state, "current_user", None)
    if principal is None:
        return RedirectResponse(url="/login", status_code=303)

    context: dict[str, object] = {
        "request": request,
        "page_title": "开发文档",
        "page_intro": "使用桥接接口开发自己的订单、客服和自动化业务。",
        "current_user": principal,
        "csrf_token": request.cookies.get(TENANT_CSRF_COOKIE, ""),
        "password_action": TENANT_PASSWORD_PATH,
        "logout_action": TENANT_LOGOUT_PATH,
        "document_html": _load_document_html(),
    }
    context.update(build_tenant_navigation(request.url.path))
    return templates.TemplateResponse(request, "developer_docs.html", context)
