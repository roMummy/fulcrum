from __future__ import annotations

import base64
import binascii
import hashlib
import json
import re
import string
from random import choice
from typing import Any
from urllib.error import HTTPError, URLError
from urllib import request as urlrequest

from app.models.entities import Channel


DEFAULT_TIMEOUT_SECONDS = 12


class WechatIpadApiClient:
    DEFAULT_LOGIN_INIT_ENDPOINT = "/api/Login/GetQR"
    DEFAULT_LOGIN_INIT_CAR_ENDPOINT = "/api/Login/GetQRCar"
    DEFAULT_CHECK_QR_ENDPOINT = "/api/Login/CheckQR?uuid={uuid}"
    DEFAULT_HEARTBEAT_ENDPOINT = "/api/Login/HeartBeat?wxid={wxid}"
    DEFAULT_HEARTBEAT_LONG_ENDPOINT = "/api/Login/HeartBeatLong?wxid={wxid}"
    DEFAULT_CACHE_INFO_ENDPOINT = "/api/Login/GetCacheInfo"
    DEFAULT_AWAKEN_ENDPOINT = "/api/Login/Awaken?wxid={wxid}"
    DEFAULT_TWICE_AUTO_AUTH_ENDPOINT = "/api/Login/TwiceAutoAuth?wxid={wxid}"
    DEFAULT_LOGOUT_ENDPOINT = "/api/Login/LogOut?wxid={wxid}"
    DEFAULT_MESSAGE_SYNC_ENDPOINT = "/api/Msg/Sync"
    DEFAULT_MESSAGE_REVOKE_ENDPOINT = "/api/Msg/Revoke"
    DEFAULT_SEND_TEXT_ENDPOINT = "/api/Msg/SendTxt"
    DEFAULT_UPLOAD_IMAGE_ENDPOINT = "/api/Msg/UploadImg"
    DEFAULT_SEND_VIDEO_ENDPOINT = "/api/Msg/SendVideo"
    DEFAULT_SEND_VOICE_ENDPOINT = "/api/Msg/SendVoice"
    DEFAULT_SHARE_LINK_ENDPOINT = "/api/Msg/ShareLink"
    DEFAULT_SEND_EMOJI_ENDPOINT = "/api/Msg/SendEmoji"
    DEFAULT_SHARE_CARD_ENDPOINT = "/api/Msg/ShareCard"
    DEFAULT_SEND_APP_ENDPOINT = "/api/Msg/SendApp"
    DEFAULT_SEND_CDN_FILE_ENDPOINT = "/api/Msg/SendCDNFile"
    DEFAULT_SEND_CDN_IMAGE_ENDPOINT = "/api/Msg/SendCDNImg"
    DEFAULT_SEND_CDN_VIDEO_ENDPOINT = "/api/Msg/SendCDNVideo"
    DEFAULT_CHATROOM_INFO_ENDPOINT = "/api/Group/GetChatRoomInfo"
    DEFAULT_CHATROOM_MEMBER_DETAIL_ENDPOINT = "/api/Group/GetChatRoomMemberDetail"
    DEFAULT_CHATROOM_INFO_DETAIL_ENDPOINT = "/api/Group/GetChatRoomInfoDetail"
    DEFAULT_CHATROOM_ADD_MEMBER_ENDPOINT = "/api/Group/AddChatRoomMember"
    DEFAULT_CHATROOM_INVITE_MEMBER_ENDPOINT = "/api/Group/InviteChatRoomMember"
    DEFAULT_CHATROOM_QRCODE_ENDPOINT = "/GetChatroomQRCode"
    DEFAULT_ACCEPT_FRIEND_ENDPOINT = "/api/Friend/PassVerify"
    DEFAULT_CONTACT_DETAIL_ENDPOINT = "/api/Friend/GetContractDetail"
    DEFAULT_CONTACT_LIST_ENDPOINT = "/api/Friend/GetContractList"
    DEFAULT_USER_PROFILE_ENDPOINT = "/api/User/GetContractProfile?wxid={wxid}"
    DEFAULT_MY_QRCODE_ENDPOINT = "/GetMyQRCode"
    DEFAULT_TOOL_DOWNLOAD_IMAGE_ENDPOINT = "/api/Tools/CdnDownloadImage"
    DEFAULT_TOOL_DOWNLOAD_VOICE_ENDPOINT = "/DownloadVoice"
    DEFAULT_TOOL_DOWNLOAD_FILE_ENDPOINT = "/api/Tools/DownloadFile"
    DEFAULT_TOOL_DOWNLOAD_VIDEO_ENDPOINT = "/DownloadVideo"
    DEFAULT_FRIEND_CIRCLE_UPLOAD_ENDPOINT = "/api/FriendCircle/Upload"
    DEFAULT_SET_STEP_ENDPOINT = "/SetStep"
    DEFAULT_SET_PROXY_ENDPOINT = "/SetProxy"
    DEFAULT_CHECK_DATABASE_ENDPOINT = "/CheckDatabaseOK"
    ERROR_CODE_MESSAGES = {
        -1: "parameter_error",
        -2: "provider_error",
        -3: "marshalling_error",
        -4: "unmarshalling_error",
        -5: "mmtls_init_error",
        -6: "packet_length_error",
        -7: "already_logged_out",
        -8: "link_expired",
        -9: "parse_packet_error",
        -10: "database_error",
        -11: "login_abnormal",
        -12: "rate_limited",
        -13: "upload_failed",
    }

    @staticmethod
    def create_device_name() -> str:
        first_names = [
            "Oliver", "Emma", "Liam", "Ava", "Noah", "Sophia", "Elijah", "Isabella",
            "James", "Mia", "William", "Amelia", "Benjamin", "Harper", "Lucas", "Evelyn",
            "Henry", "Abigail", "Alexander", "Ella", "Jackson", "Scarlett", "Sebastian",
            "Grace", "Aiden", "Chloe", "Matthew", "Zoey", "Samuel", "Lily", "David",
            "Aria", "Joseph", "Riley", "Carter", "Nora", "Owen", "Luna", "Daniel",
            "Sofia", "Gabriel", "Ellie", "Matthew", "Avery", "Isaac", "Mila", "Leo",
            "Julian", "Layla",
        ]
        last_names = [
            "Smith", "Johnson", "Williams", "Brown", "Jones", "Garcia", "Miller", "Davis",
            "Rodriguez", "Martinez", "Hernandez", "Lopez", "Gonzalez", "Wilson", "Anderson",
            "Thomas", "Taylor", "Moore", "Jackson", "Martin", "Lee", "Perez", "Thompson",
            "White", "Harris", "Sanchez", "Clark", "Ramirez", "Lewis", "Robinson", "Walker",
            "Young", "Allen", "King", "Wright", "Scott", "Torres", "Nguyen", "Hill",
            "Flores", "Green", "Adams", "Nelson", "Baker", "Hall", "Rivera", "Campbell",
            "Mitchell", "Carter", "Roberts", "Gomez", "Phillips", "Evans",
        ]
        return choice(first_names) + " " + choice(last_names) + "'s Pad"

    @staticmethod
    def create_device_id(seed: str = "") -> str:
        source = seed if seed not in ("", "string") else "".join(choice(string.ascii_letters) for _ in range(15))
        return "49" + hashlib.md5(source.encode("utf-8")).hexdigest()[2:]

    def request_json(
        self,
        channel: Channel,
        endpoint: str,
        *,
        body: dict[str, Any] | None = None,
        method: str = "POST",
    ) -> dict[str, Any]:
        target_url = f"{channel.api_base.rstrip('/')}{endpoint}"
        if body is None:
            req = urlrequest.Request(target_url, method=method)
        else:
            data = json.dumps(body, ensure_ascii=False).encode("utf-8")
            req = urlrequest.Request(target_url, data=data, method=method)
            req.add_header("Content-Type", "application/json")
        try:
            with urlrequest.urlopen(req, timeout=DEFAULT_TIMEOUT_SECONDS) as response:
                raw = response.read().decode("utf-8", errors="replace")
                return self._parse_json_like(raw, getattr(response, "status", 200))
        except HTTPError as exc:
            raw = exc.read().decode("utf-8", errors="replace")
            return self._parse_json_like(raw, exc.code)
        except URLError:
            raise

    def get_qr_code(
        self,
        channel: Channel,
        *,
        device_name: str,
        device_id: str = "",
        proxy: dict[str, Any] | None = None,
        endpoint: str | None = None,
    ) -> dict[str, Any]:
        body = {"DeviceName": device_name, "DeviceID": device_id}
        if proxy:
            body["Proxy"] = self.build_proxy_info(proxy)
        return self.request_json(channel, endpoint or self.DEFAULT_LOGIN_INIT_ENDPOINT, body=body)

    def check_qr(
        self,
        channel: Channel,
        *,
        uuid: str,
        endpoint: str | None = None,
    ) -> dict[str, Any]:
        target = endpoint or self.DEFAULT_CHECK_QR_ENDPOINT
        return self.request_json(channel, target.format(uuid=uuid), body=None)

    def heartbeat(
        self,
        channel: Channel,
        *,
        wxid: str,
        long: bool = False,
        endpoint: str | None = None,
    ) -> dict[str, Any]:
        target = endpoint or (self.DEFAULT_HEARTBEAT_LONG_ENDPOINT if long else self.DEFAULT_HEARTBEAT_ENDPOINT)
        return self.request_json(channel, target.format(wxid=wxid), body=None)

    def get_cache_info(
        self,
        channel: Channel,
        *,
        wxid: str,
        device_id: str = "",
        endpoint: str | None = None,
    ) -> dict[str, Any]:
        body = {"Wxid": wxid}
        if device_id:
            body["DeviceID"] = device_id
        return self.request_json(channel, endpoint or self.DEFAULT_CACHE_INFO_ENDPOINT, body=body)

    def awaken_login(
        self,
        channel: Channel,
        *,
        wxid: str,
        endpoint: str | None = None,
    ) -> dict[str, Any]:
        target = endpoint or self.DEFAULT_AWAKEN_ENDPOINT
        return self.request_json(channel, target.format(wxid=wxid), body=None)

    def twice_auto_auth(
        self,
        channel: Channel,
        *,
        wxid: str,
        endpoint: str | None = None,
    ) -> dict[str, Any]:
        target = endpoint or self.DEFAULT_TWICE_AUTO_AUTH_ENDPOINT
        return self.request_json(channel, target.format(wxid=wxid), body=None)

    def log_out(
        self,
        channel: Channel,
        *,
        wxid: str,
        endpoint: str | None = None,
    ) -> dict[str, Any]:
        target = endpoint or self.DEFAULT_LOGOUT_ENDPOINT
        return self.request_json(channel, target.format(wxid=wxid), body=None)

    def sync_message(
        self,
        channel: Channel,
        *,
        wxid: str,
        scene: int = 0,
        synckey: str = "",
        endpoint: str | None = None,
    ) -> dict[str, Any]:
        return self.request_json(
            channel,
            endpoint or self.DEFAULT_MESSAGE_SYNC_ENDPOINT,
            body={"Wxid": wxid, "Scene": scene, "Synckey": synckey},
        )

    def revoke_message(
        self,
        channel: Channel,
        *,
        wxid: str,
        to_wxid: str,
        client_msg_id: int | str,
        create_time: int | str,
        new_msg_id: int | str,
        endpoint: str | None = None,
    ) -> dict[str, Any]:
        return self.request_json(
            channel,
            endpoint or self.DEFAULT_MESSAGE_REVOKE_ENDPOINT,
            body={
                "Wxid": wxid,
                "ToUserName": to_wxid,
                "ClientMsgId": client_msg_id,
                "CreateTime": create_time,
                "NewMsgId": new_msg_id,
            },
        )

    def send_text_message(
        self,
        channel: Channel,
        *,
        wxid: str,
        to_wxid: str,
        content: str,
        at: str | list[str] = "",
        endpoint: str | None = None,
    ) -> dict[str, Any]:
        at_value = ",".join(at) if isinstance(at, list) else str(at or "")
        return self.request_json(
            channel,
            endpoint or self.DEFAULT_SEND_TEXT_ENDPOINT,
            body={"Wxid": wxid, "ToWxid": to_wxid, "Content": content, "Type": 1, "At": at_value},
        )

    def upload_image(
        self,
        channel: Channel,
        *,
        wxid: str,
        to_wxid: str,
        base64_data: str,
        endpoint: str | None = None,
    ) -> dict[str, Any]:
        return self.request_json(
            channel,
            endpoint or self.DEFAULT_UPLOAD_IMAGE_ENDPOINT,
            body={"Wxid": wxid, "ToWxid": to_wxid, "Base64": base64_data},
        )

    def send_video_message(
        self,
        channel: Channel,
        *,
        wxid: str,
        to_wxid: str,
        video_base64: str,
        image_base64: str,
        play_length: int,
        endpoint: str | None = None,
    ) -> dict[str, Any]:
        return self.request_json(
            channel,
            endpoint or self.DEFAULT_SEND_VIDEO_ENDPOINT,
            body={
                "Wxid": wxid,
                "ToWxid": to_wxid,
                "Base64": video_base64,
                "ImageBase64": image_base64,
                "PlayLength": play_length,
            },
        )

    def send_voice_message(
        self,
        channel: Channel,
        *,
        wxid: str,
        to_wxid: str,
        base64_data: str,
        voice_time: int,
        voice_type: int = 0,
        endpoint: str | None = None,
    ) -> dict[str, Any]:
        return self.request_json(
            channel,
            endpoint or self.DEFAULT_SEND_VOICE_ENDPOINT,
            body={
                "Wxid": wxid,
                "ToWxid": to_wxid,
                "Base64": base64_data,
                "VoiceTime": voice_time,
                "Type": voice_type,
            },
        )

    def share_link(
        self,
        channel: Channel,
        *,
        wxid: str,
        to_wxid: str,
        url: str = "",
        title: str = "",
        desc: str = "",
        thumb_url: str = "",
        xml: str = "",
        link_type: int = 0,
        endpoint: str | None = None,
    ) -> dict[str, Any]:
        body = {"Wxid": wxid, "ToWxid": to_wxid}
        if xml:
            body.update({"Xml": xml, "Type": link_type})
        else:
            body.update({"Url": url, "Title": title, "Desc": desc, "ThumbUrl": thumb_url})
        return self.request_json(channel, endpoint or self.DEFAULT_SHARE_LINK_ENDPOINT, body=body)

    def send_emoji_message(
        self,
        channel: Channel,
        *,
        wxid: str,
        to_wxid: str,
        md5: str,
        total_len: int,
        endpoint: str | None = None,
    ) -> dict[str, Any]:
        return self.request_json(
            channel,
            endpoint or self.DEFAULT_SEND_EMOJI_ENDPOINT,
            body={"Wxid": wxid, "ToWxid": to_wxid, "Md5": md5, "TotalLen": total_len},
        )

    def share_card(
        self,
        channel: Channel,
        *,
        wxid: str,
        to_wxid: str,
        card_wxid: str,
        card_nickname: str,
        card_alias: str = "",
        endpoint: str | None = None,
    ) -> dict[str, Any]:
        return self.request_json(
            channel,
            endpoint or self.DEFAULT_SHARE_CARD_ENDPOINT,
            body={
                "Wxid": wxid,
                "ToWxid": to_wxid,
                "CardWxId": card_wxid,
                "CardNickName": card_nickname,
                "CardAlias": card_alias,
            },
        )

    def send_app_message(
        self,
        channel: Channel,
        *,
        wxid: str,
        to_wxid: str,
        xml: str,
        app_type: int = 0,
        endpoint: str | None = None,
    ) -> dict[str, Any]:
        return self.request_json(
            channel,
            endpoint or self.DEFAULT_SEND_APP_ENDPOINT,
            body={"Wxid": wxid, "ToWxid": to_wxid, "Xml": xml, "Type": app_type},
        )

    def send_cdn_file_message(
        self,
        channel: Channel,
        *,
        wxid: str,
        to_wxid: str,
        content_xml: str,
        endpoint: str | None = None,
    ) -> dict[str, Any]:
        return self.request_json(
            channel,
            endpoint or self.DEFAULT_SEND_CDN_FILE_ENDPOINT,
            body={"Wxid": wxid, "ToWxid": to_wxid, "Content": content_xml},
        )

    def send_cdn_image_message(
        self,
        channel: Channel,
        *,
        wxid: str,
        to_wxid: str,
        content_xml: str,
        endpoint: str | None = None,
    ) -> dict[str, Any]:
        return self.request_json(
            channel,
            endpoint or self.DEFAULT_SEND_CDN_IMAGE_ENDPOINT,
            body={"Wxid": wxid, "ToWxid": to_wxid, "Content": content_xml},
        )

    def send_cdn_video_message(
        self,
        channel: Channel,
        *,
        wxid: str,
        to_wxid: str,
        content_xml: str,
        endpoint: str | None = None,
    ) -> dict[str, Any]:
        return self.request_json(
            channel,
            endpoint or self.DEFAULT_SEND_CDN_VIDEO_ENDPOINT,
            body={"Wxid": wxid, "ToWxid": to_wxid, "Content": content_xml},
        )

    def get_chatroom_info(
        self,
        channel: Channel,
        *,
        wxid: str,
        chatroom_id: str,
        endpoint: str | None = None,
    ) -> dict[str, Any]:
        return self.request_json(
            channel,
            endpoint or self.DEFAULT_CHATROOM_INFO_ENDPOINT,
            body={"Wxid": wxid, "QID": chatroom_id},
        )

    def get_chatroom_member_detail(
        self,
        channel: Channel,
        *,
        wxid: str,
        chatroom_id: str,
        endpoint: str | None = None,
    ) -> dict[str, Any]:
        return self.request_json(
            channel,
            endpoint or self.DEFAULT_CHATROOM_MEMBER_DETAIL_ENDPOINT,
            body={"Wxid": wxid, "QID": chatroom_id},
        )

    def add_chatroom_member(
        self,
        channel: Channel,
        *,
        wxid: str,
        chatroom_id: str,
        member_wxid: str,
        endpoint: str | None = None,
    ) -> dict[str, Any]:
        return self.request_json(
            channel,
            endpoint or self.DEFAULT_CHATROOM_ADD_MEMBER_ENDPOINT,
            body={"Wxid": wxid, "ChatRoomName": chatroom_id, "ToWxids": member_wxid},
        )

    def get_chatroom_announce(
        self,
        channel: Channel,
        *,
        wxid: str,
        chatroom_id: str,
        endpoint: str | None = None,
    ) -> dict[str, Any]:
        return self.request_json(
            channel,
            endpoint or self.DEFAULT_CHATROOM_INFO_DETAIL_ENDPOINT,
            body={"Wxid": wxid, "QID": chatroom_id},
        )

    def get_chatroom_qrcode(
        self,
        channel: Channel,
        *,
        wxid: str,
        chatroom_id: str,
        endpoint: str | None = None,
    ) -> dict[str, Any]:
        return self.request_json(
            channel,
            endpoint or self.DEFAULT_CHATROOM_QRCODE_ENDPOINT,
            body={"Wxid": wxid, "Chatroom": chatroom_id},
        )

    def invite_chatroom_member(
        self,
        channel: Channel,
        *,
        wxid: str,
        chatroom_id: str,
        to_wxids: str | list[str],
        endpoint: str | None = None,
    ) -> dict[str, Any]:
        target_ids = ",".join(to_wxids) if isinstance(to_wxids, list) else str(to_wxids)
        return self.request_json(
            channel,
            endpoint or self.DEFAULT_CHATROOM_INVITE_MEMBER_ENDPOINT,
            body={"Wxid": wxid, "ChatRoomName": chatroom_id, "ToWxids": target_ids},
        )

    def accept_friend(
        self,
        channel: Channel,
        *,
        wxid: str,
        scene: int,
        v1: str,
        v2: str,
        endpoint: str | None = None,
    ) -> dict[str, Any]:
        return self.request_json(
            channel,
            endpoint or self.DEFAULT_ACCEPT_FRIEND_ENDPOINT,
            body={"Wxid": wxid, "Scene": scene, "V1": v1, "V2": v2},
        )

    def get_contact(
        self,
        channel: Channel,
        *,
        wxid: str,
        request_wxids: str | list[str],
        endpoint: str | None = None,
    ) -> dict[str, Any]:
        return self.request_json(
            channel,
            endpoint or self.DEFAULT_CONTACT_DETAIL_ENDPOINT,
            body={"Wxid": wxid, "RequestWxids": self._join_wxids(request_wxids)},
        )

    def get_contact_detail(
        self,
        channel: Channel,
        *,
        wxid: str,
        to_wxids: str | list[str],
        chatroom_id: str = "",
        endpoint: str | None = None,
    ) -> dict[str, Any]:
        return self.request_json(
            channel,
            endpoint or self.DEFAULT_CONTACT_DETAIL_ENDPOINT,
            body={"Wxid": wxid, "Towxids": self._join_wxids(to_wxids), "Chatroom": chatroom_id},
        )

    def get_contact_list(
        self,
        channel: Channel,
        *,
        wxid: str,
        current_wxcontact_seq: int = 0,
        current_chatroom_contact_seq: int = 0,
        endpoint: str | None = None,
    ) -> dict[str, Any]:
        return self.request_json(
            channel,
            endpoint or self.DEFAULT_CONTACT_LIST_ENDPOINT,
            body={
                "Wxid": wxid,
                "CurrentWxcontactSeq": current_wxcontact_seq,
                "CurrentChatroomContactSeq": current_chatroom_contact_seq,
            },
        )

    def get_profile(
        self,
        channel: Channel,
        *,
        wxid: str,
        target_wxid: str,
        endpoint: str | None = None,
    ) -> dict[str, Any]:
        del wxid
        target = endpoint or self.DEFAULT_USER_PROFILE_ENDPOINT
        return self.request_json(channel, target.format(wxid=target_wxid), body=None)

    def get_my_qrcode(
        self,
        channel: Channel,
        *,
        wxid: str,
        style: int = 0,
        endpoint: str | None = None,
    ) -> dict[str, Any]:
        return self.request_json(
            channel,
            endpoint or self.DEFAULT_MY_QRCODE_ENDPOINT,
            body={"Wxid": wxid, "Style": style},
        )

    def download_image(
        self,
        channel: Channel,
        *,
        wxid: str,
        file_aes_key: str,
        file_no: str,
        endpoint: str | None = None,
    ) -> dict[str, Any]:
        return self.request_json(
            channel,
            endpoint or self.DEFAULT_TOOL_DOWNLOAD_IMAGE_ENDPOINT,
            body={"Wxid": wxid, "FileAesKey": file_aes_key, "FileNo": file_no},
        )

    def download_voice(
        self,
        channel: Channel,
        *,
        wxid: str,
        msg_id: str,
        voice_url: str,
        length: int,
        endpoint: str | None = None,
    ) -> dict[str, Any]:
        return self.request_json(
            channel,
            endpoint or self.DEFAULT_TOOL_DOWNLOAD_VOICE_ENDPOINT,
            body={"Wxid": wxid, "MsgId": msg_id, "Voiceurl": voice_url, "Length": length},
        )

    def download_attach(
        self,
        channel: Channel,
        *,
        wxid: str,
        attach_id: str,
        data_len: int,
        username: str,
        appid: str,
        start_pos: int = 0,
        endpoint: str | None = None,
    ) -> dict[str, Any]:
        return self.request_json(
            channel,
            endpoint or self.DEFAULT_TOOL_DOWNLOAD_FILE_ENDPOINT,
            body={
                "Wxid": wxid,
                "AttachId": attach_id,
                "DataLen": data_len,
                "Section": {"DataLen": data_len, "StartPos": start_pos},
                "UserName": username,
                "AppID": appid,
            },
        )

    def download_video(
        self,
        channel: Channel,
        *,
        wxid: str,
        msg_id: str,
        endpoint: str | None = None,
    ) -> dict[str, Any]:
        return self.request_json(
            channel,
            endpoint or self.DEFAULT_TOOL_DOWNLOAD_VIDEO_ENDPOINT,
            body={"Wxid": wxid, "MsgId": msg_id},
        )

    def friend_circle_upload(
        self,
        channel: Channel,
        *,
        wxid: str,
        base64_data: str,
        endpoint: str | None = None,
    ) -> dict[str, Any]:
        return self.request_json(
            channel,
            endpoint or self.DEFAULT_FRIEND_CIRCLE_UPLOAD_ENDPOINT,
            body={"Wxid": wxid, "Base64": base64_data},
        )

    def set_step(
        self,
        channel: Channel,
        *,
        wxid: str,
        step_count: int,
        endpoint: str | None = None,
    ) -> dict[str, Any]:
        return self.request_json(
            channel,
            endpoint or self.DEFAULT_SET_STEP_ENDPOINT,
            body={"Wxid": wxid, "StepCount": step_count},
        )

    def set_proxy(
        self,
        channel: Channel,
        *,
        wxid: str,
        proxy: dict[str, Any],
        endpoint: str | None = None,
    ) -> dict[str, Any]:
        return self.request_json(
            channel,
            endpoint or self.DEFAULT_SET_PROXY_ENDPOINT,
            body={"Wxid": wxid, "Proxy": self.build_proxy_info(proxy)},
        )

    def check_database(
        self,
        channel: Channel,
        *,
        endpoint: str | None = None,
    ) -> dict[str, Any]:
        return self.request_json(channel, endpoint or self.DEFAULT_CHECK_DATABASE_ENDPOINT, body=None, method="GET")

    @staticmethod
    def build_proxy_info(proxy: dict[str, Any]) -> dict[str, Any]:
        ip = str(proxy.get("ip") or proxy.get("host") or "").strip()
        port = str(proxy.get("port") or "").strip()
        proxy_ip = str(proxy.get("proxy_ip") or "").strip()
        if not proxy_ip and ip and port:
            proxy_ip = f"{ip}:{port}"
        return {
            "ProxyIp": proxy_ip,
            "ProxyUser": str(proxy.get("username") or proxy.get("user") or "").strip(),
            "ProxyPassword": str(proxy.get("password") or "").strip(),
        }

    @staticmethod
    def _join_wxids(wxids: str | list[str]) -> str:
        if isinstance(wxids, list):
            return ",".join(str(item or "").strip() for item in wxids if str(item or "").strip())
        return str(wxids or "").strip()

    @staticmethod
    def is_success(payload: dict[str, Any]) -> bool:
        for key in ("Success", "success", "IsSuccess", "isSuccess"):
            value = payload.get(key)
            if isinstance(value, bool):
                return value
        for key in ("Code", "code", "Ret", "ret"):
            if key not in payload:
                continue
            value = payload.get(key)
            if value in (0, "0", 200, "200"):
                return True
            if value in (None, ""):
                return False
            return False
        return False

    @staticmethod
    def unwrap_value(value: Any) -> Any:
        current = value
        while isinstance(current, dict):
            for key in ("string", "String", "str", "Str", "value", "Value"):
                if key in current:
                    current = current[key]
                    break
            else:
                return current
        return current

    @classmethod
    def dig(cls, payload: Any, *path: str) -> Any:
        current = payload
        for part in path:
            if not isinstance(current, dict):
                return None
            current = current.get(part)
        return cls.unwrap_value(current)

    @classmethod
    def extract_data(cls, payload: dict[str, Any]) -> dict[str, Any]:
        for key in ("Data", "data"):
            data = payload.get(key)
            if isinstance(data, dict):
                return data
        return {}

    @classmethod
    def extract_list(cls, payload: dict[str, Any], *keys: str) -> list[dict[str, Any]]:
        data = cls.extract_data(payload)
        candidates = [payload.get(key) for key in keys] + [data.get(key) for key in keys]
        for item in candidates:
            if isinstance(item, list):
                return [entry for entry in item if isinstance(entry, dict)]
        return []

    @classmethod
    def extract_first_dict(cls, payload: dict[str, Any], *keys: str) -> dict[str, Any]:
        data = cls.extract_data(payload)
        candidates = [payload.get(key) for key in keys] + [data.get(key) for key in keys]
        for item in candidates:
            if isinstance(item, list):
                for entry in item:
                    if isinstance(entry, dict):
                        return entry
            if isinstance(item, dict):
                return item
        return {}

    @classmethod
    def pick_value(cls, payload: dict[str, Any], *paths: tuple[str, ...]) -> Any:
        for path in paths:
            value = cls.dig(payload, *path)
            if value not in (None, ""):
                return value
        return None

    @classmethod
    def extract_message(cls, payload: dict[str, Any]) -> str | None:
        raw_message = None
        for key in ("Message", "Msg", "message", "msg"):
            value = payload.get(key)
            if value not in (None, ""):
                raw_message = str(value)
                break
        if raw_message is None:
            value = cls.pick_value(payload, ("Data", "Msg"), ("Data", "Message"), ("data", "Msg"), ("data", "Message"))
            if value not in (None, ""):
                raw_message = str(value)
        detail_message = cls.extract_detail_message(payload)
        if detail_message and (raw_message in (None, "", "登录异常", "登陆异常", "success", "成功")):
            return detail_message
        if raw_message not in (None, ""):
            return raw_message
        if detail_message:
            return detail_message
        code = cls.extract_code(payload)
        if code not in (None, ""):
            mapped = cls.describe_error_code(code)
            if mapped:
                return mapped
        return None

    @classmethod
    def extract_detail_message(cls, payload: dict[str, Any]) -> str | None:
        candidates = (
            cls.pick_value(payload, ("Data", "baseResponse", "errMsg", "string")),
            cls.pick_value(payload, ("Data", "baseResponse", "errMsg")),
            cls.pick_value(payload, ("baseResponse", "errMsg", "string")),
            cls.pick_value(payload, ("baseResponse", "errMsg")),
            cls.pick_value(payload, ("Data", "authSectResp", "errMsg", "string")),
            cls.pick_value(payload, ("Data", "authSectResp", "errMsg")),
        )
        for value in candidates:
            normalized = cls.normalize_provider_message_detail(value)
            if normalized:
                return normalized
        return None

    @staticmethod
    def normalize_provider_message_detail(value: Any) -> str | None:
        text = str(WechatIpadApiClient.unwrap_value(value) or "").strip()
        if not text:
            return None
        cdata_match = re.search(r"<Content><!\[CDATA\[(.*?)\]\]></Content>", text, flags=re.IGNORECASE | re.DOTALL)
        if cdata_match:
            extracted = cdata_match.group(1).strip()
            if extracted:
                return extracted
        text = re.sub(r"<[^>]+>", " ", text)
        text = re.sub(r"\s+", " ", text).strip()
        return text or None

    @classmethod
    def extract_code(cls, payload: dict[str, Any]) -> str | None:
        for key in ("Code", "code", "Ret", "ret"):
            value = payload.get(key)
            if value not in (None, ""):
                return str(value)
        return None

    @classmethod
    def describe_error_code(cls, code: Any) -> str | None:
        try:
            normalized = int(str(code).strip())
        except (TypeError, ValueError):
            return None
        return cls.ERROR_CODE_MESSAGES.get(normalized)

    @classmethod
    def extract_qr_fields(cls, payload: dict[str, Any]) -> dict[str, Any]:
        data = cls.extract_data(payload)
        qr_code_response = data.get("QrCodeResponse") if isinstance(data.get("QrCodeResponse"), dict) else {}
        raw_qr_url = cls.pick_value(
            payload,
            ("Data", "QrUrl"),
            ("Data", "qrUrl"),
            ("Data", "QrCodeResponse", "QrUrl"),
            ("Data", "QrCodeResponse", "qrUrl"),
            ("QrUrl",),
            ("QRCode",),
        )
        raw_qr_base64 = cls.pick_value(
            payload,
            ("Data", "QrCode"),
            ("Data", "qrcode"),
            ("Data", "Base64"),
            ("Data", "base64"),
            ("Data", "QrCodeBase64"),
            ("Data", "qrCodeBase64"),
            ("Data", "QrBase64"),
            ("Data", "qrBase64"),
            ("Data", "QrCodeResponse", "Base64"),
            ("Data", "QrCodeResponse", "base64"),
            ("Data", "QrCodeResponse", "QrCode"),
            ("Data", "QrCodeResponse", "qrcode"),
            ("Base64",),
            ("base64",),
        )
        normalized_qr_url = cls.normalize_qr_image_source(raw_qr_url)
        normalized_qr_base64 = cls.normalize_qr_image_source(raw_qr_base64)
        return {
            "uuid": cls.pick_value(
                payload,
                ("Data", "Uuid"),
                ("Data", "uuid"),
                ("Data", "QrCodeResponse", "Uuid"),
                ("Data", "QrCodeResponse", "uuid"),
                ("Uuid",),
                ("uuid",),
            ),
            "qr_url": normalized_qr_url["url"] or normalized_qr_base64["url"],
            "qr_base64": normalized_qr_base64["base64"] or normalized_qr_url["base64"],
            "qr_image_media_type": normalized_qr_base64["media_type"] or normalized_qr_url["media_type"],
            "expires_at": cls.pick_value(
                payload,
                ("Data", "expiredTime"),
                ("Data", "QrCodeResponse", "expiredTime"),
                ("Data", "ExpireAt"),
                ("ExpireAt",),
            ),
            "qr_code_response": qr_code_response,
        }

    @staticmethod
    def normalize_qr_image_source(value: Any) -> dict[str, str | None]:
        text = str(WechatIpadApiClient.unwrap_value(value) or "").strip()
        if not text:
            return {"url": None, "base64": None, "media_type": None}
        if text.startswith(("http://", "https://")):
            return {"url": text, "base64": None, "media_type": None}
        if text.startswith("data:"):
            header, _, payload = text.partition(",")
            if ";base64" not in header:
                return {"url": None, "base64": None, "media_type": None}
            media_type = header[5:].split(";", 1)[0] or "image/png"
            normalized_payload = "".join(payload.split())
            if WechatIpadApiClient.is_base64_payload(normalized_payload):
                return {"url": None, "base64": normalized_payload, "media_type": media_type}
            return {"url": None, "base64": None, "media_type": None}
        normalized_text = "".join(text.split())
        if WechatIpadApiClient.is_base64_payload(normalized_text):
            return {"url": None, "base64": normalized_text, "media_type": "image/png"}
        return {"url": None, "base64": None, "media_type": None}

    @staticmethod
    def is_base64_payload(value: str) -> bool:
        if len(value) < 8 or len(value) % 4 != 0:
            return False
        try:
            base64.b64decode(value, validate=True)
        except (binascii.Error, ValueError):
            return False
        return True

    @classmethod
    def extract_account_fields(cls, payload: dict[str, Any]) -> dict[str, Any]:
        return {
            "wxid": cls.pick_value(
                payload,
                ("Data", "acctSectResp", "userName"),
                ("Data", "UserName"),
                ("Data", "Wxid"),
                ("Wxid",),
            ),
            "nickname": cls.pick_value(
                payload,
                ("Data", "acctSectResp", "nickName"),
                ("Data", "NickName"),
                ("Data", "nickname"),
                ("NickName",),
            ),
            "alias": cls.pick_value(payload, ("Data", "acctSectResp", "alias"), ("Data", "Alias"), ("Alias",)),
            "bind_mobile": cls.pick_value(
                payload,
                ("Data", "acctSectResp", "bindMobile"),
                ("Data", "BindMobile"),
                ("BindMobile",),
            ),
        }

    @classmethod
    def extract_sync_fields(cls, payload: dict[str, Any]) -> dict[str, Any]:
        data = cls.extract_data(payload)
        messages = cls.extract_list(payload, "AddMsgs", "Messages", "MsgList", "List")
        return {
            "list": messages,
            "add_msgs": messages,
            "sync_key": cls.pick_value(payload, ("Data", "Synckey"), ("Synckey",), ("Data", "SyncKey"), ("SyncKey",)),
            "continue_flag": cls.pick_value(payload, ("Data", "ContinueFlag"), ("ContinueFlag",)),
            "data": data,
        }

    @classmethod
    def extract_send_ack_fields(cls, payload: dict[str, Any]) -> dict[str, Any]:
        first = cls.extract_first_dict(payload, "List")
        data = cls.extract_data(payload)
        client_msg_id = cls.pick_value(
            {"Data": first, **payload},
            ("Data", "ClientMsgId"),
            ("Data", "clientMsgId"),
            ("Data", "ClientImgId", "string"),
            ("Data", "ClientImgId"),
            ("Data", "clientImgId"),
            ("ClientMsgId",),
        )
        create_time = cls.pick_value(
            {"Data": first, **payload},
            ("Data", "CreateTime"),
            ("Data", "createTime"),
            ("CreateTime",),
            ("createTime",),
        )
        new_msg_id = cls.pick_value(
            {"Data": first, **payload},
            ("Data", "NewMsgId"),
            ("Data", "newMsgId"),
            ("Data", "Newmsgid"),
            ("NewMsgId",),
            ("newMsgId",),
            ("Newmsgid",),
            ("MsgId",),
        )
        return {
            "data": data,
            "list_item": first,
            "client_msg_id": None if client_msg_id in (None, "") else str(client_msg_id),
            "create_time": None if create_time in (None, "") else str(create_time),
            "new_msg_id": None if new_msg_id in (None, "") else str(new_msg_id),
        }

    def _parse_json_like(self, raw: str, status: int) -> dict[str, Any]:
        try:
            parsed = json.loads(raw) if raw else {}
        except json.JSONDecodeError:
            return {"http_status": status, "raw": raw}
        if isinstance(parsed, dict):
            parsed.setdefault("http_status", status)
            return parsed
        return {"http_status": status, "data": parsed}
