from __future__ import annotations

from datetime import datetime, timedelta

from sqlalchemy import select

from app.core.db import SessionLocal
from app.core.time import ensure_shanghai_datetime, now_shanghai
from app.models.entities import ChatroomMember
from app.services.plugin_runtime.contracts import PluginExecutionRequest, PluginExecutionResult


DEFAULT_COMMANDS = ("潜水排行", "inactive_rank", "silent_rank")


def execute_action(request: PluginExecutionRequest) -> PluginExecutionResult:
    if request.action == "reload":
        return PluginExecutionResult(ok=True, status="running", message="inactive_rank runtime is ready", output={})
    if request.action != "execute":
        return PluginExecutionResult(ok=False, status="unsupported", message=f"不支持的动作: {request.action}")

    group_id = _resolve_group_id(request.payload)
    if not group_id:
        return PluginExecutionResult(ok=False, status="error", message="潜水排行仅支持群聊。")

    days = max(_coerce_int(request.payload.get("days"), 60), 1)
    limit = max(_coerce_int(request.payload.get("limit"), 10), 1)
    now = now_shanghai()
    threshold = now - timedelta(days=days)

    with SessionLocal() as db:
        members = db.scalars(
            select(ChatroomMember).where(ChatroomMember.chatroom_id.in_(select_chatroom_ids(db, group_id)))
        ).all()
        rows: list[dict[str, object]] = []
        seen_user_ids: set[str] = set()
        for member in members:
            user_id = str(member.member_external_id or "").strip()
            if not user_id or user_id in seen_user_ids:
                continue
            seen_user_ids.add(user_id)
            last_active = member.last_active_at
            if isinstance(last_active, datetime) and last_active.tzinfo is None:
                last_active = ensure_shanghai_datetime(last_active)
            if last_active and last_active >= threshold:
                continue
            inactivity_days = 999999 if last_active is None else max(int((now - last_active).total_seconds() // 86400), 0)
            rows.append(
                {
                    "user_id": user_id,
                    "name": str(member.display_name or member.nickname or user_id).strip() or user_id,
                    "last_active_at": last_active,
                    "inactivity_days": inactivity_days,
                    "never_spoken": last_active is None,
                }
            )

    rows.sort(key=lambda item: (-(int(item["inactivity_days"])), str(item["name"])))
    ranking = rows[:limit]
    if not ranking:
        return PluginExecutionResult(
            ok=True,
            status="completed",
            message="没有符合条件的潜水成员。",
            output={
                "provider_status": "empty",
                "days": days,
                "limit": limit,
                "reply_text": f"{days} 天内没有符合条件的潜水成员。",
                "replies": [{"type": "text", "content": f"{days} 天内没有符合条件的潜水成员。"}],
            },
        )

    lines = [f"潜水排行（近 {days} 天未发言，前 {limit} 名）"]
    for index, item in enumerate(ranking, start=1):
        status = "从未发言" if bool(item["never_spoken"]) else f"{int(item['inactivity_days'])} 天未发言"
        lines.append(f"{index}. {item['name']} - {status}")
    reply_text = "\n".join(lines)
    return PluginExecutionResult(
        ok=True,
        status="completed",
        message="潜水排行生成成功。",
        output={
            "provider_status": "ranking",
            "days": days,
            "limit": limit,
            "ranking": ranking,
            "reply_text": reply_text,
            "replies": [{"type": "text", "content": reply_text}],
        },
    )


def select_chatroom_ids(db, group_id: str):
    from app.models.entities import Chatroom

    return select(Chatroom.id).where(Chatroom.external_id == group_id)


def _resolve_group_id(payload: dict[str, object]) -> str:
    if str(payload.get("chat_type") or "").strip() != "group":
        return ""
    return str(payload.get("conversation_external_id") or payload.get("room_id") or "").strip()


def _coerce_int(value: object, default: int) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return default
