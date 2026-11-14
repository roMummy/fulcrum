"""群资料的统一展示与公告快照，成员顺序不再由各渠道自行覆盖。

公告仅消费已保存任务的设备回报，落在当前租户、渠道和群的唯一快照内；
HTML 转为普通文字后才交给自动转义的模板，页面不会执行设备提供的标记或请求外链。
"""

from __future__ import annotations

import json
import re
from datetime import datetime
from html.parser import HTMLParser
from typing import Any, Sequence

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.tenant_context import require_tenant_id
from app.core.text_column import BYTE_LIMIT
from app.core.time import ensure_shanghai_datetime, now_shanghai
from app.models.entities import Channel, Chatroom, ChatroomMember
from app.services.plugin_schemas import parse_config_json

NOTICE_KEY = 'group_notice'
MAX_NOTICE_HTML = 512_000
MAX_NOTICE_TEXT = 64_000


def effective_member_role(member: ChatroomMember, owner_external_id: str | None) -> str:
    """群资料里的当前群主 ID 优先，避免漏写角色或旧群主标记影响排序和展示。"""
    if owner_external_id and member.member_external_id == owner_external_id:
        return 'owner'
    role = member.role or 'member'
    if role == 'owner' and owner_external_id:
        return 'member'
    return role if role in ('owner', 'admin') else 'member'


def sort_chatroom_members(members: Sequence[ChatroomMember], *, owner_external_id: str | None) -> list[ChatroomMember]:
    """在群成员先按身份、再按最近发言排序；退群记录始终置底，使用名称和 ID 稳定打破同序。"""
    def key(member):
        active_at = ensure_shanghai_datetime(member.last_active_at).timestamp() if member.last_active_at else float('-inf')
        return (
            member.status == 'left',
            {'owner': 0, 'admin': 1, 'member': 2}[effective_member_role(member, owner_external_id)],
            -active_at,
            str(member.display_name or member.nickname or member.member_external_id).casefold(),
            member.id or 0,
        )

    return sorted(members, key=key)


class _NoticeText(HTMLParser):
    """提取公告中的可读段落与图片占位，忽略样式、脚本、文档头和内嵌内容。"""
    _ignored_tags = {'head', 'script', 'style', 'template', 'iframe', 'object', 'svg', 'math'}
    _blocks = {'p', 'div', 'section', 'article', 'li', 'ul', 'ol', 'blockquote', 'tr', 'h1', 'h2', 'h3', 'h4', 'h5', 'h6'}

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.parts: list[str] = []
        self.ignored: list[str] = []
        self.has_images = False

    def handle_starttag(self, tag, attrs):
        if tag in self._ignored_tags:
            self.ignored.append(tag)
        if self.ignored:
            return
        if tag == 'br' or tag in self._blocks:
            self.parts.append('\n')
        elif tag == 'img':
            # 图片地址可能指向设备本地或任意外部站点，资料页只标识图片，不自动请求图源。
            self.has_images = True
            self.parts.append('\n[图片]\n')

    def handle_endtag(self, tag):
        if self.ignored:
            if tag == self.ignored[-1]:
                self.ignored.pop()
            return
        if tag in self._blocks:
            self.parts.append('\n')

    def handle_data(self, data):
        if not self.ignored:
            self.parts.append(data)

    def text(self):
        # 合并排版产生的空行，保留正文的段落和列表边界，不使用会删除内容的标签正则。
        return re.sub(r'\n+', '\n', ''.join(self.parts)).strip()


def save_webridge_group_notice(
    db: Session, channel: Channel, command: dict[str, Any], result: dict[str, Any], *,
    ok: bool, requested_at: datetime, error: str = '',
) -> str | None:
    """保存公告读取结果，返回错误供既有任务终态使用；不抛设备数据异常阻塞命令回报。

    失败只更新本次读取状态，保留以前成功获取的文字和时间。请求创建时间用于拒绝晚到
    的旧查询；群 ID 必须来自已入队命令，回报中的 ID 只用于一致性检查。
    """
    tenant_id = require_tenant_id()
    if channel.tenant_id != tenant_id or channel.channel_type != 'wechat_webridge':
        return '群公告账号不属于当前企业'
    params = command.get('params')
    room_id = params.get('room_id') if isinstance(params, dict) else None
    if not isinstance(room_id, str) or not room_id:
        return '群公告任务缺少目标群'
    db.flush()
    room = db.scalar(select(Chatroom).where(
        Chatroom.tenant_id == tenant_id, Chatroom.channel_id == channel.id,
        Chatroom.bot_id == channel.bot_id, Chatroom.external_id == room_id,
    ).with_for_update().execution_options(populate_existing=True))
    if room is None:
        return '群公告目标不存在'
    if result.get('room_id') is not None and result['room_id'] != room_id:
        return '群公告回报与目标群不一致'
    extra = parse_config_json(room.extra_json)
    previous = extra.get(NOTICE_KEY)
    previous = dict(previous) if isinstance(previous, dict) else {}
    requested_at = ensure_shanghai_datetime(requested_at)
    if previous.get('request_created_at'):
        try:
            previous_request = ensure_shanghai_datetime(datetime.fromisoformat(previous['request_created_at']))
        except (TypeError, ValueError):
            previous_request = None
        if previous_request and requested_at < previous_request:
            return None

    snapshot = dict(previous)
    problem = str(error or '群公告读取失败')[:240] if not ok else ''
    if ok:
        # 按已确认的当前协议校验，不猜测字段别名；成功但缺少缓存状态仍应显示读取失败。
        found, html_cached = result.get('cached_notice_found'), result.get('html_cached')
        summary, html = result.get('summary'), result.get('html')
        if (result.get('room_id') != room_id or type(found) is not bool or type(html_cached) is not bool
                or summary is not None and not isinstance(summary, str)
                or html is not None and not isinstance(html, str)
                or html_cached and (not found or not isinstance(html, str))):
            problem = '群公告回报格式不完整，请重新同步'
        elif len(html or '') > MAX_NOTICE_HTML or len(summary or '') > MAX_NOTICE_TEXT:
            problem = '群公告内容超过读取上限'
        else:
            parser = _NoticeText()
            try:
                if html_cached:
                    parser.feed(html)
                    parser.close()
            except (ValueError, AssertionError):
                # HTMLParser 对畸形声明可能拒绝解析，按读取失败回报，不能阻塞手机队列。
                problem = '群公告正文格式异常，请重新同步'
            body = parser.text()
            if len(body) > MAX_NOTICE_TEXT:
                problem = '群公告正文超过读取上限'
            elif not problem:
                snapshot.update(
                    body=body if found else '', summary=(summary or '') if found else '',
                    state=('body' if body else ('summary' if summary else 'empty')) if found else 'not_cached',
                    has_images=parser.has_images, fetched_at=now_shanghai().isoformat(),
                )
    attempt = {'request_created_at': requested_at.isoformat(), 'checked_at': now_shanghai().isoformat()}
    snapshot.update(**attempt, last_error=problem)
    serialized = json.dumps({**extra, NOTICE_KEY: snapshot}, ensure_ascii=False, sort_keys=True)
    if len(serialized.encode('utf-8')) > BYTE_LIMIT:
        # 群资料是 TEXT 列，容量按 UTF-8 字节而非字数计算；超限保留旧正文及其他群设置。
        # 不使用日志的字符串截断器，否则页面会把被静默截断的公告当作完整正文。
        problem = '群公告内容超过资料存储上限'
        serialized = json.dumps({**extra, NOTICE_KEY: {**previous, **attempt, 'last_error': problem}}, ensure_ascii=False, sort_keys=True)
        if len(serialized.encode('utf-8')) > BYTE_LIMIT:
            return problem
    room.extra_json = serialized
    return problem or None


def build_chatroom_profile(channel: Channel, chatroom: Chatroom, members: list[dict[str, Any]]) -> dict[str, Any]:
    """为现有「资料」标签提供一致视图，能力未接入与已读取空缓存分别说明。"""
    saved = parse_config_json(chatroom.extra_json).get(NOTICE_KEY)
    saved = saved if isinstance(saved, dict) else {}
    notice_supported = channel.channel_type == 'wechat_webridge'
    owner = next((member for member in members if (
        member['member_external_id'] == chatroom.owner_external_id
        or not chatroom.owner_external_id and member.get('role') == 'owner'
    )), None)
    owner_name = next((owner.get(key) for key in ('display_name', 'nickname', 'member_external_id')
                       if owner and owner.get(key) not in (None, '', '-')), '暂未获取')
    # 仅接受本模块定义的快照类型，损坏数据不向模板泄漏对象，也不引入旧字段兼容路径。
    body = saved.get('body') if isinstance(saved.get('body'), str) else ''
    summary = saved.get('summary') if isinstance(saved.get('summary'), str) else ''
    state = saved.get('state')
    state = state if state in ('body', 'summary', 'empty', 'not_cached') else 'not_fetched'
    return {
        'owner_name': owner_name,
        'notice': {
            'supported': notice_supported,
            'state': state if notice_supported else 'unsupported',
            'body': (body or summary) if notice_supported else '',
            'has_images': bool(saved.get('has_images')),
            'fetched_at': saved.get('fetched_at'),
            'checked_at': saved.get('checked_at'),
            'last_error': saved.get('last_error', '') if notice_supported else '',
            'source_label': '手机缓存' if notice_supported else '',
        },
        # 当前群详情各渠道均未接入群内置顶消息读取；不借用朋友圈的置顶诊断字段。
        'pinned': {'supported': False},
    }
