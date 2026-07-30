from __future__ import annotations

from urllib.parse import quote

from fastapi import APIRouter, Form, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy import select

from app.core.db import SessionLocal
from app.core.time import now_shanghai
from app.models.entities import Channel, OpenApiApp
from app.services.auth import has_required_role
from app.services.open_api_apps import OpenApiAppInput, list_authorized_channel_ids, list_open_api_apps, upsert_open_api_app


router = APIRouter(tags=["open-api-admin"])
templates = Jinja2Templates(directory="app/templates")


@router.get("/open-api/apps", response_class=HTMLResponse)
async def open_api_apps_page(request: Request):
    """开放接口应用配置页。"""

    _enforce_admin_role(request)
    db = SessionLocal()
    try:
        context = _build_page_context(
            request,
            notice=request.query_params.get("notice", ""),
            notice_tone=request.query_params.get("tone", ""),
        )
        apps = list_open_api_apps(db)
        channels = db.scalars(select(Channel).order_by(Channel.id.desc())).all()
        editing_app = _load_editing_app(db, request)
        context["apps"] = apps
        context["channels"] = channels
        context["editing_app"] = editing_app
        context["channel_name_map"] = {int(channel.id): f"{channel.name} #{channel.id}" for channel in channels}
        context["app_authorized_channel_ids"] = {int(app.id): list_authorized_channel_ids(db, app.id) for app in apps}
        context["editing_authorized_channel_ids"] = list_authorized_channel_ids(db, editing_app.id) if editing_app else []
        return templates.TemplateResponse(request, "open_api_apps.html", context)
    finally:
        db.close()


@router.post("/open-api/apps")
async def open_api_app_save_action(
    request: Request,
    app_id: int = Form(0),
    name: str = Form(...),
    app_key: str = Form(...),
    app_secret: str = Form(""),
    default_channel_id: int = Form(...),
    status: str = Form("active"),
    webhook_url: str = Form(""),
    webhook_secret: str = Form(""),
    authorized_channel_ids: list[int] = Form([]),
):
    """创建或更新开放接口应用。"""

    _enforce_admin_role(request)
    db = SessionLocal()
    try:
        app = upsert_open_api_app(
            db,
            app_id,
            OpenApiAppInput(
                name=name,
                app_key=app_key,
                app_secret=app_secret,
                default_channel_id=default_channel_id,
                status=status,
                webhook_url=webhook_url,
                webhook_secret=webhook_secret,
                authorized_channel_ids=tuple(authorized_channel_ids or ()),
            ),
        )
        return RedirectResponse(url="/open-api/apps?notice=" + quote(f"开放应用 {app.name} 已保存"), status_code=303)
    except Exception as exc:
        return RedirectResponse(url="/open-api/apps?notice=" + quote(f"开放应用保存失败：{exc}") + "&tone=danger", status_code=303)
    finally:
        db.close()


def _enforce_admin_role(request: Request):
    user = getattr(request.state, "current_user", None)
    if not user:
        raise HTTPException(status_code=401, detail="Authentication required")
    if not has_required_role(user, "admin"):
        raise HTTPException(status_code=403, detail="Permission denied")
    return user


def _load_editing_app(db, request: Request) -> OpenApiApp | None:
    """根据查询参数读取正在编辑的开放应用。

    列表页只负责选择要编辑的记录；表单集中在页面上方，避免在表格行内堆叠大量输入框。
    """

    try:
        edit_id = int(request.query_params.get("edit_id") or 0)
    except ValueError:
        return None
    if edit_id <= 0:
        return None
    return db.get(OpenApiApp, edit_id)


def _build_page_context(request: Request, *, notice: str = "", notice_tone: str = "") -> dict[str, object]:
    """构建开放接口页面模板上下文。

    这里不反向导入 app.main.render，避免路由模块和主应用之间出现循环依赖。
    """

    primary_nav_items = [
        {"label": "总览", "href": "/", "active": False},
        {"label": "接入", "href": "/channels", "active": False},
        {"label": "机器人", "href": "/bots", "active": False},
        {"label": "插件", "href": "/plugins", "active": False},
        {"label": "消息", "href": "/conversations", "active": False},
        {"label": "开放接口", "href": "/open-api/apps", "active": True},
        {"label": "排障", "href": "/monitoring", "active": False},
    ]
    return {
        "request": request,
        "page_title": "开放接口应用",
        "current_user": getattr(request.state, "current_user", None),
        "app_started_at": now_shanghai(),
        "notice": notice,
        "notice_tone": notice_tone or ("danger" if "失败" in str(notice or "") else "info"),
        "nav_items": primary_nav_items,
        "primary_nav_items": primary_nav_items,
        "secondary_nav_items": [],
        "secondary_nav_active": False,
        "section_nav_items": [
            {"label": "应用配置", "href": "/open-api/apps", "active": True},
        ],
        "module_links": {},
    }
