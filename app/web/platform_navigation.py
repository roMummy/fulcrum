"""平台后台顶级导航和系统运维二级导航的唯一构造入口。"""

from __future__ import annotations


_PLATFORM_PRIMARY_NAVIGATION = (
    ("dashboard", "平台概览", "/platform/dashboard"),
    ("tenants", "租户管理", "/platform/tenants"),
    ("plans", "套餐管理", "/platform/plans"),
    ("billing", "订单审核", "/platform/billing/orders"),
    ("plugins", "插件管理", "/platform/plugins"),
    ("users", "平台员工", "/platform/users"),
    ("operations", "系统运维", "/platform/operations"),
)

_PLATFORM_OPERATIONS_NAVIGATION = (
    ("operations", "运行监控", "/platform/operations"),
    ("system", "服务器与数据库", "/platform/system"),
    ("protocol_services", "协议服务", "/platform/protocol-services"),
    ("proxy_pool", "代理池", "/platform/proxy-pool"),
    ("logs", "全局日志", "/platform/logs"),
)


def build_platform_primary_navigation(active: str) -> list[dict[str, object]]:
    """生成平台顶级导航，基础设施配置统一归入系统运维而不单独占位。"""

    normalized_active = str(active or "").strip()
    return [
        {
            "label": label,
            "href": href,
            "active": key == normalized_active,
        }
        for key, label, href in _PLATFORM_PRIMARY_NAVIGATION
    ]


def build_platform_operations_navigation(active: str) -> list[dict[str, object]]:
    """生成系统运维二级入口，并保证当前配置页只有一个激活项。"""

    normalized_active = str(active or "").strip()
    return [
        {
            "label": label,
            "href": href,
            "active": key == normalized_active,
        }
        for key, label, href in _PLATFORM_OPERATIONS_NAVIGATION
    ]
