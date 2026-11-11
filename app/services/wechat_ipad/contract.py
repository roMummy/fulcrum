"""微信 iPad 874 协议的固定 HTTP 契约。

该模块只描述已经核对过的外部接口，不读取渠道配置，也不承载登录状态。
所有运行代码必须从这里取得端点，防止旧协议路径通过隐藏配置重新进入主流程。
"""

from __future__ import annotations

from dataclasses import dataclass, fields


@dataclass(frozen=True, slots=True)
class Ipad874Endpoints:
    """按 874 Swagger 业务分组保存端点，字段名直接表达调用目的。"""

    # 登录接口中只有二维码取码和唤醒使用 JSON 请求体，其余状态类接口使用查询参数。
    login_get_qr: str = "/api/Login/LoginGetQR"
    login_get_qr_car: str = "/api/Login/LoginGetQRCar"
    login_check_qr: str = "/api/Login/LoginCheckQR"
    login_heartbeat: str = "/api/Login/HeartBeat"
    login_heartbeat_long: str = "/api/Login/HeartBeatLong"
    login_get_cache_info: str = "/api/Login/GetCacheInfo"
    login_awaken: str = "/api/Login/LoginAwaken"
    login_twice_auto_auth: str = "/api/Login/LoginTwiceAutoAuth"
    login_logout: str = "/api/Login/LogOut"

    # 消息端点保持平台通用发送能力，具体请求体由协议客户端统一构造。
    message_sync: str = "/api/Msg/Sync"
    message_revoke: str = "/api/Msg/Revoke"
    message_send_text: str = "/api/Msg/SendTxt"
    message_upload_image: str = "/api/Msg/UploadImg"
    message_send_video: str = "/api/Msg/SendVideo"
    message_send_voice: str = "/api/Msg/SendVoice"
    message_share_link: str = "/api/Msg/ShareLink"
    message_send_emoji: str = "/api/Msg/SendEmoji"
    message_share_card: str = "/api/Msg/ShareCard"
    message_send_app: str = "/api/Msg/SendApp"
    message_send_cdn_file: str = "/api/Msg/SendCDNFile"
    message_send_cdn_image: str = "/api/Msg/SendCDNImg"
    message_send_cdn_video: str = "/api/Msg/SendCDNVideo"

    # 群聊与联系人端点继续服务现有通用群管理、联系人同步和账号资料能力。
    group_get_info: str = "/api/Group/GetChatRoomInfo"
    group_get_member_detail: str = "/api/Group/GetChatRoomMemberDetail"
    group_get_info_detail: str = "/api/Group/GetChatRoomInfoDetail"
    group_add_member: str = "/api/Group/AddChatRoomMember"
    group_invite_member: str = "/api/Group/InviteChatRoomMember"
    group_get_qrcode: str = "/api/Group/GetQRCode"
    friend_pass_verify: str = "/api/Friend/PassVerify"
    friend_get_detail: str = "/api/Friend/GetContractDetail"
    friend_get_list: str = "/api/Friend/GetContractList"
    user_get_profile: str = "/api/User/GetContractProfile"
    user_get_qrcode: str = "/api/User/GetQRCode"

    # 工具端点全部位于 /api/Tools；874 已删除旧版根路径工具接口和数据库探针。
    tools_download_image: str = "/api/Tools/CdnDownloadImage"
    tools_download_voice: str = "/api/Tools/DownloadVoice"
    tools_download_file: str = "/api/Tools/DownloadFile"
    tools_download_video: str = "/api/Tools/DownloadVideo"
    tools_update_step: str = "/api/Tools/UpdateStepNumberApi"
    tools_set_proxy: str = "/api/Tools/setproxy"
    friend_circle_upload: str = "/api/FriendCircle/Upload"

    @property
    def all(self) -> frozenset[str]:
        """返回不可变端点集合，供测试和运行诊断检查契约完整性。"""

        return frozenset(str(getattr(self, item.name)) for item in fields(self))


@dataclass(frozen=True, slots=True)
class Ipad874Contract:
    """记录当前产品唯一允许使用的 874 来源、默认地址与端点集合。"""

    source_repository: str
    source_revision: str
    default_api_base: str
    endpoints: Ipad874Endpoints

    @property
    def all_endpoints(self) -> frozenset[str]:
        """对外暴露只读端点集合，避免调用方直接遍历数据类内部结构。"""

        return self.endpoints.all


# 固定源提交用于审计本次 Swagger 映射；后续升级必须显式更新契约和对应测试。
IPAD_874_CONTRACT = Ipad874Contract(
    source_repository="ipad-874（私有仓库，未公开）",
    source_revision="d9d3def46f8b5793e4272ba713fcce24c7df6267",
    default_api_base="http://127.0.0.1:9064",
    endpoints=Ipad874Endpoints(),
)
