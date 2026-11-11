"""企业租户登录后的文档中心:桥接接口文档、插件开发规范,以及标准示例插件下载。"""

from __future__ import annotations

import io
import re
import zipfile
from pathlib import Path

import markdown
from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse, Response
from fastapi.templating import Jinja2Templates

from app.web.auth_common import TENANT_CSRF_COOKIE, TENANT_LOGOUT_PATH, TENANT_PASSWORD_PATH
from app.web.tenant_navigation import build_tenant_navigation


router = APIRouter(tags=["developer-docs"])
templates = Jinja2Templates(directory="app/templates")
DOCS_ROOT = Path(__file__).resolve().parents[2] / "docs"
DOCUMENT_PATH = DOCS_ROOT / "bridge-api.md"
PLUGIN_DOCUMENT_PATH = DOCS_ROOT / "plugin-development.md"
# 标准示例插件:随仓库维护的一个完整可安装的插件目录,文档中心把它打成 zip 提供下载
PLUGIN_TEMPLATE_DIR = DOCS_ROOT / "plugin-template" / "hello_plugin"


def _slugify(value: str, separator: str) -> str:
    """保留中文的锚点生成：去掉标点，空白换成分隔符，重复标题由 toc 自动追加序号。"""

    cleaned = re.sub(r"[^\w\s-]", "", value, flags=re.UNICODE).strip().lower()
    return re.sub(r"[\s_-]+", separator, cleaned) or "section"


def _load_document(path: Path = DOCUMENT_PATH) -> tuple[str, str]:
    """读取版本库内固定的 Markdown 文档，返回（正文 HTML, 左侧目录 HTML）。

    目录由 toc 扩展按 h2/h3 生成，每个标题带锚点，点击目录即可跳转到对应接口。
    """

    try:
        content = path.read_text(encoding="utf-8")
        # 文档内容来自受版本控制的固定文件，不接受请求参数拼接路径；模板只对
        # 这份受控内容使用 safe 输出，避免把 Markdown 标记原样显示给开发者。
        converter = markdown.Markdown(extensions=["extra", "codehilite", "toc"], extension_configs={"toc": {"toc_depth": "2-3", "slugify": _slugify}})
        return converter.convert(content), converter.toc
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
    }
    context["document_html"], context["document_toc"] = _load_document()
    context.update(build_tenant_navigation(request.url.path))
    context["section_nav_items"] = _docs_tabs("bridge")
    return templates.TemplateResponse(request, "developer_docs.html", context)


def _docs_tabs(active: str) -> list[dict[str, object]]:
    """文档中心的页签:接口文档与插件开发规范。"""

    return [
        {"label": "桥接接口", "href": "/developer/docs", "active": active == "bridge"},
        {"label": "插件开发规范", "href": "/developer/docs/plugins", "active": active == "plugins"},
    ]


@router.get("/developer/docs/plugins", response_class=HTMLResponse)
async def plugin_docs_page(request: Request):
    """展示插件开发规范,并提供标准示例插件的下载入口。"""

    principal = getattr(request.state, "current_user", None)
    if principal is None:
        return RedirectResponse(url="/login", status_code=303)

    context: dict[str, object] = {
        "request": request,
        "page_title": "插件开发规范",
        "page_intro": "按规范编写插件,交给平台管理员上传即可安装和升级。",
        "current_user": principal,
        "csrf_token": request.cookies.get(TENANT_CSRF_COOKIE, ""),
        "password_action": TENANT_PASSWORD_PATH,
        "logout_action": TENANT_LOGOUT_PATH,
        "template_download": "/developer/docs/plugins/hello_plugin.zip",
    }
    context["document_html"], context["document_toc"] = _load_document(PLUGIN_DOCUMENT_PATH)
    context.update(build_tenant_navigation(request.url.path))
    context["section_nav_items"] = _docs_tabs("plugins")
    return templates.TemplateResponse(request, "developer_docs.html", context)


def build_plugin_template_zip() -> bytes:
    """把标准示例插件目录打成 zip(顶层是 hello_plugin/),与后台上传安装的格式一致。"""

    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as archive:
        for file in sorted(PLUGIN_TEMPLATE_DIR.rglob("*")):
            relative = file.relative_to(PLUGIN_TEMPLATE_DIR)
            if file.is_file() and not file.is_symlink() and "__pycache__" not in relative.parts and file.suffix != ".pyc":
                archive.write(file, f"{PLUGIN_TEMPLATE_DIR.name}/{relative.as_posix()}")
    return buffer.getvalue()


@router.get("/developer/docs/plugins/hello_plugin.zip")
async def plugin_template_download(request: Request):
    """下载标准示例插件;和文档一样只对已登录的企业会话开放。"""

    if getattr(request.state, "current_user", None) is None:
        return RedirectResponse(url="/login", status_code=303)
    return Response(
        content=build_plugin_template_zip(),
        media_type="application/zip",
        headers={"Content-Disposition": 'attachment; filename="hello_plugin.zip"'},
    )
