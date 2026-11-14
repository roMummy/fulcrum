"""群邀请关系的唯一快照、系统消息证据及无递归层级图谱。

邀请边只使用明确成员 ID，昵称只作显示；成员资料优先于系统消息，空快照和旧消息
不能清空已知关系。查看图谱不改数据库，历史补查也不修改成员在群状态。
"""

from __future__ import annotations

import json
import xml.etree.ElementTree as ET
from datetime import datetime
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.tenant_context import require_tenant_id
from app.core.text_column import BYTE_LIMIT
from app.core.time import ensure_shanghai_datetime
from app.models.entities import Channel, Chatroom, ChatroomMember, Conversation, Message
from app.services.account_identity import account_identity
from app.services.chatroom_directory import get_or_create_member
from app.services.plugin_schemas import parse_config_json

SOURCE_LABELS = {'member_profile': '成员资料', 'system_message': '入群系统消息'}
HISTORY_LIMIT = 2000


def _person_id(value: Any) -> str:
    """允许不同渠道的个人 ID，但拒绝空值、群 ID、占位文本与包含空白的名字。"""
    if not isinstance(value, str):
        return ''
    value = value.strip()
    if (not value or len(value) > 128 or value.endswith('@chatroom')
            or value.lower() in {'0', '-', 'null', 'undefined', 'unknown', 'system', 'weixin'}
            or any(character.isspace() for character in value)):
        return ''
    return value


def member_invitation(member: ChatroomMember) -> dict[str, Any]:
    """只读取当前唯一快照，旧渠道字段在下次同步时由适配器直接归一，不另设兼容读取。"""
    saved = parse_config_json(member.extra_json).get('invitation')
    if not isinstance(saved, dict) or not isinstance(saved.get('source'), str) or saved['source'] not in SOURCE_LABELS:
        return {}
    inviter = _person_id(saved.get('inviter_external_id'))
    if not inviter or inviter == member.member_external_id:
        return {}
    return {**saved, 'inviter_external_id': inviter}


def save_member_invitation(
    member: ChatroomMember, inviter_id: str, *, source: str, recorded_at: datetime,
    method: str = 'invite', event_key: str = '', inviter_name: str = '',
) -> bool:
    """保存一条可核实的邀请边，返回是否更新；空字段、跨租户和较低优先级证据不覆盖。"""
    inviter_id = _person_id(inviter_id)
    if (not inviter_id or inviter_id == member.member_external_id
            or source not in SOURCE_LABELS or method not in ('invite', 'qr')):
        return False
    if member.tenant_id != require_tenant_id():
        return False
    previous = member_invitation(member)
    recorded_at = ensure_shanghai_datetime(recorded_at)
    if previous:
        if previous['source'] == 'member_profile' and source != 'member_profile':
            return False
        if previous['source'] == source:
            try:
                before = ensure_shanghai_datetime(datetime.fromisoformat(previous.get('recorded_at', '')))
            except (TypeError, ValueError):
                before = None
            if before and recorded_at < before:
                return False
            # 相同事件的重复回报不重写；同一个人再次邀请则推进证据时间，避免中间较早的
            # 入群事件延迟到达时覆盖最新关系。recorded_at 始终代表本来源的最新有效证据。
            if previous['inviter_external_id'] == inviter_id and previous.get('method') == method:
                if before == recorded_at or event_key and previous.get('event_key') == event_key:
                    return False
    saved = {
        'inviter_external_id': inviter_id, 'source': source, 'method': method,
        'recorded_at': recorded_at.isoformat(), 'event_key': str(event_key or '')[:128],
        'inviter_name': str(inviter_name or '')[:255],
    }
    serialized = json.dumps({**parse_config_json(member.extra_json), 'invitation': saved}, ensure_ascii=False, sort_keys=True)
    if len(serialized.encode('utf-8')) > BYTE_LIMIT:
        return False
    member.extra_json = serialized
    return True


def parse_system_invitation(content: str, *, own_id: str = '') -> dict[str, Any] | None:
    """解析微信系统 XML 的明确邀请/扫码模板，拒绝仅有昵称、普通文字或不完整的链接。"""
    if not isinstance(content, str) or len(content) > 128_000 or '<sysmsg' not in content:
        return None
    if '<!DOCTYPE' in content.upper() or '<!ENTITY' in content.upper():
        return None
    try:
        root = ET.fromstring(content.strip())
    except ET.ParseError:
        return None
    if root.tag != 'sysmsg' or root.get('type') != 'sysmsgtemplate':
        return None
    template = root.findtext('.//content_template/template') or ''
    if '"$username$"邀请"$names$"加入了群聊' in template:
        inviter_link, member_link, method = 'username', 'names', 'invite'
    elif '你邀请"$names$"加入了群聊' in template:
        inviter_link, member_link, method = '', 'names', 'invite'
    elif ('"$adder$"通过扫描"$from$"分享的二维码加入群聊' in template
          or '"$adder$"通过"$from$"的邀请二维码加入群聊' in template):
        inviter_link, member_link, method = 'from', 'adder', 'qr'
    else:
        return None

    def linked_people(name: str) -> list[tuple[str, str]]:
        link = root.find(f".//content_template/link_list/link[@name='{name}']")
        if link is None:
            return []
        rows = link.findall('memberlist/member') or link.findall('member')
        return [(key, str(row.findtext('nickname') or '')[:255])
                for row in rows if (key := _person_id(row.findtext('username')))]

    inviters = linked_people(inviter_link) if inviter_link else [(_person_id(own_id), '')]
    members = linked_people(member_link)
    if len(inviters) != 1 or not inviters[0][0] or not members:
        return None
    inviter_id, inviter_name = inviters[0]
    return {'inviter_external_id': inviter_id, 'inviter_name': inviter_name, 'method': method,
            'member_ids': list(dict.fromkeys(key for key, _ in members if key != inviter_id))}


def _own_id(channel: Channel) -> str:
    """“你邀请”必须能对应本渠道当前账号 ID，不能把系统消息发送者误当作邀请人。"""
    own_id = account_identity(channel)['external_id']
    if not own_id and channel.channel_type == 'wechat_ipad':
        own_id = parse_config_json(channel.adapter_config_json).get('wxid')
    if not own_id and channel.provider_account_id:
        account = channel.provider_account
        own_id = account.external_user_id if account is not None else ''
    return _person_id(own_id)


def record_invitation_event(
    db: Session, channel: Channel, conversation: Conversation, normalized: dict[str, Any], *,
    recorded_at: datetime, existing_only: bool = False,
) -> int:
    """入站与历史补查共用的边界；只处理可信系统消息，不受群功能通知开关影响。"""
    message = normalized.get('message')
    if not isinstance(message, dict) or message.get('message_type') != 'system':
        return 0
    if channel.channel_type not in ('wechat_webridge', 'wechat_ipad', 'workwechat', 'wechat_gugatool'):
        return 0
    tenant_id = require_tenant_id()
    if (channel.tenant_id != tenant_id or conversation.tenant_id != tenant_id
            or conversation.channel_id != channel.id or conversation.bot_id != channel.bot_id
            or conversation.chat_type != 'group'):
        return 0
    details = normalized.get('details')
    details = details if isinstance(details, dict) else {}
    own_id = _own_id(channel)
    parsed = next((parsed for content in (message.get('content'), details.get('content_xml'))
                   if (parsed := parse_system_invitation(content, own_id=own_id))), None)
    if parsed is None:
        return 0
    # 先 flush 再锁定群，防止 populate_existing 覆盖当前入站事务已经更新的目录资料。
    db.flush()
    room = db.scalar(select(Chatroom).where(
        Chatroom.tenant_id == tenant_id, Chatroom.channel_id == channel.id,
        Chatroom.bot_id == channel.bot_id, Chatroom.external_id == conversation.external_id,
    ).with_for_update().execution_options(populate_existing=True))
    if room is None or room.management_status == 'archived':
        return 0
    updated = 0
    for member_id in parsed['member_ids']:
        member = db.scalar(select(ChatroomMember).where(
            ChatroomMember.tenant_id == tenant_id, ChatroomMember.channel_id == channel.id,
            ChatroomMember.bot_id == channel.bot_id, ChatroomMember.chatroom_id == room.id,
            ChatroomMember.member_external_id == member_id,
        ).with_for_update())
        if member is None:
            if existing_only:
                continue
            member, _ = get_or_create_member(db, channel, room, member_id, status='active')
        updated += int(save_member_invitation(
            member, parsed['inviter_external_id'], source='system_message', recorded_at=recorded_at,
            method=parsed['method'], event_key=normalized.get('event_key', ''), inviter_name=parsed['inviter_name'],
        ))
    return updated


def backfill_chatroom_invitations(db: Session, channel: Channel, room: Chatroom) -> dict[str, Any]:
    """仅检查本群已保存的最近系统消息；不读取远端历史，不新增历史成员或改写在群状态。"""
    tenant_id = require_tenant_id()
    if channel.tenant_id != tenant_id or room.tenant_id != tenant_id or room.channel_id != channel.id or room.bot_id != channel.bot_id:
        return {'checked': 0, 'updated': 0, 'limited': False}
    conversation = db.scalar(select(Conversation).where(
        Conversation.tenant_id == tenant_id, Conversation.channel_id == channel.id,
        Conversation.bot_id == channel.bot_id, Conversation.external_id == room.external_id,
        Conversation.chat_type == 'group',
    ))
    rows = db.scalars(select(Message).where(
        Message.tenant_id == tenant_id, Message.conversation_id == conversation.id,
        Message.message_type == 'system', Message.direction == 'in',
    ).order_by(Message.id.desc()).limit(HISTORY_LIMIT + 1)).all() if conversation else []
    updated = 0
    for message in reversed(rows[:HISTORY_LIMIT]):
        updated += record_invitation_event(
            db, channel, conversation,
            {'message': {'message_type': 'system', 'content': message.content}, 'event_key': f'message:{message.id}'},
            recorded_at=message.created_at, existing_only=True,
        )
    return {'checked': min(len(rows), HISTORY_LIMIT), 'updated': updated, 'limited': len(rows) > HISTORY_LIMIT}


def build_invitation_graph(members: list[dict[str, Any]]) -> dict[str, Any]:
    """按 ID 生成层级图谱；孤立成员单独展示，环只切断视图边，不改邀请证据。

    上级未知但确实邀请了其他人的成员仍是图谱根节点；只有既没有展示上级、也没有
    展示下级的成员才进入未关联区。所有遍历使用显式栈，深层邀请链不消耗递归栈。
    """
    rows = {row['member_external_id']: row for row in members}
    nodes = {key: {
        'external_id': key, 'name': row.get('display_name') if row.get('display_name') not in (None, '', '-') else row.get('nickname') or key,
        'avatar_url': row.get('avatar_url', ''), 'status': row.get('status', 'active'), 'role': row.get('role', 'member'),
        'placeholder': False,
    } for key, row in rows.items()}
    parents: dict[str, str | None] = {}
    direct_counts: dict[str, int] = {}
    known_count = 0
    for key, row in rows.items():
        saved = row.get('invitation')
        saved = saved if isinstance(saved, dict) else {}
        inviter = _person_id(saved.get('inviter_external_id'))
        source = saved.get('source')
        inviter = inviter if inviter != key and isinstance(source, str) and source in SOURCE_LABELS else ''
        if inviter:
            known_count += 1
            direct_counts[inviter] = direct_counts.get(inviter, 0) + 1
            if inviter not in nodes:
                nodes[inviter] = {'external_id': inviter, 'name': saved.get('inviter_name') or inviter,
                                  'avatar_url': '', 'status': '', 'role': '', 'placeholder': True}
            source_label = SOURCE_LABELS[saved['source']]
            if saved.get('method') == 'qr':
                source_label += ' · 扫码来源'
            row['inviter'] = {'known': True, 'name': nodes[inviter]['name'], 'external_id': inviter,
                              'source_label': source_label, 'recorded_at': saved.get('recorded_at')}
        else:
            row['inviter'] = {'known': False, 'name': '未知', 'external_id': '', 'source_label': '', 'recorded_at': None}
        parents[key] = inviter or None
    for key in nodes:
        parents.setdefault(key, None)

    resolved: set[str] = set()
    cycle_roots: set[str] = set()
    for key in nodes:
        path: set[str] = set()
        cursor = key
        while cursor is not None and cursor not in resolved:
            if cursor in path:
                parents[cursor] = None
                cycle_roots.add(cursor)
                break
            path.add(cursor)
            cursor = parents[cursor]
        resolved.update(path)
    children: dict[str, list[str]] = {key: [] for key in nodes}
    for key, parent in parents.items():
        if parent is not None:
            children[parent].append(key)
    keys = {key: str(index) for index, key in enumerate(nodes, 1)}
    flat = []
    stack = [(key, 0) for key, parent in reversed(list(parents.items())) if parent is None]
    while stack:
        key, depth = stack.pop()
        node = nodes[key]
        # 统计使用原始有效邀请边，循环视图中的断边不应让成员的直接邀请人数减少。
        direct = direct_counts.get(key, 0)
        if key in rows:
            rows[key]['direct_invites'] = direct
        flat.append({**node, 'key': keys[key], 'parent_key': keys[parents[key]] if parents[key] else '',
                     'depth': depth, 'child_count': len(children[key]), 'direct_invites': direct,
                     'connected': parents[key] is not None or bool(children[key]),
                     'cycle': key in cycle_roots,
                     'inviter': rows[key]['inviter'] if key in rows else {'known': False}})
        stack.extend((child, depth + 1) for child in reversed(children[key]))
    return {'nodes': flat, 'total': len(members), 'known_count': known_count,
            'unknown_count': len(members) - known_count, 'cycle_count': len(cycle_roots),
            'unlinked_count': sum(not node['connected'] and not node['placeholder'] for node in flat)}
