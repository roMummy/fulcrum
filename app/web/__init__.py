"""SaaS 浏览器端路由入口。

平台管理员和租户员工使用完全独立的 Router。主应用只需要从本模块导入并注册，
不应再把两类登录逻辑重新写回 ``app.main``，这样后续平台控制台与租户工作台可以
分别演进，同时继续共享底层密码、数据库会话和 CSRF 服务。
"""

from app.web.group_triggers import router as group_triggers_router
from app.web.platform_auth import router as platform_auth_router
from app.web.platform_admin import router as platform_admin_router
from app.web.platform_billing import router as platform_billing_router
from app.web.platform_operations import router as platform_operations_router
from app.web.platform_plugins import router as platform_plugins_router
from app.web.public_site import router as public_site_router
from app.web.billing import router as billing_router
from app.web.developer_docs import router as developer_docs_router
from app.web.provider_nodes import router as provider_nodes_router
from app.web.open_api_apps import router as open_api_apps_router
from app.web.tenant_auth import router as tenant_auth_router
from app.web.tenant_admin import router as tenant_admin_router

__all__ = [
    "group_triggers_router",
    "platform_admin_router",
    "platform_auth_router",
    "platform_billing_router",
    "platform_operations_router",
    "platform_plugins_router",
    "billing_router",
    "developer_docs_router",
    "provider_nodes_router",
    "open_api_apps_router",
    "public_site_router",
    "tenant_admin_router",
    "tenant_auth_router",
]
