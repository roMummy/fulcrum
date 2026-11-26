"""抖音 URL 视频由微桥探测大小，只有明确超限才将同一条待发回复改成链接卡片。"""

import json
import re

from sqlalchemy import select, update
from sqlalchemy.orm import Session

from app.models.entities import Channel, Conversation, Message, TaskJob
from app.services.plugin_scopes import plugin_group_conversation_enabled
from app.services.task_queue import parse_job_payload

from .contract import as_object, text

MIB = 1024**2
_SIZE_ERROR = re.compile(r"video exceeds max_size_bytes limit \(([0-9]{1,20}) > ([0-9]{1,20})\)")


class OversizeCard(dict):
    """只由平台在验证播放地址后创建；普通接口传入的 JSON 字典不能启用此发送策略。"""


def configured_limit_mib(value: object) -> float:
    # 与 JSON 数字的安全整数范围对齐，量化到整字节，让手机回报可与原命令精确比较。
    from app.services.plugin_runtime.replies import PluginReplyError

    if type(value) not in (int, float) or not 1 <= value < 2**33:
        raise PluginReplyError("微桥视频直发上限必须是不小于 1 MiB 的有效数值")
    return int(value * MIB) / MIB


def queue_card_if_oversize(db: Session, channel: Channel, job: TaskJob, data: dict, error: str) -> bool:
    card = as_object(data.get("oversize_card"))
    cmd = as_object(data.get("command"))
    args = as_object(cmd.get("params"))
    limit = args.get("max_size_mb")
    if not card or cmd.get("type") != "send_video_from_url" or type(limit) not in (int, float):
        return False
    match = _SIZE_ERROR.fullmatch(error)
    if not match or int(match[1]) <= int(match[2]) or int(match[2]) != limit * MIB:
        return False
    conv = db.get(Conversation, data["conversation_id"])
    if (not conv or not channel.is_enabled or conv.channel_id != channel.id or conv.bot_id != channel.bot_id
            or conv.tenant_id != channel.tenant_id or job.tenant_id != channel.tenant_id
            or conv.external_id != args.get("receiver") or not args.get("source")
            or not plugin_group_conversation_enabled(db, channel=channel, conversation=conv, plugin_code="douyin_parser")):
        return False

    # 原任务的终态和卡片入队同一事务提交；并发或重复回执不能再创建第二条卡片。
    won = db.execute(update(TaskJob).where(TaskJob.id == job.id, TaskJob.tenant_id == channel.tenant_id,
                                           TaskJob.status == "running").values(status="failed")).rowcount
    if not won:
        return True
    message = db.scalar(select(Message).where(
        Message.tenant_id == channel.tenant_id, Message.conversation_id == conv.id, Message.direction == "out",
        Message.message_type == "video", Message.external_status == "pending",
        Message.raw_request_json.contains(cmd["request_id"])))
    if not message:
        return False
    raw = json.loads(message.raw_request_json)
    if raw.get("request_id") != cmd["request_id"] or raw.get("job_id") != job.id:
        return False
    from .service import enqueue_message_command

    # 仅使用已验证的原任务地址、目标和作品资料，不使用回执提供的 URL 或收件人。
    card_payload = {
        "message_type": "link",
        "link_url": args["source"],
        "link_title": text(card.get("title")),
        "link_description": text(card.get("description")),
    }
    thumb_url = text(card.get("thumb_url"))
    if thumb_url:
        card_payload["thumb_url"] = thumb_url
    try:
        followup = enqueue_message_command(db, channel, conv, card_payload, plugin_code="douyin_parser")
    except ValueError:
        # 原群已退出等情况仍按原视频发送失败结算。
        return False
    followup.dedupe_key = f"video-size-card:{job.id}"
    card_request = parse_job_payload(followup)["command"]["request_id"]
    message.message_type = "link"
    message.content = text(card.get("title")) or "[link]"
    message.raw_request_json = json.dumps({**raw, "job_id": followup.id, "request_id": card_request,
                                           "oversize_video_job_id": job.id}, ensure_ascii=False)
    return True
