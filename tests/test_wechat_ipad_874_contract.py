from __future__ import annotations

import inspect
from pathlib import Path
from unittest.mock import patch

from app.models.entities import Channel
from app.services.channel_adapters.wechat_ipad import WechatIpadAdapter
from app.services.channel_catalog import get_channel_type_definition
from app.services.wechat_ipad.client import WechatIpadApiClient
from app.services.wechat_ipad.contract import IPAD_874_CONTRACT


def _build_channel() -> Channel:
    """构造最小渠道对象，避免协议契约测试依赖数据库或租户上下文。"""

    return Channel(
        bot_id=1,
        name="iPad 874 契约测试",
        channel_type="wechat_ipad",
        api_base="http://127.0.0.1:9064",
    )


def test_ipad_874_contract_locks_verified_source_and_default_address() -> None:
    """协议版本必须指向本次核对过的源提交，避免无记录地漂移到未知实现。"""

    assert IPAD_874_CONTRACT.source_repository == "ipad-874（私有仓库，未公开）"
    assert IPAD_874_CONTRACT.source_revision == "d9d3def46f8b5793e4272ba713fcce24c7df6267"
    assert IPAD_874_CONTRACT.default_api_base == "http://127.0.0.1:9064"


def test_ipad_874_contract_uses_current_login_and_tool_endpoints_only() -> None:
    """关键端点必须与 874 Swagger 一致，并彻底排除已经下线的旧路径。"""

    endpoints = IPAD_874_CONTRACT.endpoints
    assert endpoints.login_get_qr == "/api/Login/LoginGetQR"
    assert endpoints.login_get_qr_car == "/api/Login/LoginGetQRCar"
    assert endpoints.login_check_qr == "/api/Login/LoginCheckQR"
    assert endpoints.login_awaken == "/api/Login/LoginAwaken"
    assert endpoints.login_twice_auto_auth == "/api/Login/LoginTwiceAutoAuth"
    assert endpoints.group_get_qrcode == "/api/Group/GetQRCode"
    assert endpoints.user_get_qrcode == "/api/User/GetQRCode"
    assert endpoints.tools_download_voice == "/api/Tools/DownloadVoice"
    assert endpoints.tools_download_video == "/api/Tools/DownloadVideo"
    assert endpoints.tools_update_step == "/api/Tools/UpdateStepNumberApi"
    assert endpoints.tools_set_proxy == "/api/Tools/setproxy"

    retired_endpoints = {
        "/api/Login/GetQR",
        "/api/Login/GetQRCar",
        "/api/Login/CheckQR",
        "/api/Login/Awaken",
        "/GetChatroomQRCode",
        "/GetMyQRCode",
        "/DownloadVoice",
        "/DownloadVideo",
        "/SetStep",
        "/SetProxy",
        "/CheckDatabaseOK",
    }
    assert retired_endpoints.isdisjoint(IPAD_874_CONTRACT.all_endpoints)


def test_ipad_874_client_uses_structured_query_and_awaken_body() -> None:
    """登录查询参数与唤醒请求体必须放在 874 控制器实际读取的位置。"""

    channel = _build_channel()
    client = WechatIpadApiClient()

    with patch.object(client, "request_json", return_value={"Success": True}) as request_json:
        client.check_qr(channel, uuid="uuid /+")
        request_json.assert_called_once_with(
            channel,
            IPAD_874_CONTRACT.endpoints.login_check_qr,
            query={"uuid": "uuid /+"},
        )

    with patch.object(client, "request_json", return_value={"Success": True}) as request_json:
        client.get_cache_info(channel, wxid="wxid_874")
        request_json.assert_called_once_with(
            channel,
            IPAD_874_CONTRACT.endpoints.login_get_cache_info,
            query={"wxid": "wxid_874"},
        )

    with patch.object(client, "request_json", return_value={"Success": True}) as request_json:
        client.awaken_login(
            channel,
            wxid="wxid_874",
            proxy={"host": "127.0.0.1", "port": "8899", "username": "proxy-user", "password": "proxy-pass"},
        )
        request_json.assert_called_once_with(
            channel,
            IPAD_874_CONTRACT.endpoints.login_awaken,
            body={
                "Wxid": "wxid_874",
                "Proxy": {
                    "ProxyIp": "127.0.0.1:8899",
                    "ProxyUser": "proxy-user",
                    "ProxyPassword": "proxy-pass",
                },
            },
        )


def test_ipad_874_request_json_percent_encodes_query_values() -> None:
    """查询参数统一交给标准 URL 编码器，不能再通过字符串 format 拼接用户值。"""

    class FakeResponse:
        status = 200

        def __enter__(self):
            return self

        def __exit__(self, exc_type, exc, traceback):
            return False

        def read(self) -> bytes:
            return b'{"Success": true}'

    channel = _build_channel()
    client = WechatIpadApiClient()
    with patch("app.services.wechat_ipad.client.urlrequest.urlopen", return_value=FakeResponse()) as urlopen:
        result = client.request_json(
            channel,
            IPAD_874_CONTRACT.endpoints.login_check_qr,
            query={"uuid": "uuid /+"},
        )

    assert result["Success"] is True
    requested_url = urlopen.call_args.args[0].full_url
    assert requested_url == "http://127.0.0.1:9064/api/Login/LoginCheckQR?uuid=uuid+%2F%2B"


def test_ipad_874_client_removes_endpoint_overrides_and_database_probe() -> None:
    """客户端只允许进入固定 874 契约，不再暴露旧端点覆盖或不存在的数据库探针。"""

    methods = (
        "get_qr_code",
        "check_qr",
        "heartbeat",
        "get_cache_info",
        "awaken_login",
        "twice_auto_auth",
        "log_out",
        "sync_message",
        "get_chatroom_info",
        "get_chatroom_member_detail",
    )
    for method_name in methods:
        assert "endpoint" not in inspect.signature(getattr(WechatIpadApiClient, method_name)).parameters
    assert not hasattr(WechatIpadApiClient, "check_database")


def test_ipad_874_tools_use_current_request_fields() -> None:
    """874 修改步数使用 Number 字段，代理结构直接复用协议定义的三项凭据。"""

    channel = _build_channel()
    client = WechatIpadApiClient()
    with patch.object(client, "request_json", return_value={"Success": True}) as request_json:
        client.set_step(channel, wxid="wxid_874", step_count=12000)
        request_json.assert_called_once_with(
            channel,
            IPAD_874_CONTRACT.endpoints.tools_update_step,
            body={"Wxid": "wxid_874", "Number": 12000},
        )

    with patch.object(client, "request_json", return_value={"Success": True}) as request_json:
        client.download_voice(
            channel,
            wxid="wxid_874",
            msg_id="12345",
            from_user_name="room-001@chatroom",
            bufid="99887766",
            length=3200,
        )
        request_json.assert_called_once_with(
            channel,
            IPAD_874_CONTRACT.endpoints.tools_download_voice,
            body={
                "Wxid": "wxid_874",
                "MsgId": 12345,
                "FromUserName": "room-001@chatroom",
                "Bufid": "99887766",
                "Length": 3200,
            },
        )

    with patch.object(client, "request_json", return_value={"Success": True}) as request_json:
        client.download_video(
            channel,
            wxid="wxid_874",
            msg_id="54321",
            to_wxid="wxid_sender",
            data_len=65536,
            start_pos=0,
            section_length=32768,
        )
        request_json.assert_called_once_with(
            channel,
            IPAD_874_CONTRACT.endpoints.tools_download_video,
            body={
                "Wxid": "wxid_874",
                "ToWxid": "wxid_sender",
                "MsgId": 54321,
                "DataLen": 65536,
                "Section": {"StartPos": 0, "DataLen": 32768},
                "CompressType": 0,
            },
        )


def test_ipad_874_group_and_contact_bodies_follow_swagger_field_names() -> None:
    """群二维码和联系人接口必须使用 874 模型字段，不能继续发送旧协议键名。"""

    channel = _build_channel()
    client = WechatIpadApiClient()
    with patch.object(client, "request_json", return_value={"Success": True}) as request_json:
        client.get_chatroom_qrcode(channel, wxid="wxid_874", chatroom_id="room-001@chatroom")
        request_json.assert_called_once_with(
            channel,
            IPAD_874_CONTRACT.endpoints.group_get_qrcode,
            body={"Wxid": "wxid_874", "QID": "room-001@chatroom"},
        )

    with patch.object(client, "request_json", return_value={"Success": True}) as request_json:
        client.get_contact_detail(
            channel,
            wxid="wxid_874",
            to_wxids=["wxid_a", "wxid_b"],
            chatroom_id="room-001@chatroom",
        )
        request_json.assert_called_once_with(
            channel,
            IPAD_874_CONTRACT.endpoints.friend_get_detail,
            body={"Wxid": "wxid_874", "Towxids": "wxid_a,wxid_b", "ChatRoom": "room-001@chatroom"},
        )


def test_ipad_874_voice_metadata_uses_xml_byte_length() -> None:
    """语音下载必须读取 XML 字节长度，不能把播放时长误发给 874 下载接口。"""

    metadata = WechatIpadAdapter()._extract_voice_info(
        {
            "MsgId": 12345,
            "FromUserName": {"string": "room-001@chatroom"},
            "Content": {
                "string": '<msg><voicemsg voicelength="5200" length="3062" bufid="99887766" /></msg>'
            },
        }
    )

    assert metadata == {
        "msg_id": "12345",
        "from_user_name": "room-001@chatroom",
        "bufid": "99887766",
        "length": "3062",
    }


def test_ipad_874_video_metadata_initializes_download_section_from_xml() -> None:
    """视频同步消息要直接生成 874 DownloadVideo 所需的总长度和首段范围。"""

    metadata = WechatIpadAdapter()._extract_video_info(
        {
            "MsgId": 54321,
            "FromUserName": {"string": "wxid_sender"},
            "Content": {"string": '<msg><videomsg length="65536" playlength="8" /></msg>'},
        }
    )

    assert metadata == {
        "msg_id": "54321",
        "to_wxid": "wxid_sender",
        "data_len": "65536",
        "start_pos": "0",
        "section_length": "65536",
        "compress_type": "0",
    }


def test_ipad_874_link_reply_uses_send_app_type_five() -> None:
    """平台链接卡片必须映射到 874 的标准 SendApp，而不是依赖插件拼微信 XML。"""

    request = WechatIpadAdapter()._build_request(
        "link",
        "wxid_bot",
        "room-001@chatroom",
        {
            "title": "活动中心",
            "desc": "A&B 专场",
            "url": "https://tenant.example.test/activity?a=1&b=2",
            "thumb_url": "https://tenant.example.test/activity.png",
        },
    )

    assert request is not None
    assert request["endpoint"] == IPAD_874_CONTRACT.endpoints.message_send_app
    assert request["body"]["Type"] == 5
    assert "<type>5</type>" in request["body"]["Xml"]
    assert "A&amp;B" in request["body"]["Xml"]
    assert "a=1&amp;b=2" in request["body"]["Xml"]


def test_wechat_ipad_catalog_uses_platform_managed_endpoint_without_unused_token() -> None:
    """租户目录不暴露平台地址或无效 Token，只声明端点由平台统一托管。"""

    definition = get_channel_type_definition("wechat_ipad")
    fields = {field.name: field for field in definition.config_fields}

    assert "api_base" not in fields
    assert "bot_token" not in fields
    assert definition.platform_managed_endpoint is True


def test_tenant_routes_remove_retired_channel_proxy_and_technical_login_actions() -> None:
    """租户主路由只保留业务化登录会话，不得继续注册渠道长期代理和旧技术动作。"""

    source = Path("app/main.py").read_text(encoding="utf-8")

    assert "extract_" + "wechat_ipad_proxy_form" not in source
    assert "\n    proxy_host: str = Form" not in source
    for retired_route in (
        "wechat-ipad/" + "login-init",
        "wechat-ipad/" + "status-sync",
        "wechat-ipad/" + "qrcode-refresh",
        "wechat-ipad/" + "logout",
        "wechat-ipad/" + "reconnect",
    ):
        assert retired_route not in source
