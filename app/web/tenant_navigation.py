"""企业租户后台的统一主导航与模块内导航。"""

from __future__ import annotations


def _is_active(path: str, prefixes: tuple[str, ...]) -> bool:
    """按稳定业务入口判断激活态，避免不同 Router 各自复制菜单并产生漂移。"""

    return any(path == prefix or path.startswith(f"{prefix}/") for prefix in prefixes)


def _item(label: str, href: str, path: str, prefixes: tuple[str, ...]) -> dict[str, str | bool]:
    """创建模板可直接消费的导航项。"""

    return {"label": label, "href": href, "active": _is_active(path, prefixes)}


def build_tenant_navigation(path: str) -> dict[str, object]:
    """返回企业租户后台唯一允许展示的信息架构。"""

    normalized_path = str(path or "/").strip() or "/"
    primary_nav_items = [
        _item("总览", "/dashboard", normalized_path, ("/dashboard",)),
        _item("账号接入", "/channels", normalized_path, ("/channels", "/provider-nodes")),
        _item("运营能力", "/plugins", normalized_path, ("/plugins",)),
        _item("消息", "/conversations", normalized_path, ("/conversations", "/messages")),
        _item("套餐", "/account/billing", normalized_path, ("/account",)),
        _item("企业设置", "/tenant/users", normalized_path, ("/tenant",)),
    ]

    # 企业微信客户端由账号接入步骤进入，不再作为与账号清单并列的技术模块展示。
    section_nav_items: list[dict[str, str | bool]] = []
    if _is_active(normalized_path, ("/plugins",)):
        section_nav_items = [
            {
                "label": "能力开关",
                "href": "/plugins",
                "active": normalized_path == "/plugins" or (
                    normalized_path.startswith("/plugins/")
                    and not normalized_path.startswith("/plugins/runtime")
                ),
            },
            _item("运行状态", "/plugins/runtime", normalized_path, ("/plugins/runtime",)),
        ]
    elif _is_active(normalized_path, ("/conversations", "/messages")):
        section_nav_items = [
            _item("会话", "/conversations", normalized_path, ("/conversations",)),
            _item("消息记录", "/messages", normalized_path, ("/messages",)),
        ]
    elif _is_active(normalized_path, ("/account",)):
        section_nav_items = [
            _item(
                "套餐与额度",
                "/account/billing",
                normalized_path,
                ("/account/billing", "/account/onboarding"),
            ),
            _item("订单", "/account/orders", normalized_path, ("/account/orders",)),
        ]
    elif _is_active(normalized_path, ("/tenant",)):
        section_nav_items = [
            _item("员工管理", "/tenant/users", normalized_path, ("/tenant/users",)),
            _item("订阅信息", "/tenant/subscription", normalized_path, ("/tenant/subscription",)),
        ]

    return {
        "nav_items": primary_nav_items,
        "primary_nav_items": primary_nav_items,
        "secondary_nav_items": [],
        "secondary_nav_active": False,
        "section_nav_items": section_nav_items,
    }
