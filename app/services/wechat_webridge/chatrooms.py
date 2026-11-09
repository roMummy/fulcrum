"""WeBridge 群详情页数据。

群资料、成员快照、消息流水的组装逻辑与企微群详情一致，直接复用其构建函数，
这里只覆盖微桥特有的同步入口、文案和不支持的能力开关。
"""

from __future__ import annotations

from typing import Any

from sqlalchemy.orm import Session

from app.models.entities import Channel, Chatroom
from app.services.points import PointsService
from app.services.workwechat_chatrooms import build_workwechat_chatroom_detail


def build_webridge_chatroom_detail(db: Session, channel: Channel, chatroom: Chatroom) -> dict[str, Any]:
    """返回微桥群详情；成员同步通过 room_members 命令下发给手机插件。"""

    detail = build_workwechat_chatroom_detail(db, channel, chatroom)
    # 成员积分:本群账户余额,与插件消费/赚取使用同一份账户。
    balances = PointsService(db).group_balances(str(chatroom.external_id))
    for member in detail.get("members", []):
        member["points"] = balances.get(str(member.get("member_external_id")), 0)
    detail.update(
        {
            "show_points": True,
            # 同步按钮走通用单群同步入口，该入口会为微桥渠道入队 room_members 命令。
            "sync_action": f"/channels/{channel.id}/chatrooms/{chatroom.id}/sync",
            "sync_button_label": "同步群成员",
            "member_section_hint": "成员来自手机插件的群成员同步；为空时点击右上角“同步群成员”，稍后刷新本页。",
            "auto_download_media_label": "暂不支持",
            "supports_media_download": False,
        }
    )
    return detail
