"""开放 API 能力目录。

该模块把微信 iPad 874 的内部端点映射为稳定的业务能力编码。外部调用方只看
业务能力，不能依赖 874 的原始路径、字段名或设备控制接口；渠道适配器仍然可以
在内部使用固定契约完成真正的协议调用。
"""

from __future__ import annotations

from dataclasses import dataclass

from app.services.wechat_ipad.contract import IPAD_874_CONTRACT


@dataclass(frozen=True, slots=True)
class OpenApiCapability:
    """开放能力的稳定元数据，供 API、后台页面和开发文档共同使用。"""

    code: str
    title: str
    description: str
    operation: str
    channel_types: tuple[str, ...]
    provider_methods: tuple[str, ...] = ()
    status: str = "available"


def _endpoint(*names: str) -> tuple[str, ...]:
    """从固定 874 契约读取端点，避免能力目录复制协议路径后发生漂移。"""

    endpoints = IPAD_874_CONTRACT.endpoints
    return tuple(str(getattr(endpoints, name)) for name in names)


_WECHAT_IPAD = ("wechat_ipad",)


# 874 的登录、心跳、登出、代理和步数工具不进入此目录。它们属于平台内部
# 连接生命周期，不能被外部业务系统直接调用。
_CAPABILITIES: tuple[OpenApiCapability, ...] = (
    OpenApiCapability(
        code="account.read",
        title="读取账号资料和状态",
        description="读取已经接入账号的业务资料、在线状态和最近运行摘要。",
        operation="read",
        channel_types=_WECHAT_IPAD,
        provider_methods=_endpoint("user_get_profile"),
    ),
    OpenApiCapability(
        code="contact.read",
        title="读取联系人",
        description="查询联系人列表和联系人详情。",
        operation="read",
        channel_types=_WECHAT_IPAD,
        provider_methods=_endpoint("friend_get_list", "friend_get_detail"),
    ),
    OpenApiCapability(
        code="friend.request.accept",
        title="处理好友申请",
        description="通过业务接口接受已经进入账号的好友申请。",
        operation="write",
        channel_types=_WECHAT_IPAD,
        provider_methods=_endpoint("friend_pass_verify"),
    ),
    OpenApiCapability(
        code="group.read",
        title="读取群信息",
        description="查询群名称、备注、状态和成员数量等群资料。",
        operation="read",
        channel_types=_WECHAT_IPAD,
        provider_methods=_endpoint("group_get_info", "group_get_info_detail"),
    ),
    OpenApiCapability(
        code="group.members.read",
        title="读取群成员",
        description="查询指定群的成员外部标识和成员资料。",
        operation="read",
        channel_types=_WECHAT_IPAD,
        provider_methods=_endpoint("group_get_member_detail"),
    ),
    OpenApiCapability(
        code="group.members.write",
        title="新增或邀请群成员",
        description="按渠道支持情况新增或邀请群成员。",
        operation="write",
        channel_types=_WECHAT_IPAD,
        provider_methods=_endpoint("group_add_member", "group_invite_member"),
    ),
    OpenApiCapability(
        code="group.qrcode.read",
        title="读取群二维码",
        description="生成指定群的业务二维码地址或临时媒体引用。",
        operation="read",
        channel_types=_WECHAT_IPAD,
        provider_methods=_endpoint("group_get_qrcode"),
    ),
    OpenApiCapability(
        code="message.read",
        title="同步消息",
        description="按游标读取已授权账号的入站消息增量。",
        operation="read",
        channel_types=_WECHAT_IPAD,
        provider_methods=_endpoint("message_sync"),
    ),
    OpenApiCapability(
        code="message.send.text",
        title="发送文本消息",
        description="向私聊或群聊发送文本消息。",
        operation="write",
        channel_types=_WECHAT_IPAD,
        provider_methods=_endpoint("message_send_text"),
    ),
    OpenApiCapability(
        code="message.send.rich",
        title="发送富媒体消息",
        description="发送图片、语音、视频、表情、链接、名片、小程序和应用消息。",
        operation="write",
        channel_types=_WECHAT_IPAD,
        provider_methods=_endpoint(
            "message_upload_image",
            "message_send_video",
            "message_send_voice",
            "message_share_link",
            "message_send_emoji",
            "message_share_card",
            "message_send_app",
            "message_send_cdn_image",
            "message_send_cdn_video",
        ),
    ),
    OpenApiCapability(
        code="message.revoke",
        title="撤回消息",
        description="撤回当前开放应用创建且渠道仍允许撤回的消息任务。",
        operation="write",
        channel_types=_WECHAT_IPAD,
        provider_methods=_endpoint("message_revoke"),
    ),
    OpenApiCapability(
        code="media.upload",
        title="上传媒体",
        description="上传文件、图片、语音或视频并获得平台媒体引用。",
        operation="write",
        channel_types=_WECHAT_IPAD,
        provider_methods=_endpoint("message_upload_image", "message_send_cdn_file"),
    ),
    OpenApiCapability(
        code="media.download",
        title="下载媒体",
        description="下载已授权消息中的图片、语音、文件或视频。",
        operation="read",
        channel_types=_WECHAT_IPAD,
        provider_methods=_endpoint(
            "tools_download_image",
            "tools_download_voice",
            "tools_download_file",
            "tools_download_video",
        ),
    ),
    OpenApiCapability(
        code="webhook.receive",
        title="接收开放事件",
        description="接收入站消息、发送结果、账号状态和任务状态事件。",
        operation="read",
        channel_types=_WECHAT_IPAD,
    ),
)


def list_open_api_capabilities(*, channel_type: str | None = None) -> tuple[OpenApiCapability, ...]:
    """返回按渠道过滤后的稳定开放能力目录。"""

    normalized = str(channel_type or "").strip()
    if not normalized:
        return _CAPABILITIES
    return tuple(item for item in _CAPABILITIES if normalized in item.channel_types)


def get_open_api_capability(code: str) -> OpenApiCapability | None:
    """按业务能力编码查找能力；未知编码返回空值供调用方生成稳定错误。"""

    normalized = str(code or "").strip()
    return next((item for item in _CAPABILITIES if item.code == normalized), None)

