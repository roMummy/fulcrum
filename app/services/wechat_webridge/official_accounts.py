"""通过公众号专用列表接口补齐消息页的名称与头像,独立于普通好友目录。"""

from __future__ import annotations

import json
from typing import Any
from xml.etree import ElementTree

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.time import now_shanghai
from app.models.entities import Channel, Conversation, TaskJob, WorkWechatContact
from app.services.identity_directory import looks_like_raw_id
from app.services.task_queue import parse_job_payload
from app.services.wechat_webridge import service

SYNC_PRIORITY = 18
PAGE_SIZE = 100
REFRESH_SECONDS = 3600
RETRY_SECONDS = 300
STATE_KEY = "official_account_directory"


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


def _save_state(channel: Channel, **updates: Any) -> None:
    runtime = service._runtime(channel)
    runtime[STATE_KEY] = {**service.as_object(runtime.get(STATE_KEY)), **updates}
    service._save_runtime(channel, runtime)


def _enqueue_page(db: Session, channel: Channel, offset: int) -> TaskJob:
    job = service.enqueue_command(
        db, channel, command_name="official_accounts",
        args={"type": "all", "limit": PAGE_SIZE, "offset": offset}, priority=SYNC_PRIORITY,
    )
    data = parse_job_payload(job)
    # 只为内部完整同步续页;通用命令桥的关键词/类型筛选仍是单次查询。
    data[STATE_KEY] = True
    job.payload_json = json.dumps(data, ensure_ascii=False, sort_keys=True)
    _save_state(channel, requested_at=now_shanghai().isoformat())
    db.flush()
    return job


def request_sync(db: Session, channel: Channel, *, force: bool = False) -> TaskJob | None:
    """手机轮询时自动补齐;失败退避,未完成的同渠道任务合并。调用方负责提交。"""

    if channel.is_enabled is False or channel.deleted_at is not None:
        return None
    db.flush()
    jobs = db.scalars(select(TaskJob).where(
        TaskJob.job_type == service.JOB_TYPE, TaskJob.status.in_(("pending", "running")),
        TaskJob.priority == SYNC_PRIORITY,
    )).all()
    for job in jobs:
        data = parse_job_payload(job)
        if int(data.get("channel_id") or 0) == int(channel.id) and data.get(STATE_KEY):
            return job
    state = service.as_object(service._runtime(channel).get(STATE_KEY))
    if not force:
        age = service._seconds_since(state.get("synced_at"))
        since_request = service._seconds_since(state.get("requested_at"))
        if (age is not None and age < REFRESH_SECONDS) or (since_request is not None and since_request < RETRY_SECONDS):
            return None
    return _enqueue_page(db, channel, 0)


def successful_result(payload: dict[str, Any], result: dict[str, Any]) -> bool:
    """登录/联系人未就绪及异常响应不能当作成功的空目录。"""

    wrapper = service.as_object(payload.get("result"))
    # 任一执行层明确失败都必须保留；不能解包后只读取最内层的成功诊断。
    return (
        payload.get("ok", wrapper.get("ok", result.get("ok"))) is True
        and wrapper.get("ok") is not False
        and result.get("ok") is not False
        and result.get("status", "ok") == "ok"
        and isinstance(result.get("items"), list)
        and all(isinstance(item, dict) for item in result["items"])
    )


def after_result(db: Session, channel: Channel, data: dict[str, Any], result: dict[str, Any]) -> None:
    """按返回的实际条数续取下一页,最后一页才标记完整同步成功。"""

    if not data.get(STATE_KEY):
        return
    args = service.as_object(service.as_object(data.get("command")).get("params"))
    offset = args.get("offset", 0)
    if result.get("offset", offset) != offset:
        return
    # 部分真机版本把布尔值序列化为 0/1,只接受明确的布尔或这两个整数。
    has_more = result.get("has_more")
    if type(has_more) not in (bool, int) or has_more not in (0, 1):
        return
    if has_more:
        items = result["items"]
        # 空页不能推进游标;异常响应留待退避后重试,不无限下发相同分页。
        next_offset = offset + len(items)
        if items and next_offset <= 4294967295:
            _enqueue_page(db, channel, next_offset)
    else:
        _save_state(channel, synced_at=now_shanghai().isoformat())
