"""会话类型:群聊 / 公众号 / 私聊。消息列表的筛选与标识统一用这里的判断。

微信公众号在通讯里是一个特殊的"联系人",账号 ID 以 `gh_` 开头(例如 gh_1a2b3c4d5e6f);
"服务通知"(notifymessage)汇集的也是公众号推送。它们不是真人私聊,混在私聊里会淹没真正的客户消息,
所以单独成一类,列表里可以按类型筛选,并带明显的标识。
"""

from __future__ import annotations

from sqlalchemy import and_, or_
from sqlalchemy.sql import Select

from app.models.entities import Conversation

KIND_GROUP = "group"
KIND_OFFICIAL = "official"
KIND_DIRECT = "direct"
KINDS = (KIND_GROUP, KIND_DIRECT, KIND_OFFICIAL)

KIND_LABELS = {KIND_GROUP: "群聊", KIND_DIRECT: "私聊", KIND_OFFICIAL: "公众号"}

# 账号 ID 前缀为 gh_ 的是公众号;notifymessage 是微信的"服务通知"入口,也归公众号一类
_OFFICIAL_PREFIX = "gh_"
_OFFICIAL_EXACT = ("notifymessage",)


def is_official_account(external_id: object) -> bool:
    ident = str(external_id or "").strip().lower()
    return ident.startswith(_OFFICIAL_PREFIX) or ident in _OFFICIAL_EXACT


def conversation_kind(chat_type: object, external_id: object) -> str:
    """群聊以 chat_type 为准;非群聊里再按账号 ID 区分公众号和私聊。"""

    if str(chat_type or "").strip().lower() == "group":
        return KIND_GROUP
    return KIND_OFFICIAL if is_official_account(external_id) else KIND_DIRECT


def normalize_kind(value: object) -> str:
    """筛选参数只认三种类型,其余(含空)表示不筛选。"""

    text = str(value or "").strip().lower()
    return text if text in KINDS else ""


def _official_clause():
    return or_(
        Conversation.external_id.istartswith(_OFFICIAL_PREFIX, autoescape=True),  # autoescape:gh_ 里的下划线不是通配符
        Conversation.external_id.in_(_OFFICIAL_EXACT),
    )


def apply_kind_filter(statement: Select, kind: str) -> Select:
    """给会话查询加上类型条件;kind 为空时原样返回。"""

    if kind == KIND_GROUP:
        return statement.where(Conversation.chat_type == "group")
    if kind == KIND_OFFICIAL:
        return statement.where(and_(Conversation.chat_type != "group", _official_clause()))
    if kind == KIND_DIRECT:
        return statement.where(and_(Conversation.chat_type != "group", ~_official_clause()))
    return statement
