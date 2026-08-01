"""SaaS 浏览器端路由入口。

平台管理员和租户员工使用完全独立的 Router。主应用只需要从本模块导入并注册，
不应再把两类登录逻辑重新写回 ``app.main``，这样后续平台控制台与租户工作台可以
分别演进，同时继续共享底层密码、数据库会话和 CSRF 服务。
"""

from app.web.platform_auth import router as platform_auth_router
from app.web.platform_admin import router as platform_admin_router
from app.web.tenant_auth import router as tenant_auth_router

__all__ = ["platform_admin_router", "platform_auth_router", "tenant_auth_router"]
