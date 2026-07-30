from __future__ import annotations

import json
import time
from pathlib import Path
from typing import Any


PROCESSED_LIMIT = 5000


def try_acquire_job_lock(
    state_dir: Path,
    *,
    job_id: int,
    payload_hash: str,
    event_id: str = "",
    stale_seconds: int = 3600,
) -> bool:
    # 用原子创建 lock 文件实现跨线程/跨进程互斥，避免 WS 重放时同一任务被发送多次。
    locks_dir = _locks_dir(state_dir)
    locks_dir.mkdir(parents=True, exist_ok=True)
    lock_path = _lock_path(state_dir, job_id)
    if _is_stale_lock(lock_path, stale_seconds=stale_seconds):
        lock_path.unlink(missing_ok=True)
    payload = {
        "job_id": int(job_id),
        "payload_hash": str(payload_hash or ""),
        "event_id": str(event_id or ""),
        "started_at": time.time(),
    }
    try:
        with lock_path.open("x", encoding="utf-8") as file:
            json.dump(payload, file, ensure_ascii=False, sort_keys=True)
    except FileExistsError:
        return False
    return True


def release_job_lock(state_dir: Path, *, job_id: int) -> None:
    # complete/fail 回传后释放锁；已完成任务仍由 processed.json 防重放。
    _lock_path(state_dir, job_id).unlink(missing_ok=True)


def mark_job_processed(state_dir: Path, *, job_id: int, payload_hash: str, result: dict[str, Any] | None = None) -> None:
    # 记录已完成任务，后续同 job_id + payload_hash 的 WS 重放只补 ack，不再触发 Hook 发送。
    processed = _load_processed(state_dir)
    key = str(int(job_id))
    processed[key] = {
        "job_id": int(job_id),
        "payload_hash": str(payload_hash or ""),
        "completed_at": time.time(),
        "result": result or {},
    }
    _store_processed(state_dir, _trim_processed(processed))
    release_job_lock(state_dir, job_id=job_id)


def is_job_processed(state_dir: Path, *, job_id: int, payload_hash: str) -> bool:
    item = _load_processed(state_dir).get(str(int(job_id)))
    return isinstance(item, dict) and str(item.get("payload_hash") or "") == str(payload_hash or "")


def has_processed_payload_conflict(state_dir: Path, *, job_id: int, payload_hash: str) -> bool:
    # 同一个 job_id 如果出现不同 payload_hash，说明云端或本地状态异常，客户端不能擅自发送。
    item = _load_processed(state_dir).get(str(int(job_id)))
    return isinstance(item, dict) and str(item.get("payload_hash") or "") != str(payload_hash or "")


def _locks_dir(state_dir: Path) -> Path:
    return Path(state_dir) / "locks"


def _lock_path(state_dir: Path, job_id: int) -> Path:
    return _locks_dir(state_dir) / f"job-{int(job_id)}.lock"


def _processed_path(state_dir: Path) -> Path:
    return Path(state_dir) / "processed.json"


def _is_stale_lock(lock_path: Path, *, stale_seconds: int) -> bool:
    if not lock_path.exists():
        return False
    try:
        payload = json.loads(lock_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return True
    started_at = float(payload.get("started_at") or 0)
    return bool(started_at and time.time() - started_at > max(1, stale_seconds))


def _load_processed(state_dir: Path) -> dict[str, Any]:
    path = _processed_path(state_dir)
    if not path.exists():
        return {}
    try:
        parsed = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    return parsed if isinstance(parsed, dict) else {}


def _store_processed(state_dir: Path, payload: dict[str, Any]) -> None:
    path = _processed_path(state_dir)
    path.parent.mkdir(parents=True, exist_ok=True)
    temp_path = path.with_suffix(".tmp")
    temp_path.write_text(json.dumps(payload, ensure_ascii=False, sort_keys=True, indent=2), encoding="utf-8")
    temp_path.replace(path)


def _trim_processed(processed: dict[str, Any]) -> dict[str, Any]:
    if len(processed) <= PROCESSED_LIMIT:
        return processed
    items = sorted(
        processed.items(),
        key=lambda item: float(item[1].get("completed_at") or 0) if isinstance(item[1], dict) else 0,
        reverse=True,
    )
    return dict(items[:PROCESSED_LIMIT])
