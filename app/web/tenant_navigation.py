"""企业租户后台的统一主导航与模块内导航。"""

from __future__ import annotations

import re


def _is_active(path: str, prefixes: tuple[str, ...]) -> bool:
    """按稳定业务入口判断激活态，避免不同 Router 各自复制菜单并产生漂移。"""

    return any(path == prefix or path.startswith(f"{prefix}/") for prefix in prefixes)


# 单群页面路径为 /channels/{渠道}/chatrooms[/{群}],属于“群管理”而不是“账号接入”。
_CHATROOM_PATH = re.compile(r"^/channels/\d+/chatrooms(/|$)")


def _item(label: str, href: str, path: str, prefixes: tuple[str, ...]) -> dict[str, str | bool]:
    """创建模板可直接消费的导航项。"""

    return {"label": label, "href": href, "active": _is_active(path, prefixes)}


def build_tenant_navigation(path: str) -> dict[str, object]:
    """返回企业租户后台唯一允许展示的信息架构。"""

    normalized_path = str(path or "/").strip() or "/"
    in_chatroom_page = bool(_CHATROOM_PATH.match(normalized_path))
    # 主导航只放日常运营要用的几项:群、好友、消息、朋友圈是核心资产,各自只有一个入口。
    primary_nav_items = [
        _item("总览", "/dashboard", normalized_path, ("/dashboard",)),
        {"label": "群管理", "href": "/chatrooms", "active": in_chatroom_page or _is_active(normalized_path, ("/chatrooms",))},
        _item("好友", "/contacts", normalized_path, ("/contacts",)),
        _item("消息", "/messages", normalized_path, ("/messages",)),
        _item("朋友圈", "/moments", normalized_path, ("/moments",)),
        _item("积分", "/points", normalized_path, ("/points",)),
        _item("插件", "/plugins", normalized_path, ("/plugins",)),
        {
            "label": "账号接入",
            "href": "/channels",
            "active": not in_chatroom_page and _is_active(normalized_path, ("/channels", "/provider-nodes")),
        },
    ]
    # 低频入口收进右上角账号菜单,避免主导航堆满。
    account_nav_items = [
        _item("AI 助手", "/tenant/ai", normalized_path, ("/tenant/ai",)),
        _item("企业设置", "/tenant/users", normalized_path, ("/tenant",)),
        _item("套餐与订单", "/account/billing", normalized_path, ("/account",)),
        _item("开发文档", "/developer/docs", normalized_path, ("/developer/docs",)),
    ]

    # 企业微信客户端由账号接入步骤进入，不再作为与账号清单并列的技术模块展示。
    section_nav_items: list[dict[str, str | bool]] = []
    if _is_active(normalized_path, ("/account",)):
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
            _item("AI 助手", "/tenant/ai", normalized_path, ("/tenant/ai",)),
            _item("开放应用", "/tenant/open-apps", normalized_path, ("/tenant/open-apps",)),
            _item("本地代理连接器", "/tenant/connectors", normalized_path, ("/tenant/connectors",)),
            _item("订阅信息", "/tenant/subscription", normalized_path, ("/tenant/subscription",)),
        ]

    return {
        "nav_items": primary_nav_items,
        "account_nav_items": account_nav_items,
        "primary_nav_items": primary_nav_items,
        "secondary_nav_items": [],
        "secondary_nav_active": False,
        "section_nav_items": section_nav_items,
    }
