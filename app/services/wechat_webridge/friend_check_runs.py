"""好友检测的持久队列、结果与恢复；清空边界只由明确的用户操作推进。"""

from __future__ import annotations

import json
import math
import uuid
from typing import Any

from sqlalchemy import delete, select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session
from sqlalchemy.orm.attributes import set_committed_value

from app.core.time import ensure_shanghai_datetime, now_shanghai
from app.models.entities import Channel, FriendCheckItem, FriendCheckRun, TaskJob, WorkWechatContact
from app.services.task_queue import parse_job_payload
from . import friend_actions, friend_check, wakeup
from .contract import JOB_TYPE, as_object

FriendCheckError = friend_check.FriendCheckError


def _object(raw: str | None) -> dict:
    try:
        value = json.loads(raw or "{}")
    except (TypeError, ValueError):
        return {}
    return value if isinstance(value, dict) else {}


def _dump(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"))


def _items(db: Session, run: FriendCheckRun) -> list[FriendCheckItem]:
    return list(db.scalars(select(FriendCheckItem).where(
        FriendCheckItem.tenant_id == run.tenant_id, FriendCheckItem.run_id == run.id,
    ).order_by(FriendCheckItem.position)).all())


def _generation(run: FriendCheckRun, generation: str | None) -> None:
    if not generation or generation != run.generation:
        raise FriendCheckError(409, "检测记录已经变化，请重新读取后继续")


def _lock(db: Session, run: FriendCheckRun) -> None:
    # SQLite 的 FOR UPDATE 不加行锁，使用版本条件更新，防止旧页面或迟到结果覆盖清空操作。
    revision = run.revision
    changed = db.execute(update(FriendCheckRun).where(
        FriendCheckRun.id == run.id, FriendCheckRun.tenant_id == run.tenant_id,
        FriendCheckRun.generation == run.generation, FriendCheckRun.revision == revision,
    ).values(revision=revision + 1), execution_options={"synchronize_session": False}).rowcount
    if changed != 1:
        db.rollback()
        raise FriendCheckError(409, "检测记录正在更新，请重新读取后继续")
    set_committed_value(run, "revision", revision + 1)


def _seed(db: Session, channel: Channel, run: FriendCheckRun, *, history: bool) -> None:
    contacts = list(db.scalars(select(WorkWechatContact).where(
        WorkWechatContact.channel_id == channel.id, WorkWechatContact.contact_kind == "friend",
    ).order_by(WorkWechatContact.display_name, WorkWechatContact.id)).all())
    latest: dict[int, TaskJob] = {}
    if history:
        by_id = {contact.id: contact for contact in contacts}
        for job in db.scalars(select(TaskJob).where(
            TaskJob.job_type == JOB_TYPE, TaskJob.related_type == friend_check.RELATED_TYPE,
            TaskJob.tenant_id == channel.tenant_id,
        ).order_by(TaskJob.id.desc())).all():
            contact = by_id.get(job.related_id)
            if contact is None or contact.id in latest:
                continue
            payload = parse_job_payload(job)
            command = as_object(payload.get("command"))
            args = as_object(command.get("params"))
            if (payload.get("channel_id") == channel.id and command.get("type") == friend_check.COMMAND
                    and args.get("refresh") is True and args.get("id") == contact.contact_external_id):
                latest[contact.id] = job
    contacts = [contact for contact in contacts if contact.deleted_at is None or contact.id in latest]
    # 升级时从已有单项任务恢复；完成的放在队列前部，未完成的继续使用原任务。
    contacts.sort(key=lambda contact: contact.id not in latest or latest[contact.id].status in friend_check.ACTIVE_STATUSES)
    for position, contact in enumerate(contacts):
        snapshot = friend_check._contact_row(contact)
        job = latest.get(contact.id)
        result = None
        if job is not None and job.status not in friend_check.ACTIVE_STATUSES:
            result = {**friend_check._classify_result(job, contact.contact_external_id), "contact": snapshot, "_job_id": job.id}
            if contact.deleted_at is not None:
                result["removed"] = True
                result["message"] = "已删除微信好友"
            run.cursor += 1
        db.add(FriendCheckItem(
            tenant_id=channel.tenant_id, run_id=run.id, channel_id=channel.id,
            contact_id=contact.id, position=position, contact_json=_dump(snapshot),
            result_json=_dump(result) if result else None, job_id=job.id if job else None,
            completed_at=job.finished_at if result else None,
        ))
    run.total = len(contacts)
    run.initialized = True
    run.status = "completed" if run.total and run.cursor == run.total else "paused"
    if run.status == "completed":
        run.finished_at = now_shanghai()
    db.flush()


def _run(db: Session, channel: Channel) -> FriendCheckRun:
    friend_check._validate_channel(channel)
    run = db.scalar(select(FriendCheckRun).where(
        FriendCheckRun.tenant_id == channel.tenant_id, FriendCheckRun.channel_id == channel.id,
    ))
    if run is None:
        run = FriendCheckRun(tenant_id=channel.tenant_id, channel_id=channel.id)
        db.add(run)
        try:
            db.flush()
        except IntegrityError:
            db.rollback()
            run = db.scalar(select(FriendCheckRun).where(
                FriendCheckRun.tenant_id == channel.tenant_id, FriendCheckRun.channel_id == channel.id,
            ))
            if run is None:
                raise
    if not run.initialized:
        _lock(db, run)
        _seed(db, channel, run, history=True)
    return run


def _progress(run: FriendCheckRun) -> dict[str, Any]:
    return {"run_id": run.id, "generation": run.generation, "cursor": run.cursor,
            "total": run.total, "elapsed_ms": run.elapsed_ms}


def _action(job: TaskJob) -> str:
    payload = parse_job_payload(job)
    if job.related_type == friend_check.RELATED_TYPE:
        return "scan" if payload.get("friend_check_scan", True) else "recheck"
    command = as_object(payload.get("command")).get("type")
    return next((name for name, value in friend_actions.COMMANDS.items() if value == command), "")


def _record(db: Session, channel: Channel, run: FriendCheckRun, item: FriendCheckItem, job: TaskJob) -> bool:
    saved = _object(item.result_json)
    if saved.get("_job_id") == job.id:
        return False
    payload = parse_job_payload(job)
    if payload.get("friend_check_generation", run.generation) != run.generation:
        return False
    if payload.get("channel_id") != channel.id or job.tenant_id != channel.tenant_id:
        return False
    _lock(db, run)
    if friend_check._expire_if_stale(db, job):
        db.flush()
    if job.status in friend_check.ACTIVE_STATUSES:
        return False
    snapshot = _object(item.contact_json)
    action = _action(job)
    if action in {"scan", "recheck"}:
        classified = friend_check._classify_result(job, str(snapshot.get("external_id") or ""))
        if saved and action == "recheck":
            saved.update(recheckCategory=classified["category"], pay_name_last_char=classified["pay_name_last_char"], reason=classified["reason"], message="")
        else:
            saved = {**classified, "contact": snapshot}
        if item.result_json is None:
            run.cursor += 1
            if job.finished_at is not None:
                seconds = (ensure_shanghai_datetime(job.finished_at) - ensure_shanghai_datetime(job.created_at)).total_seconds()
                run.elapsed_ms += max(0, int(seconds * 1000))
                if run.cursor > 1:
                    run.elapsed_ms += int(run.interval_seconds * 1000)
    elif action in friend_actions.COMMANDS:
        response = friend_actions.poll_action(db, channel, job.id, commit=False)
        outcome = response["result"]
        saved["message"] = outcome["message"]
        if outcome["ok"]:
            saved["contact"] = outcome["contact"]
            item.contact_json = _dump(outcome["contact"])
            if action == "delete":
                saved["removed"] = True
    else:
        return False
    saved["_job_id"] = job.id
    item.result_json = _dump(saved)
    item.completed_at = job.finished_at or now_shanghai()
    if run.total and run.cursor == run.total:
        run.status = "completed"
        run.finished_at = run.finished_at or now_shanghai()
    db.flush()
    return True


def _snapshot(db: Session, run: FriendCheckRun, items: list[FriendCheckItem] | None = None) -> dict[str, Any]:
    rows = _items(db, run) if items is None else items
    results = [_object(item.result_json) for item in rows if item.result_json]
    pending_rows = [item for item in rows if item.job_id and _object(item.result_json).get("_job_id") != item.job_id]
    pending = None
    if pending_rows:
        item = pending_rows[0]
        job = db.get(TaskJob, item.job_id)
        if job is not None:
            pending = {"id": job.id, "action": _action(job), "contact_id": item.contact_id}
    return {**_progress(run), "revision": run.revision, "status": run.status,
            "interval_seconds": run.interval_seconds,
            "contacts": [_object(item.contact_json) for item in rows] if rows else None,
            "results": results, "pending": pending}


def get_state(db: Session, channel: Channel) -> dict[str, Any]:
    run = _run(db, channel)
    rows = _items(db, run)
    pending = {item.job_id: item for item in rows if item.job_id and _object(item.result_json).get("_job_id") != item.job_id}
    if pending:
        for job in db.scalars(select(TaskJob).where(
            TaskJob.tenant_id == channel.tenant_id, TaskJob.id.in_(pending), TaskJob.job_type == JOB_TYPE,
        )).all():
            _record(db, channel, run, pending[job.id], job)
    snapshot = _snapshot(db, run, rows)
    db.commit()
    return snapshot


def start_run(db: Session, channel: Channel, *, generation: str, interval_seconds: float = 1) -> dict[str, Any]:
    run = _run(db, channel)
    _generation(run, generation)
    if isinstance(interval_seconds, bool) or not isinstance(interval_seconds, (int, float)) or not math.isfinite(interval_seconds) or not 0 <= interval_seconds <= 60:
        raise FriendCheckError(400, "检测间隔必须为 0 至 60 秒")
    _lock(db, run)
    if not run.total:
        _seed(db, channel, run, history=False)
    run.interval_seconds = interval_seconds
    if run.cursor < run.total:
        run.status = "running"
        run.started_at = run.started_at or now_shanghai()
    snapshot = _snapshot(db, run)
    db.commit()
    return snapshot


def pause_run(db: Session, channel: Channel, *, generation: str) -> dict[str, Any]:
    run = _run(db, channel)
    _generation(run, generation)
    _lock(db, run)
    if run.status != "completed":
        run.status = "paused"
    snapshot = _snapshot(db, run)
    db.commit()
    return snapshot


def clear_run(db: Session, channel: Channel, *, generation: str, confirmed: bool) -> dict[str, Any]:
    if confirmed is not True:
        raise FriendCheckError(400, "请先确认清空检测记录")
    run = _run(db, channel)
    _generation(run, generation)
    _lock(db, run)
    db.execute(delete(FriendCheckItem).where(
        FriendCheckItem.tenant_id == channel.tenant_id, FriendCheckItem.run_id == run.id,
    ), execution_options={"synchronize_session": False})
    run.generation = uuid.uuid4().hex
    run.status = "paused"
    run.cursor = run.total = run.elapsed_ms = 0
    run.started_at = run.finished_at = None
    run.initialized = True
    snapshot = _snapshot(db, run, [])
    db.commit()
    return snapshot


def _item(db: Session, run: FriendCheckRun, contact_id: int) -> FriendCheckItem:
    item = db.scalar(select(FriendCheckItem).where(
        FriendCheckItem.run_id == run.id, FriendCheckItem.tenant_id == run.tenant_id,
        FriendCheckItem.contact_id == contact_id,
    ))
    if item is None:
        raise FriendCheckError(404, "未找到该账号的好友检测记录")
    return item


def _link(db: Session, run: FriendCheckRun, item: FriendCheckItem, job: TaskJob, *, scan: bool) -> dict[str, Any]:
    payload = parse_job_payload(job)
    payload.update(friend_check_run_id=run.id, friend_check_generation=run.generation, friend_check_scan=scan)
    job.payload_json = _dump(payload)
    item.job_id = job.id
    db.flush()
    response = {"job_id": job.id, "progress": _progress(run)}
    channel_id = run.channel_id
    db.commit()
    wakeup.notify(channel_id)
    return response


def start_item(db: Session, channel: Channel, contact_id: int, *, generation: str, scan: bool = True) -> dict[str, Any]:
    run = _run(db, channel)
    _generation(run, generation)
    _lock(db, run)
    item = _item(db, run, contact_id)
    if not scan and not item.result_json:
        raise FriendCheckError(409, "请先完成这位好友的检测，再重新检查")
    if item.job_id and _object(item.result_json).get("_job_id") != item.job_id:
        job = db.get(TaskJob, item.job_id)
        if job is not None:
            if _action(job) != ("scan" if scan else "recheck"):
                raise FriendCheckError(409, "该好友已有其他操作在进行，请先查询原任务结果")
            response = {"job_id": job.id, "progress": _progress(run)}
            db.commit()
            return response
    if scan:
        next_item = db.scalar(select(FriendCheckItem).where(
            FriendCheckItem.run_id == run.id, FriendCheckItem.tenant_id == run.tenant_id,
            FriendCheckItem.result_json.is_(None),
        ).order_by(FriendCheckItem.position).limit(1))
        if next_item is None or next_item.contact_id != contact_id:
            raise FriendCheckError(409, "检测进度已经变化，请重新读取后继续")
        run.status = "running"
    job = friend_check.start_check(db, channel, contact_id, commit=False)
    return _link(db, run, item, job, scan=scan)


def start_action(db: Session, channel: Channel, contact_id: int, *, generation: str, action: str, suffix: str = "- 单删") -> dict[str, Any]:
    run = _run(db, channel)
    _generation(run, generation)
    _lock(db, run)
    item = _item(db, run, contact_id)
    if not item.result_json:
        raise FriendCheckError(409, "请先完成这位好友的检测")
    if item.job_id and _object(item.result_json).get("_job_id") != item.job_id:
        raise FriendCheckError(409, "该好友已有操作在进行，请先查询原任务结果")
    job = friend_actions.start_action(db, channel, contact_id, action, suffix, commit=False)
    return _link(db, run, item, job, scan=False)


def record_completed_job(db: Session, channel: Channel, job: TaskJob) -> None:
    """手机回报后直接保存结果；浏览器关闭也不会丢，旧代次回报只保留任务记录。"""

    payload = parse_job_payload(job)
    run_id = payload.get("friend_check_run_id")
    if not run_id:
        return
    run = db.scalar(select(FriendCheckRun).where(
        FriendCheckRun.id == run_id, FriendCheckRun.tenant_id == channel.tenant_id,
        FriendCheckRun.channel_id == channel.id,
    ))
    if run is None or run.generation != payload.get("friend_check_generation"):
        return
    item = db.scalar(select(FriendCheckItem).where(
        FriendCheckItem.run_id == run.id, FriendCheckItem.tenant_id == channel.tenant_id,
        FriendCheckItem.job_id == job.id,
    ))
    if item is not None:
        _record(db, channel, run, item, job)
        db.commit()


def _poll(db: Session, channel: Channel, job_id: int, generation: str, *, action: bool) -> dict[str, Any]:
    run = _run(db, channel)
    _generation(run, generation)
    item = db.scalar(select(FriendCheckItem).where(
        FriendCheckItem.run_id == run.id, FriendCheckItem.tenant_id == channel.tenant_id,
        FriendCheckItem.job_id == job_id,
    ))
    # 代次正确也不能读取已清空、已被新任务替换的旧结果。先核对归属，再执行可能更新资料的轮询。
    if item is None:
        raise FriendCheckError(404, "未找到当前检测记录对应的任务")
    poll = friend_actions.poll_action if action else friend_check.poll_check
    response = poll(db, channel, job_id, commit=False)
    if response["state"] == "completed":
        _record(db, channel, run, item, db.get(TaskJob, job_id))
    response["progress"] = _progress(run)
    db.commit()
    return response


def poll_item(db: Session, channel: Channel, job_id: int, *, generation: str) -> dict[str, Any]:
    return _poll(db, channel, job_id, generation, action=False)


def poll_action(db: Session, channel: Channel, job_id: int, *, generation: str) -> dict[str, Any]:
    return _poll(db, channel, job_id, generation, action=True)
