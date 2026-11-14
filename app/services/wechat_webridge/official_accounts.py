"""公众号身份补齐：只使用账号匹配的发布者名称与手机返回的真实资料。"""
from __future__ import annotations

from typing import Any
from xml.etree import ElementTree

from sqlalchemy import and_, or_, select
from sqlalchemy.orm import Session

from app.core.time import now_shanghai
from app.models.entities import Channel, Conversation, TaskJob, WorkWechatContact
from app.services.identity_directory import looks_like_raw_id

PROFILE_RETRY_SECONDS = 600


def publisher_name(external_id: str, content: Any) -> str:
    """只读取文章阅读器的账号发布者，并校验账号；转发文章不能冒充当前联系人。

    文章封面 thumburl 与视频号 avatar 都不是公众号头像，不参与账号资料提取。
    XML 限长并拒绝 DTD/实体声明，异常消息仅跳过补资料，仍由原消息流程保存。
    """
    if not external_id.lower().startswith('gh_') or not isinstance(content, str):
        return ''
    if len(content) > 128_000 or '<!DOCTYPE' in content.upper() or '<!ENTITY' in content.upper():
        return ''
    try:
        root = ElementTree.fromstring(content.strip())
    except (ElementTree.ParseError, ValueError):
        return ''
    # 手机真实文章将账号发布者放在 mmreader 内，与 category/item 的文章及视频资料同级。
    # 固定完整路径，避免递归查找时把文章中的其他发布者当作当前公众号身份。
    publisher = root.find('appmsg/mmreader/publisher') if root.tag == 'msg' else None
    if publisher is None or (publisher.findtext('username') or '').strip() != external_id:
        return ''
    name = (publisher.findtext('nickname') or '').strip()
    return name[:255] if name and name != external_id and not looks_like_raw_id(name) else ''


def observe_message(db: Session, channel: Channel, normalized: dict[str, Any], conversation: Conversation) -> None:
    """基础资料在去重后补齐，不重写历史消息、不覆盖手机目录已确认的昵称或头像。"""
    message = normalized.get('message') or {}
    external_id = str(conversation.external_id or '')
    if (conversation.chat_type == 'group' or not external_id.lower().startswith('gh_')
            or message.get('sender_id') != external_id):
        return
    details = normalized.get('details') or {}
    name = publisher_name(external_id, details.get('content_xml') or message.get('content'))
    if not name:
        return
    contact = db.scalar(select(WorkWechatContact).where(
        WorkWechatContact.channel_id == channel.id, WorkWechatContact.contact_external_id == external_id,
    ))
    if contact is None:
        contact = WorkWechatContact(tenant_id=channel.tenant_id, channel_id=channel.id,
                                   contact_kind='official', contact_external_id=external_id,
                                   source='wechat_webridge', first_seen_at=now_shanghai())
        db.add(contact)
    # 发布者名称仅填补缺失值；目录资料有独立更新时间，不由历史文章回放覆盖。
    for field in ('name', 'display_name'):
        previous = getattr(contact, field)
        if not previous or previous == external_id or looks_like_raw_id(previous):
            setattr(contact, field, name)
    if not conversation.title or conversation.title == external_id or looks_like_raw_id(conversation.title):
        conversation.title = name
    if not message.get('sender_name') or message['sender_name'] == external_id:
        message['sender_name'] = name


def refresh_missing_profiles(db: Session, channel: Channel) -> None:
    """手机在线时补查已存在公众号会话；十分钟节流、在途合并，不在页面 GET 下发任务。"""
    from app.services.wechat_webridge import service

    runtime = service._runtime(channel)
    bridge = service.as_object(runtime.get('webridge'))
    if channel.is_enabled is False or 'official_accounts' not in bridge.get('capabilities', []):
        return
    state = service.as_object(runtime.get('official_account_profiles'))
    age = service._seconds_since(state.get('requested_at'))
    if age is not None and age < PROFILE_RETRY_SECONDS:
        return
    missing = db.scalar(select(Conversation.id).outerjoin(WorkWechatContact, and_(
        WorkWechatContact.channel_id == Conversation.channel_id,
        WorkWechatContact.contact_external_id == Conversation.external_id,
    )).where(
        Conversation.channel_id == channel.id, Conversation.chat_type == 'direct',
        Conversation.external_id.istartswith('gh_', autoescape=True),
        or_(WorkWechatContact.id.is_(None), WorkWechatContact.name.is_(None), WorkWechatContact.name == '',
            WorkWechatContact.name == Conversation.external_id, WorkWechatContact.avatar_url.is_(None),
            WorkWechatContact.avatar_url == ''),
    ).limit(1))
    if missing is None:
        return
    for job in db.scalars(select(TaskJob).where(TaskJob.job_type == service.JOB_TYPE,
                                               TaskJob.status.in_(('pending', 'running')))).all():
        payload = service.parse_job_payload(job)
        if (payload.get('channel_id') == channel.id
                and service.as_object(payload.get('command')).get('type') == 'official_accounts'):
            return
    service.enqueue_command(db, channel, command_name='official_accounts', args={}, priority=30)
    runtime['official_account_profiles'] = {**state, 'requested_at': now_shanghai().isoformat()}
    service._save_runtime(channel, runtime)
