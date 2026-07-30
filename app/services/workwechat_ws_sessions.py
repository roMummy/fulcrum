from __future__ import annotations

import json
import asyncio
import threading
from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True)
class RegisteredWebSocket:
    websocket: Any
    loop: Any | None = None


class WorkWechatWebSocketRegistry:
    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._connections: dict[int, list[RegisteredWebSocket]] = {}

    def register(self, channel_id: int, websocket: Any, loop: Any | None = None) -> None:
        # 当前实现是单进程内存注册表；多 worker 部署时需要升级为 Redis/pubsub 广播。
        registered = RegisteredWebSocket(websocket=websocket, loop=loop)
        with self._lock:
            connections = self._connections.setdefault(int(channel_id), [])
            if registered not in connections:
                connections.append(registered)

    def unregister(self, channel_id: int, websocket: Any) -> None:
        with self._lock:
            connections = self._connections.get(int(channel_id))
            if not connections:
                return
            connections[:] = [item for item in connections if item.websocket is not websocket]
            if not connections:
                self._connections.pop(int(channel_id), None)

    def notify_job_available(self, *, channel_id: int, job_id: int) -> dict[str, int]:
        # WS 只做实时唤醒；真正领取任务仍通过 HTTP poll，便于处理锁定、重试和断线恢复。
        payload = json.dumps({"event_type": "job_available", "job_id": int(job_id)}, ensure_ascii=False)
        sent = 0
        failed = 0
        with self._lock:
            connections = list(self._connections.get(int(channel_id), set()))
        for registered in connections:
            websocket = registered.websocket
            try:
                send_text = getattr(websocket, "send_text", None)
                if not callable(send_text):
                    failed += 1
                    continue
                result = send_text(payload)
                if hasattr(result, "__await__"):
                    if registered.loop is None:
                        failed += 1
                        continue
                    asyncio.run_coroutine_threadsafe(result, registered.loop)
                    sent += 1
                else:
                    sent += 1
            except Exception:
                failed += 1
        return {"sent": sent, "failed": failed}


workwechat_ws_registry = WorkWechatWebSocketRegistry()
