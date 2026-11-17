"""微桥逐成员邀请资料：主动启动、小批量续排，进度与关系证据均可跨进程恢复。"""

from __future__ import annotations

import json
import uuid
from datetime import datetime, timedelta
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.core.tenant_context import require_tenant_id
from app.core.time import ensure_shanghai_datetime, now_shanghai
from app.models.entities import Channel, Chatroom, ChatroomMember, TaskJob
from app.services.chatroom_invitations import JOIN_INFO_SOURCE, _person_id, save_member_invitation
from app.services.plugin_schemas import parse_config_json
from app.services.task_queue import FINAL_STATUSES, parse_job_payload
from app.services.wechat_webridge import service, wakeup

COMMAND = 'room_member_join_info'
STATE_KEY = 'invitation_sync'
RELATED_TYPE = 'chatroom_invitation_sync'
BATCH_SIZE = 3
ALREADY_SETTLED = 'invitation_command_already_settled'


def state(room: Chatroom) -> dict[str, Any]:
    """同步元数据只保存在群上；成员关系仍使用唯一 invitation 对象，不复制一套图谱。"""
    return service.as_object(parse_config_json(room.extra_json).get(STATE_KEY))


def _save(room: Chatroom, current: dict[str, Any]) -> None:
    room.extra_json = service._merge_directory_extra(room.extra_json, **{STATE_KEY: current})


def support_reason(channel: Channel, room: Chatroom) -> str:
    """能力来自手机实际注册信息；没有声明时明确提示，不用旧成员接口假装已经查询。"""
    if channel.channel_type != 'wechat_webridge':
        return '当前渠道暂未接入邀请资料读取'
    if COMMAND not in service.command_catalog(channel)['reported_capabilities']:
        return '当前微桥版本未提供邀请资料读取，请更新手机插件后重新连接'
    if not channel.is_enabled:
        return '渠道已停用，请先启用渠道'
    if room.management_status == 'archived' or room.status == 'left':
        return '群已停止采集或已退出，无法同步当前邀请资料'
    return ''


def view(room: Chatroom) -> dict[str, Any]:
    """生成只读进度，不检查手机、不续排任务；普通 GET 与状态轮询均调用此函数。"""
    current = state(room)
    result = {key: int(current.get(key) or 0) for key in ('total', 'completed', 'known', 'unknown', 'failed')}
    status = current.get('status', 'idle')
    if status == 'running':
        summary = f"正在同步 {result['completed']} / {result['total']} 人，等待手机返回资料"
    elif status == 'completed':
        summary = f"已查询 {result['completed']} 人：查到邀请人 {result['known']} 人，未知 {result['unknown']} 人，失败 {result['failed']} 人"
    else:
        summary = '同步当前在群成员的邀请资料，退出页面后仍会继续'
    return {**result, 'status': status, 'run_id': current.get('run_id', ''), 'summary': summary,
            'updated_at': current.get('updated_at'), 'started_at': current.get('started_at')}


def _locked_room(db: Session, channel: Channel, room_id: int) -> Chatroom | None:
    """先保存本事务其他资料，再加群行锁；统一锁顺序防止重复提交或并发回报丢失进度。"""
    if channel.tenant_id != require_tenant_id():
        return None
    db.flush()
    return db.scalar(select(Chatroom).where(
        Chatroom.id == room_id, Chatroom.tenant_id == channel.tenant_id,
        Chatroom.channel_id == channel.id, Chatroom.bot_id == channel.bot_id,
    ).with_for_update().execution_options(populate_existing=True))


def _members(channel: Channel, room: Chatroom):
    return select(ChatroomMember).where(
        ChatroomMember.tenant_id == channel.tenant_id, ChatroomMember.channel_id == channel.id,
        ChatroomMember.bot_id == channel.bot_id, ChatroomMember.chatroom_id == room.id,
        ChatroomMember.status == 'active',
    )


def request_sync(db: Session, channel: Channel, room: Chatroom) -> dict[str, Any]:
    """只在明确操作时开始一轮；同群在途合并，固定成员主键上界避免新成员让本轮无限增长。"""
    locked = _locked_room(db, channel, room.id)
    if locked is None:
        raise ValueError('未找到当前群')
    reason = support_reason(channel, locked)
    if reason:
        raise ValueError(reason)
    if state(locked).get('status') == 'running':
        return view(locked)
    members = _members(channel, locked).subquery()
    total, upper = db.execute(select(func.count(members.c.id), func.max(members.c.id))).one()
    if not total:
        raise ValueError('暂无在群成员，请先同步群资料')
    now = now_shanghai().isoformat()
    _save(locked, {'run_id': uuid.uuid4().hex, 'status': 'running', 'cursor': 0, 'upper': int(upper),
                   'total': int(total), 'enqueued': 0, 'completed': 0, 'known': 0, 'unknown': 0, 'failed': 0,
                   'active': [], 'exhausted': False, 'started_at': now, 'updated_at': now})
    _advance_locked(db, channel, locked)
    return view(locked)


def _advance_locked(db: Session, channel: Channel, room: Chatroom) -> None:
    """在群锁内结算少量在途终态并补下一批；调用方提交后才能释放锁和通知设备。"""
    current = dict(state(room))
    if current.get('status') != 'running':
        return
    active = current.get('active', [])
    running = []
    # 只读取本轮最多三个任务，不扫描整张历史任务表。终态任务出 active 后不再重复计数。
    for job_id in active:
        job = db.scalar(select(TaskJob).where(
            TaskJob.id == job_id, TaskJob.tenant_id == channel.tenant_id,
            TaskJob.job_type == service.JOB_TYPE,
        ).with_for_update().execution_options(populate_existing=True))
        if job is not None and job.status not in FINAL_STATUSES:
            running.append(job_id)
            continue
        current['completed'] += 1
        if job is None or job.status != 'completed':
            current['failed'] += 1
        else:
            payload = service.as_object(parse_config_json(job.result_json).get('webridge'))
            result = service.as_object(payload.get('result'))
            result = service.as_object(result.get('result')) or result
            current['known' if _person_id(result.get('inviter_wxid')) else 'unknown'] += 1
    if not current.get('exhausted'):
        available = BATCH_SIZE - len(running)
        if available:
            members = list(db.scalars(_members(channel, room).where(
                ChatroomMember.id > current['cursor'], ChatroomMember.id <= current['upper'],
            ).order_by(ChatroomMember.id).limit(available)))
            for member in members:
                job = service.enqueue_command(
                    db, channel, command_name=COMMAND,
                    args={'room_id': room.external_id, 'wxid': member.member_external_id, 'refresh': True},
                    request_id=f"invite_{current['run_id']}_{member.id}", priority=18,
                )
                # 命令目标和同步代次固化在服务端任务中，不接受手机回报临时指定其他群或成员。
                data = parse_job_payload(job)
                data[STATE_KEY] = {'run_id': current['run_id'], 'chatroom_id': room.id, 'member_id': member.id}
                job.payload_json = json.dumps(data, ensure_ascii=False, sort_keys=True)
                job.related_type, job.related_id = RELATED_TYPE, room.id
                running.append(int(job.id))
                current['cursor'] = member.id
                current['enqueued'] += 1
            current['exhausted'] = len(members) < available
    current['active'] = running
    if not running and current['exhausted']:
        current['status'] = 'completed'
        # 轮次中途退群或删除的成员不再查询，最终分母以实际下发人数为准。
        current['total'] = current['enqueued']
        current['finished_at'] = now_shanghai().isoformat()
    current['updated_at'] = now_shanghai().isoformat()
    _save(room, current)


def resume_channel(db: Session, channel: Channel) -> None:
    """手机轮询时恢复用户已启动的轮次；进程重启、超时和关闭页面均不丢续排能力。"""
    if channel.tenant_id != require_tenant_id():
        return
    rooms = list(db.scalars(select(Chatroom).where(
        Chatroom.tenant_id == channel.tenant_id, Chatroom.channel_id == channel.id,
    ).order_by(Chatroom.id)))
    for room in rooms:
        if state(room).get('status') != 'running':
            continue
        locked = _locked_room(db, channel, room.id)
        if locked is not None:
            _advance_locked(db, channel, locked)


def _join_time(value: Any) -> datetime | None:
    """只解析渠道实际时间值；null、非法值和未来时刻保留未知，绝不填入任务/获取时间。"""
    try:
        if isinstance(value, bool) or value in (None, '', 0):
            return None
        if isinstance(value, (int, float)):
            moment = datetime.fromtimestamp(value / 1000 if value > 10**11 else value).astimezone()
        elif isinstance(value, str):
            moment = ensure_shanghai_datetime(datetime.fromisoformat(value.replace('Z', '+00:00')))
        else:
            return None
        return moment if moment.year >= 2011 and moment <= now_shanghai() + timedelta(minutes=5) else None
    except (ValueError, OverflowError, OSError):
        return None


def save_result(db: Session, channel: Channel, job: TaskJob, result: dict[str, Any], *, ok: bool) -> str:
    """在唯一命令结果路径落库，返回校验错误；失败及身份错配均不修改已有邀请资料。"""
    if job.tenant_id != channel.tenant_id:
        return 'invitation_job_tenant_mismatch'
    data = parse_job_payload(job)
    command = service.as_object(data.get('command'))
    params = service.as_object(command.get('params'))
    room_external_id, member_external_id = params.get('room_id'), params.get('wxid')
    if not isinstance(room_external_id, str) or not isinstance(member_external_id, str):
        return 'invitation_result_target_mismatch'
    room_id = db.scalar(select(Chatroom.id).where(
        Chatroom.tenant_id == channel.tenant_id, Chatroom.channel_id == channel.id,
        Chatroom.bot_id == channel.bot_id, Chatroom.external_id == room_external_id,
    ))
    room = _locked_room(db, channel, room_id) if room_id else None
    if room is None:
        return 'invitation_room_not_found'
    # 首次终态检查发生在通用入口，另一个并发回报可能已在等待群锁期间提交。
    # 按“群 -> 任务”统一锁序重新确认，防止重复回报把已完成任务再次改写为失败。
    db.refresh(job, with_for_update=True)
    if job.status in FINAL_STATUSES:
        return ALREADY_SETTLED
    sync = service.as_object(data.get(STATE_KEY))
    if sync and (sync.get('chatroom_id') != room.id or sync.get('run_id') != state(room).get('run_id')):
        return 'invitation_sync_superseded'
    if not ok:
        return ''
    if result.get('room_id') != room_external_id or result.get('wxid') != member_external_id:
        return 'invitation_result_target_mismatch'
    member = db.scalar(select(ChatroomMember).where(
        ChatroomMember.tenant_id == channel.tenant_id, ChatroomMember.channel_id == channel.id,
        ChatroomMember.bot_id == channel.bot_id, ChatroomMember.chatroom_id == room.id,
        ChatroomMember.member_external_id == member_external_id,
    ).with_for_update().execution_options(populate_existing=True))
    if member is None or sync and sync.get('member_id') != member.id:
        return 'invitation_member_not_found'
    if result.get('ok') is False or result.get('status') in ('failed', 'error', 'unsupported'):
        return 'invitation_query_failed'
    inviter_raw = result.get('inviter_wxid')
    inviter = _person_id(inviter_raw)
    if (inviter_raw not in (None, '') and not inviter) or inviter == member_external_id:
        return 'invitation_inviter_invalid'
    method = result.get('join_type')
    # 真实回报 join_type=unknown，描述里提到二维码也不擅自改变结构化类型。
    method = method if method in ('invite', 'qr') else 'unknown'
    save_member_invitation(
        member, inviter, source=JOIN_INFO_SOURCE, recorded_at=job.created_at,
        fetched_at=now_shanghai(), joined_at=_join_time(result.get('join_time')),
        method=method, event_key=service.text(command.get('request_id')),
        inviter_name=service.text(result.get('inviter_name')),
        description=result.get('join_description') if isinstance(result.get('join_description'), str) else '',
    )
    return ''


def after_result(db: Session, channel: Channel, job: TaskJob) -> None:
    """终态已提交后再结算进度；若进程在两个事务间退出，轮询维护仍能根据终态补结算。"""
    sync = service.as_object(parse_job_payload(job).get(STATE_KEY))
    if not sync:
        return
    room = _locked_room(db, channel, sync.get('chatroom_id'))
    if room is None or state(room).get('run_id') != sync.get('run_id'):
        return
    _advance_locked(db, channel, room)
    db.commit()
    wakeup.notify(int(channel.id))
