from __future__ import annotations

from collections.abc import Callable, Iterable
from typing import Any

from sqlalchemy.orm import Session

from app.core.logging import get_logger
from app.models.entities import Channel, Conversation
from app.services.task_queue import enqueue_job
from app.services.workwechat_cloud_poll import notify_cloud_job_enqueued, resolve_outbound_job_type


logger = get_logger(__name__)


def enqueue_outbound_jobs(
    db: Session,
    *,
    conversation: Conversation,
    channel: Channel,
    payloads: Iterable[dict[str, Any]],
    plugin_code: str,
    related_type: str,
    related_id: int | None,
    priority: int,
) -> list[int]:
    job_ids: list[int] = []
    for payload in payloads:
        job = enqueue_job(
            db,
            job_type=resolve_outbound_job_type(channel),
            payload={
                "channel_id": channel.id,
                "conversation_id": conversation.id,
                "payload": payload,
                "plugin_code": plugin_code,
            },
            priority=priority,
            related_type=related_type,
            related_id=related_id,
        )
        notify_cloud_job_enqueued(channel, job)
        job_ids.append(job.id)
    return job_ids


def enqueue_outbound_jobs_by_conversation(
    db: Session,
    *,
    conversations: Iterable[Conversation],
    plugin_code: str,
    related_type: str,
    related_id: int | None,
    priority: int,
    payload_builder: Callable[[Conversation, Channel], list[dict[str, Any]]],
    log_context: dict[str, Any] | None = None,
) -> dict[str, Any]:
    job_ids: list[int] = []
    failed_targets: list[dict[str, Any]] = []
    pushed_conversation_count = 0

    for conversation in conversations:
        try:
            channel = db.get(Channel, int(conversation.channel_id or 0))
            if channel is None:
                raise ValueError("channel_not_found")
            if not channel.is_enabled:
                raise ValueError("channel_disabled")

            payloads = payload_builder(conversation, channel)
            job_ids.extend(
                enqueue_outbound_jobs(
                    db,
                    conversation=conversation,
                    channel=channel,
                    payloads=payloads,
                    plugin_code=plugin_code,
                    related_type=related_type,
                    related_id=related_id,
                    priority=priority,
                )
            )
            pushed_conversation_count += 1
        except Exception as exc:
            failed_target = {
                "conversation_id": conversation.id,
                "channel_id": int(conversation.channel_id or 0),
                "external_id": str(conversation.external_id or "").strip(),
                "error": str(exc),
            }
            failed_targets.append(failed_target)
            logger.warning(
                "outbound batch skipped failed conversation",
                extra={
                    "plugin_code": plugin_code,
                    **(log_context or {}),
                    **failed_target,
                },
            )
            db.rollback()

    return {
        "job_ids": job_ids,
        "failed_targets": failed_targets,
        "pushed_conversation_count": pushed_conversation_count,
    }


def enqueue_proactive_push_batch(
    db: Session,
    *,
    conversations: Iterable[Conversation],
    plugin_code: str,
    related_type: str,
    related_id: int | None,
    priority: int,
    payload_builder: Callable[[Conversation, Channel], list[dict[str, Any]]],
    log_context: dict[str, Any] | None = None,
) -> dict[str, Any]:
    return enqueue_outbound_jobs_by_conversation(
        db,
        conversations=conversations,
        plugin_code=plugin_code,
        related_type=related_type,
        related_id=related_id,
        priority=priority,
        payload_builder=payload_builder,
        log_context=log_context,
    )
