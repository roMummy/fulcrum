"""微桥新群自动同步:bot 被拉进新群后,自动把群资料(群名、人数)和成员同步进来,不用手动点"同步群组"。

为什么不能只等"入群事件":协议文档没有"本账号被拉进群"的专门事件,`group_member_join` 是否对本账号触发文档也明确
写了"未说明,需真机测试"(新群第一次被看到时只建快照、不发入群事件)。所以这里用三种触发,全部汇到同一个入口
`request_group_sync`(去重 + 防抖),下发 `groups_all`(协议一次返回全部群,没有分页):

1. 定期对账(最可靠,安静的新群也能发现):手机每次长轮询时顺带检查,距上次同步超过 PERIODIC_SECONDS 就下发一次;
2. 收到"还没登记的群"的消息:群里有人说话说明 bot 已经在群里了;
3. 入群相关事件和系统消息:`group_member_join`、"邀请你加入群聊"这类,尽快发现。

`groups_all` 的结果回来后(见 service.accept_command_result)由 `after_groups_result` 先做"退群对账",再为"新发现的群"和"还没有任何成员记录的群"
继续下发 `room_members`,每轮最多 MAX_MEMBER_SYNC_PER_ROUND 个,避免一次对手机下发太多;同一个群 MEMBER_RETRY_SECONDS 内不重复请求,
手机不支持或失败时不会每轮都重试。

同步状态保存在渠道运行信息 runtime_info_json["group_sync"] 里。

退群/被移出:协议同样没有"本账号被移出群"的专门事件,所以用同一份 `groups_all` 全量快照对账:曾经同步过(status=synced)的群,
连续 MISS_LIMIT 次完整快照里都没有,就认定 bot 已不在群里,标记 status=left、is_active=False(群功能和 AI 随之停掉,
运行模式保持用户原来的选择)。如果之前刚收到"你被移出群聊/你已退出群聊/群聊已解散"这类系统消息,一次缺失就够。
带关键词过滤、total 对不上、被截断的结果不是完整快照,不参与对账。群再次出现在快照里时自动恢复原来的启用状态。
"""

from __future__ import annotations

import json
import re
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.logging import get_logger
from app.core.time import now_shanghai
from app.models.entities import Channel, Chatroom, ChatroomMember, TaskJob
from app.services.task_queue import parse_job_payload
from app.services.wechat_webridge import service, wakeup

logger = get_logger(__name__)

# 比普通命令(默认 90)略低,不抢出站消息;比设备/账号状态(6、7)高,保证同步不被饿死。
GROUP_SYNC_PRIORITY = 20
MEMBER_SYNC_PRIORITY = 19
# 定期对账间隔:groups_all 一次返回全部群,代价很小。
PERIODIC_SECONDS = 600
# 事件触发的最小间隔(防抖):一个群里连续多条消息、一次拉进多个群,都只下发一次。
EVENT_MIN_GAP_SECONDS = 45
# 每轮最多为多少个群下发成员同步。
MAX_MEMBER_SYNC_PER_ROUND = 8
# 同一个群的成员同步失败/没拿到成员后,多久之后才允许再试。
MEMBER_RETRY_SECONDS = 3600
# 状态里记录"最近请求过成员同步的群"的上限,防止无限增长。
MEMBER_ATTEMPT_LIMIT = 300

# 系统消息里"本账号被拉进群/加入群"的常见说法。宽一点没关系:误判只会多一次被防抖限制的 groups_all。
JOIN_TEXT = re.compile(r"邀请你|你被.{0,40}(邀请|拉).{0,8}(加入|进)|你已(经)?加入|你加入了|加入了?群聊|拉你(进|入)")
JOIN_EVENTS = {"group_member_join"}
# 系统消息里"本账号退群/被移出群/群解散"的常见说法。
LEAVE_TEXT = re.compile(r"你被.{0,40}移出.{0,4}群聊|将你移出.{0,4}群聊|你已(经)?退出.{0,4}群聊|你退出了.{0,4}群聊|群聊已(被)?解散|你已被移出")
# 连续这么多次完整的群列表快照里都没有这个群,才认定 bot 已不在群里(单次异常返回不会误伤)。
MISS_LIMIT = 2
# 看到退群/被移出的系统消息之后多久内,一次快照里缺失就足够认定。
SUSPECT_WINDOW_SECONDS = 1800


def _state(channel: Channel) -> dict[str, Any]:
    return service.as_object(service._runtime(channel).get("group_sync"))


def _save_state(channel: Channel, state: dict[str, Any]) -> None:
    runtime = service._runtime(channel)
    runtime["group_sync"] = state
    service._save_runtime(channel, runtime)


def _has_pending(db: Session, channel: Channel, priority: int, *, room_id: str = "") -> bool:
    """同渠道是否已有这类命令还没做完(room_id 不为空时只看这个群)。用优先级当标记,避免扫描整个队列。"""

    cid = int(channel.id)
    for job in db.scalars(
        select(TaskJob).where(TaskJob.job_type == service.JOB_TYPE, TaskJob.status.in_(("pending", "running")), TaskJob.priority == priority)
    ).all():
        data = parse_job_payload(job)
        if int(data.get("channel_id") or 0) != cid:
            continue
        if room_id and service.text(service.as_object(service.as_object(data.get("command")).get("params")).get("room_id")) != room_id:
            continue
        return True
    return False


def request_group_sync(db: Session, channel: Channel, *, reason: str, min_gap_seconds: float = EVENT_MIN_GAP_SECONDS) -> TaskJob | None:
    """请求同步群列表。已有未完成的同步、或距上次请求不到 min_gap_seconds 时不重复下发,返回 None。调用方负责提交。"""

    state = _state(channel)
    since = service._seconds_since(state.get("requested_at"))
    if since is not None and since < min_gap_seconds:
        return None
    if _has_pending(db, channel, GROUP_SYNC_PRIORITY):
        return None
    job = service.enqueue_command(db, channel, command_name="groups_all", args={}, priority=GROUP_SYNC_PRIORITY)
    _save_state(channel, {**state, "requested_at": now_shanghai().isoformat(), "reason": reason})
    logger.info("webridge group sync requested", extra={"channel_id": channel.id, "reason": reason, "job_id": job.id})
    return job


def reconcile_if_due(db: Session, channel: Channel) -> None:
    """长轮询维护时调用:距上次同步超过 PERIODIC_SECONDS 就下发一次群列表同步。"""

    request_group_sync(db, channel, reason="periodic", min_gap_seconds=PERIODIC_SECONDS)


def observe_inbound_event(db: Session, channel: Channel, normalized: dict[str, Any], conversation: Any) -> None:
    """入站事件触发:未登记的群有人说话、入群事件、"邀请你加入群聊"这类系统消息。调用方负责提交。"""

    event_type = service.text(normalized.get("event_type"))
    message = service.as_object(normalized.get("message"))
    message_type = service.text(message.get("message_type"))
    reason = ""
    is_group = service.text(conversation.chat_type) == "group"
    if message_type == "system" and is_group and LEAVE_TEXT.search(service.text(message.get("content"))):
        # 先记下"疑似被移出",让下一次群列表快照里缺失这个群时一次就能认定;再请求一次同步确认。
        suspects = {str(k): str(v) for k, v in service.as_object(_state(channel).get("suspect_left")).items()}
        suspects[service.text(conversation.external_id)] = now_shanghai().isoformat()
        _save_state(channel, {**_state(channel), "suspect_left": suspects})
        reason = "leave_system_message"
    elif event_type in JOIN_EVENTS:
        reason = "member_join_event"
    elif message_type == "system" and JOIN_TEXT.search(service.text(message.get("content"))):
        reason = "join_system_message"
    elif is_group and service.text(conversation.external_id).endswith("@chatroom"):
        known = db.scalar(select(Chatroom.id).where(Chatroom.channel_id == channel.id, Chatroom.external_id == conversation.external_id).limit(1))
        if known is None:
            reason = "unknown_group_message"
    if not reason:
        return
    if request_group_sync(db, channel, reason=reason) is not None:
        # 事件在入站事务里处理,命令要等事务提交之后才查得到:延迟一点再叫醒挂起中的手机轮询。
        wakeup.notify(int(channel.id), 2.0)


def known_room_ids(db: Session, channel: Channel) -> set[str]:
    return set(db.scalars(select(Chatroom.external_id).where(Chatroom.channel_id == channel.id)).all())


def after_groups_result(db: Session, channel: Channel, known_before: set[str], seen_ids: set[str] | None = None) -> list[str]:
    """groups_all 结果已写入之后调用:先做退群对账,再为新发现的群和还没有成员记录的群下发 room_members。

    seen_ids 是这次完整快照里的全部群 ID;为 None 表示结果不是完整快照(带关键词、被截断等),不做退群对账。
    返回本轮下发成员同步的群 ID。
    """

    now = now_shanghai()
    if seen_ids is not None:
        reconcile_departures(db, channel, seen_ids, now)
    state = _state(channel)
    attempts = {str(key): str(value) for key, value in service.as_object(state.get("member_requested_at")).items()}
    rooms = db.scalars(select(Chatroom).where(Chatroom.channel_id == channel.id).order_by(Chatroom.id.asc())).all()
    with_members = set(db.scalars(select(ChatroomMember.chatroom_id).where(ChatroomMember.channel_id == channel.id).distinct()).all())
    new_ids = [room.external_id for room in rooms if room.external_id not in known_before]
    # 新发现的群优先,其次是还没有任何成员记录的群(包括以前同步失败过的),已归档(完全关闭)的群不拉成员。
    targets: list[str] = list(new_ids)
    targets += [room.external_id for room in rooms if room.id not in with_members and room.external_id not in targets and room.management_status != "archived" and room.status != "left"]
    requested: list[str] = []
    for room_id in targets:
        if len(requested) >= MAX_MEMBER_SYNC_PER_ROUND:
            break
        last = service._seconds_since(attempts.get(room_id))
        if room_id not in new_ids and last is not None and last < MEMBER_RETRY_SECONDS:
            continue
        if _has_pending(db, channel, MEMBER_SYNC_PRIORITY, room_id=room_id):
            continue
        service.enqueue_command(db, channel, command_name="room_members", args={"room_id": room_id}, priority=MEMBER_SYNC_PRIORITY)
        attempts[room_id] = now.isoformat()
        requested.append(room_id)
    # 只保留最近的记录,防止无限增长
    if len(attempts) > MEMBER_ATTEMPT_LIMIT:
        attempts = dict(sorted(attempts.items(), key=lambda item: item[1])[-MEMBER_ATTEMPT_LIMIT:])
    _save_state(channel, {**state, "last_done_at": now.isoformat(), "last_new_groups": len(new_ids), "member_requested_at": attempts})
    if new_ids or requested:
        names = {room.external_id: (room.name or room.external_id) for room in rooms}
        logger.info(
            "webridge new groups synced",
            extra={
                "channel_id": channel.id,
                "new_groups": [names.get(item, item) for item in new_ids][:10],
                "new_group_count": len(new_ids),
                "member_sync_requested": len(requested),
            },
        )
    return requested


def complete_snapshot_ids(command: dict[str, Any], result: dict[str, Any]) -> set[str] | None:
    """从 groups_all 的结果里取出"全部群 ID";结果不是完整快照时返回 None(不参与退群对账)。

    带关键词过滤的结果只是一部分;total 和条数对不上、或带"被截断/还有更多"标记也不是完整快照。
    """

    params = service.as_object(command.get("params") or command.get("args"))
    if service.text(params.get("keyword")):
        return None
    items = service._result_items(result, "groups", "items", "rooms")
    total = result.get("total")
    if isinstance(total, int) and not isinstance(total, bool) and total != len(items):
        return None
    if result.get("truncated") or result.get("has_more"):
        return None
    ids = {service._directory_id(item, "account_id", "room_id", "chatroom_id", "user_name", "wxid", "id") for item in items}
    ids.discard("")
    return ids


def mark_left(room: Chatroom, now: Any, *, reason: str) -> None:
    """bot 已不在这个群:status=left、is_active=False;运行模式不动,并记下退群前是否启用,重新加入时据此恢复。"""

    try:
        extra = json.loads(room.extra_json or "{}")
    except (TypeError, ValueError):
        extra = {}
    if not isinstance(extra, dict):
        extra = {}
    extra["left"] = {"at": now.isoformat(), "was_active": bool(room.is_active), "reason": reason}
    room.extra_json = json.dumps(extra, ensure_ascii=False, sort_keys=True)
    room.status = "left"
    room.is_active = False
    logger.info("webridge group left", extra={"channel_id": room.channel_id, "group": room.name or room.external_id, "reason": reason})


def restore_if_returned(room: Chatroom) -> bool:
    """群重新出现在快照里:如果它之前被标记为已退出,恢复退群前的启用状态。返回是否发生了恢复。调用方随后会把 status 写回 synced。"""

    if room.status != "left":
        return False
    try:
        extra = json.loads(room.extra_json or "{}")
    except (TypeError, ValueError):
        extra = {}
    left = service.as_object(extra.get("left")) if isinstance(extra, dict) else {}
    room.is_active = bool(left.get("was_active"))
    logger.info("webridge group rejoined", extra={"channel_id": room.channel_id, "group": room.name or room.external_id, "restored_active": room.is_active})
    return True


def reconcile_departures(db: Session, channel: Channel, seen_ids: set[str], now: Any) -> list[str]:
    """用完整群快照对账:曾经同步过的群连续缺失 MISS_LIMIT 次(或刚收到被移出的系统消息后缺失 1 次)就标记为已退出。返回刚标记的群名。"""

    state = _state(channel)
    missing = {str(k): int(v) for k, v in service.as_object(state.get("missing")).items()}
    suspects = {str(k): str(v) for k, v in service.as_object(state.get("suspect_left")).items()}
    left_names: list[str] = []
    for room in db.scalars(select(Chatroom).where(Chatroom.channel_id == channel.id, Chatroom.status == "synced")).all():
        room_id = room.external_id
        if room_id in seen_ids:
            missing.pop(room_id, None)
            suspects.pop(room_id, None)
            continue
        missing[room_id] = missing.get(room_id, 0) + 1
        age = service._seconds_since(suspects.get(room_id))
        suspected = age is not None and age <= SUSPECT_WINDOW_SECONDS
        if missing[room_id] >= (1 if suspected else MISS_LIMIT):
            mark_left(room, now, reason="left_message_then_missing" if suspected else "missing_from_group_list")
            left_names.append(room.name or room_id)
            missing.pop(room_id, None)
            suspects.pop(room_id, None)
    # 计数只保留还在"已同步"状态的群,过期的怀疑记录也清掉
    synced = set(db.scalars(select(Chatroom.external_id).where(Chatroom.channel_id == channel.id, Chatroom.status == "synced")).all())
    missing = {key: value for key, value in missing.items() if key in synced}
    suspects = {key: value for key, value in suspects.items() if (service._seconds_since(value) or 0) <= SUSPECT_WINDOW_SECONDS * 2}
    _save_state(channel, {**state, "missing": missing, "suspect_left": suspects})
    return left_names
