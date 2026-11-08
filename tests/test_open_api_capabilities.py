from __future__ import annotations

from app.services.open_api_capabilities import list_open_api_capabilities


def test_open_api_capabilities_expose_874_business_features_only() -> None:
    """开放能力必须覆盖 874 非登录业务功能，并排除登录与代理控制。"""

    capabilities = {item.code: item for item in list_open_api_capabilities()}

    assert {
        "account.read",
        "contact.read",
        "friend.request.accept",
        "group.read",
        "group.members.read",
        "group.members.write",
        "group.qrcode.read",
        "message.read",
        "message.send.text",
        "message.send.rich",
        "message.revoke",
        "media.upload",
        "media.download",
        "webhook.receive",
    }.issubset(capabilities)

    exposed_methods = {method for item in capabilities.values() for method in item.provider_methods}
    assert "/api/Login/LoginGetQR" not in exposed_methods
    assert "/api/Login/HeartBeat" not in exposed_methods
    assert "/api/Tools/setproxy" not in exposed_methods
    assert all(item.channel_types == ("wechat_ipad",) for item in capabilities.values())


def test_open_api_capability_items_have_stable_metadata() -> None:
    """能力目录字段固定，页面和外部客户端不能依赖协议内部类名。"""

    items = list_open_api_capabilities()

    assert items
    assert len({item.code for item in items}) == len(items)
    assert all(item.title and item.description for item in items)
    assert all(item.operation in {"read", "write"} for item in items)
    assert all(item.status in {"available", "planned"} for item in items)
