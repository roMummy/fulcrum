"""WeBridge 8080 WebSocket：手机主动连接，事件和命令结果均走文本帧。"""

from __future__ import annotations

import asyncio
import json
import time
from contextlib import suppress

from fastapi import WebSocket, WebSocketDisconnect
from fastapi.concurrency import run_in_threadpool
from sqlalchemy import select

from app.core.db import TenantSessionLocal
from app.core.logging import get_logger
from app.core.tenant_context import tenant_scope
from app.core.tenant_db import tenant_session
from app.models.entities import Channel
from app.services.account_connections import verify_account_connection
from app.services.account_connections.contracts import AccountConnectionError
from app.services.channel_events import ingest_inbound_event

from . import service, wakeup
from .access import verify_connection_token

logger = get_logger(__name__)
HELLO_TIMEOUT_SECONDS = 10
COMMAND_CHECK_SECONDS = 1
PING_INTERVAL_SECONDS = 25
PONG_TIMEOUT_SECONDS = 60
MAX_FRAME_BYTES = 10 * 1024 * 1024
_connections: dict[int, WebSocket] = {}


def _process(tenant_id: int, channel_id: int, token: str, frame: dict, *, claim: bool = False):
    # 每次操作都重新读取渠道和凭据，删除账号或轮换凭据后旧连接不能继续领任务。
    with tenant_scope(tenant_id), tenant_session(TenantSessionLocal) as db:
        channel = db.scalar(select(Channel).where(Channel.id == channel_id).with_for_update())
        if channel is None or channel.deleted_at is not None or channel.channel_type != "wechat_webridge" or not verify_connection_token(channel, token):
            raise ValueError("invalid_connection_address")
        if claim:
            return service.claim_commands(db, channel, frame)
        kind = frame.get("type")
        if kind == "hello":
            service.register_device(db, channel, frame)
            if not channel.is_enabled:
                try:
                    verify_account_connection(db, channel_id=channel_id)
                except AccountConnectionError:
                    # 注册已经保存；与 HTTP 注册一样允许手机继续收发诊断命令。
                    logger.warning("webridge websocket account verification pending", extra={"channel_id": channel_id})
            return
        if not service.identity_matches(channel, frame):
            raise ValueError("webridge_identity_mismatch")
        if kind == "command_result":
            try:
                service.accept_command_result(db, channel, frame)
            except ValueError as exc:
                # 重连可能补发已清理的历史结果；不因此丢掉同连接的后续帧。
                if str(exc) != "webridge_request_not_found":
                    raise
        elif kind == "event" and channel.is_enabled:
            frame.setdefault("event_id", service.mark_event_seen(channel, frame))
            ingest_inbound_event(db, channel, frame, headers={}, source="wechat_webridge")
            db.commit()


async def serve(websocket: WebSocket, *, channel_id: int, tenant_id: int, connection_token: str) -> None:
    await websocket.accept()
    tasks: list[asyncio.Task] = []
    try:
        # 插件 Open 后立即补发，不以 hello_ack 作为接收事件/结果的门槛。
        buffered: list[dict] = []
        deadline = time.monotonic() + HELLO_TIMEOUT_SECONDS
        while True:
            frame = await asyncio.wait_for(websocket.receive_json(), max(0, deadline - time.monotonic()))
            if not isinstance(frame, dict):
                continue
            if frame.get("type") == "hello":
                hello = frame
                break
            if frame.get("type") in {"event", "command_result"}:
                if len(buffered) >= 32:
                    raise ValueError("too_many_frames_before_hello")
                buffered.append(frame)
        await run_in_threadpool(_process, tenant_id, channel_id, connection_token, hello)
        previous = _connections.get(channel_id)
        _connections[channel_id] = websocket
        if previous is not None and previous is not websocket:
            with suppress(Exception):
                await previous.close(code=1000, reason="replaced")
        await websocket.send_json({"type": "hello_ack"})
        for frame in buffered:
            await run_in_threadpool(_process, tenant_id, channel_id, connection_token, frame)

        last_pong = [time.monotonic()]
        processing = asyncio.Lock()

        async def process(frame, *, claim=False):
            # 同一连接收包和发包共用运行快照，避免 SQLite 上并行写入丢失资料更新。
            async with processing:
                return await run_in_threadpool(_process, tenant_id, channel_id, connection_token, frame, claim=claim)

        async def receive():
            while True:
                frame = await websocket.receive_json()
                if not isinstance(frame, dict):
                    continue
                if frame.get("type") == "pong":
                    last_pong[0] = time.monotonic()
                elif frame.get("type") in {"event", "command_result"}:
                    # 旧会话补发可保留原 session_id；设备和微信账号仍须匹配。
                    await process(frame)

        async def send():
            wake = asyncio.Event()
            loop = asyncio.get_running_loop()
            wakeup.register(channel_id, loop, wake)
            next_ping = 0.0
            try:
                while _connections.get(channel_id) is websocket:
                    wake.clear()
                    now = time.monotonic()
                    if now - last_pong[0] > PONG_TIMEOUT_SECONDS:
                        raise ValueError("pong_timeout")
                    if now >= next_ping:
                        await websocket.send_json({"type": "ping", "ts_ms": int(time.time() * 1000)})
                        next_ping = now + PING_INTERVAL_SECONDS
                    commands = await process(hello, claim=True)
                    for command in commands:
                        frame = {"type": "command", "request_id": command["request_id"], "command_name": command["command_name"], "args": command["args"]}
                        encoded = json.dumps(frame, ensure_ascii=False, separators=(",", ":"))
                        if len(encoded.encode("utf-8")) > MAX_FRAME_BYTES:
                            # 不能拆帧或改成 Base64；让任务明确失败且不重发。
                            await process({**hello, "type": "command_result", "request_id": frame["request_id"], "ok": False, "error": "webridge_ws_frame_exceeds_10_mib", "result": {}})
                            continue
                        await websocket.send_text(encoded)
                    with suppress(asyncio.TimeoutError):
                        await asyncio.wait_for(wake.wait(), COMMAND_CHECK_SECONDS)
            finally:
                wakeup.unregister(channel_id, loop, wake)

        tasks = [asyncio.create_task(receive()), asyncio.create_task(send())]
        done, _ = await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
        for task in done:
            task.result()
    except WebSocketDisconnect:
        pass
    except (ValueError, asyncio.TimeoutError):
        with suppress(RuntimeError, OSError):
            await websocket.close(code=1008, reason="invalid_frame_or_connection")
    except (RuntimeError, OSError):
        pass
    except Exception:
        logger.exception("webridge websocket failed", extra={"channel_id": channel_id})
        with suppress(RuntimeError, OSError):
            await websocket.close(code=1011, reason="processing_failed")
    finally:
        if _connections.get(channel_id) is websocket:
            _connections.pop(channel_id, None)
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        # 已领取的命令保持 running，等待结果或现有超时处理，避免断线自动重发副作用。
