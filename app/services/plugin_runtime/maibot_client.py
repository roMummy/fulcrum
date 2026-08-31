from __future__ import annotations

import asyncio
import json
import re
import ssl
import threading
import time
import uuid
from collections import OrderedDict
from dataclasses import dataclass, field
from typing import Any, Callable, Mapping, Protocol

from websockets.asyncio.client import connect as websocket_connect

from app.core.logging import get_logger


logger = get_logger(__name__)
CONVERSATION_TOKEN_PATTERN = re.compile(r"^mbs-conversation-(\d+)$")
MAX_WEBSOCKET_MESSAGE_BYTES = 2 * 1024 * 1024
MAX_RECENT_CONVERSATIONS = 1_000
RECENT_CONVERSATION_TTL_SECONDS = 24 * 60 * 60
START_TIMEOUT_SECONDS = 2.0
SUBMIT_TIMEOUT_SECONDS = 2.0


@dataclass(frozen=True)
class MaiBotConnectionKey:
    """使用租户与插件绑定共同定位一条独立 MaiBot 连接。"""

    tenant_id: int
    binding_id: int


@dataclass(frozen=True)
class MaiBotConnectionConfig:
    """保存一条绑定连接的有限传输配置，API Key 不进入对象字符串表示。"""

    ws_url: str
    api_key: str = field(repr=False)
    platform_name: str = "multi-bot-saas"
    verify_ssl: bool = True
    connect_timeout_seconds: int = 15
    receive_timeout_seconds: int = 120
    heartbeat_interval_seconds: int = 20
    reconnect_delay_seconds: int = 5
    queue_maxsize: int = 200
    max_send_retries: int = 2


MaiBotReplyHandler = Callable[[MaiBotConnectionKey, int, Mapping[str, Any]], None]


@dataclass(frozen=True)
class _QueuedSubmission:
    """保存待发送 APIMessageBase 和平台已验证的内部会话编号。"""

    api_message: Mapping[str, Any]
    conversation_id: int


class _ManagedConnection(Protocol):
    key: MaiBotConnectionKey
    config: MaiBotConnectionConfig

    def start(self) -> None:
        ...

    def submit(self, *, api_message: Mapping[str, Any], conversation_id: int) -> bool:
        ...

    def stop(self) -> None:
        ...


class MaiBotBindingConnection:
    """在独立线程中维护单个租户插件绑定的 MaiBot WebSocket。"""

    def __init__(
        self,
        *,
        key: MaiBotConnectionKey,
        config: MaiBotConnectionConfig,
        reply_handler: MaiBotReplyHandler,
        connector: Callable[..., Any] = websocket_connect,
    ):
        self.key = key
        self.config = config
        self.reply_handler = reply_handler
        self._connector = connector
        self._thread: threading.Thread | None = None
        self._thread_lock = threading.Lock()
        self._loop: asyncio.AbstractEventLoop | None = None
        self._queue: asyncio.Queue[_QueuedSubmission] | None = None
        self._stop_event: asyncio.Event | None = None
        self._connected_event: asyncio.Event | None = None
        self._websocket: Any = None
        self._loop_ready = threading.Event()
        self._stop_requested = threading.Event()
        self._recent_conversations: OrderedDict[int, float] = OrderedDict()

    def start(self) -> None:
        """幂等启动当前绑定自己的后台线程，不与其他绑定共享事件循环。"""

        with self._thread_lock:
            if self._thread is not None and self._thread.is_alive():
                return
            self._stop_requested.clear()
            self._loop_ready.clear()
            self._thread = threading.Thread(
                target=self._thread_main,
                name=f"maibot-{self.key.tenant_id}-{self.key.binding_id}",
                daemon=True,
            )
            self._thread.start()

    def submit(self, *, api_message: Mapping[str, Any], conversation_id: int) -> bool:
        """线程安全提交消息；队列未就绪或已满时立即返回失败。"""

        normalized_conversation_id = int(conversation_id)
        if normalized_conversation_id <= 0:
            return False
        self.start()
        if not self._loop_ready.wait(START_TIMEOUT_SECONDS):
            return False
        loop = self._loop
        if loop is None or loop.is_closed() or self._stop_requested.is_set():
            return False
        future = asyncio.run_coroutine_threadsafe(
            self._enqueue(
                _QueuedSubmission(
                    api_message=dict(api_message),
                    conversation_id=normalized_conversation_id,
                )
            ),
            loop,
        )
        try:
            return bool(future.result(timeout=SUBMIT_TIMEOUT_SECONDS))
        except Exception:
            future.cancel()
            return False

    def stop(self) -> None:
        """停止当前绑定连接并等待短时间释放线程、队列和 WebSocket。"""

        self._stop_requested.set()
        loop = self._loop
        if loop is not None and not loop.is_closed() and self._loop_ready.is_set():
            future = asyncio.run_coroutine_threadsafe(self._request_stop(), loop)
            try:
                future.result(timeout=SUBMIT_TIMEOUT_SECONDS)
            except Exception:
                future.cancel()
        thread = self._thread
        if thread is not None and thread.is_alive() and thread is not threading.current_thread():
            thread.join(timeout=5)

    def _thread_main(self) -> None:
        try:
            asyncio.run(self._run())
        except Exception:
            logger.exception(
                "maibot binding connection stopped unexpectedly",
                extra={"tenant_id": self.key.tenant_id, "binding_id": self.key.binding_id},
            )
        finally:
            self._loop = None
            self._queue = None
            self._stop_event = None
            self._connected_event = None
            self._websocket = None
            self._recent_conversations.clear()
            self._loop_ready.clear()

    async def _run(self) -> None:
        self._loop = asyncio.get_running_loop()
        self._queue = asyncio.Queue(maxsize=max(1, int(self.config.queue_maxsize)))
        self._stop_event = asyncio.Event()
        self._connected_event = asyncio.Event()
        self._loop_ready.set()
        if self._stop_requested.is_set():
            self._stop_event.set()

        connection_task = asyncio.create_task(self._connection_loop(), name="maibot-connection")
        sender_task = asyncio.create_task(self._sender_loop(), name="maibot-sender")
        try:
            await self._stop_event.wait()
        finally:
            connection_task.cancel()
            sender_task.cancel()
            await asyncio.gather(connection_task, sender_task, return_exceptions=True)
            await self._close_websocket()

    async def _enqueue(self, submission: _QueuedSubmission) -> bool:
        queue = self._queue
        if queue is None or queue.full() or self._stop_requested.is_set():
            return False
        queue.put_nowait(submission)
        return True

    async def _request_stop(self) -> None:
        stop_event = self._stop_event
        if stop_event is not None:
            stop_event.set()
        connected_event = self._connected_event
        if connected_event is not None:
            connected_event.clear()
        await self._close_websocket()

    async def _connection_loop(self) -> None:
        """持续建连和接收；单次断线只影响当前绑定，不影响其他租户连接。"""

        while not self._is_stopping():
            try:
                ssl_context = _build_ssl_context(self.config.ws_url, verify_ssl=self.config.verify_ssl)
                headers = {
                    "x-uuid": f"mbs-{self.key.tenant_id}-{self.key.binding_id}-{uuid.uuid4().hex}",
                    "x-apikey": self.config.api_key,
                    "x-platform": self.config.platform_name,
                }
                async with self._connector(
                    self.config.ws_url,
                    additional_headers=headers,
                    open_timeout=self.config.connect_timeout_seconds,
                    ping_interval=self.config.heartbeat_interval_seconds,
                    ping_timeout=self.config.connect_timeout_seconds,
                    close_timeout=5,
                    max_size=MAX_WEBSOCKET_MESSAGE_BYTES,
                    proxy=None,
                    ssl=ssl_context,
                ) as websocket:
                    self._websocket = websocket
                    if self._connected_event is not None:
                        self._connected_event.set()
                    logger.info(
                        "maibot binding connected",
                        extra={"tenant_id": self.key.tenant_id, "binding_id": self.key.binding_id},
                    )
                    await self._receive_loop(websocket)
            except asyncio.CancelledError:
                raise
            except Exception:
                if not self._is_stopping():
                    logger.warning(
                        "maibot binding connection lost",
                        extra={"tenant_id": self.key.tenant_id, "binding_id": self.key.binding_id},
                    )
            finally:
                if self._connected_event is not None:
                    self._connected_event.clear()
                self._websocket = None

            if not self._is_stopping():
                await self._wait_or_stop(self.config.reconnect_delay_seconds)

    async def _receive_loop(self, websocket: Any) -> None:
        while not self._is_stopping():
            raw_message = await asyncio.wait_for(
                websocket.recv(),
                timeout=max(30, int(self.config.receive_timeout_seconds)),
            )
            if isinstance(raw_message, bytes):
                if len(raw_message) > MAX_WEBSOCKET_MESSAGE_BYTES:
                    continue
                raw_text = raw_message.decode("utf-8", errors="replace")
            else:
                raw_text = str(raw_message or "")
            if not raw_text or len(raw_text.encode("utf-8")) > MAX_WEBSOCKET_MESSAGE_BYTES:
                continue
            try:
                package = json.loads(raw_text)
            except json.JSONDecodeError:
                continue
            if not isinstance(package, Mapping) or str(package.get("type") or "") != "sys_std":
                continue
            conversation_id = _extract_conversation_id(package)
            if conversation_id is None or not self._conversation_is_recent(conversation_id):
                # MaiBot 只能回复当前连接近期实际发送过的内部会话。回包里的任意
                # 原始群号、用户号或其他绑定会话编号都不会进入平台出站队列。
                continue
            await asyncio.to_thread(self.reply_handler, self.key, conversation_id, dict(package))

    async def _sender_loop(self) -> None:
        while not self._is_stopping():
            queue = self._queue
            if queue is None:
                await self._wait_or_stop(0.1)
                continue
            submission = await queue.get()
            try:
                await self._send_submission(submission)
            finally:
                queue.task_done()

    async def _send_submission(self, submission: _QueuedSubmission) -> None:
        attempts = max(1, int(self.config.max_send_retries))
        for attempt in range(attempts):
            if self._is_stopping():
                return
            connected_event = self._connected_event
            if connected_event is None:
                return
            await connected_event.wait()
            websocket = self._websocket
            if websocket is None:
                continue
            package = {
                "ver": 1,
                "msg_id": f"mbs-{uuid.uuid4().hex}",
                "type": "sys_std",
                "meta": {
                    "sender_user": self.config.api_key,
                    "platform": self.config.platform_name,
                    "timestamp": time.time(),
                },
                "payload": dict(submission.api_message),
            }
            self._remember_conversation(submission.conversation_id)
            try:
                await websocket.send(json.dumps(package, ensure_ascii=False, separators=(",", ":")))
                return
            except asyncio.CancelledError:
                raise
            except Exception:
                if self._connected_event is not None:
                    self._connected_event.clear()
                await self._close_websocket()
                if attempt + 1 < attempts:
                    await self._wait_or_stop(self.config.reconnect_delay_seconds)

    def _remember_conversation(self, conversation_id: int) -> None:
        current_time = time.monotonic()
        self._prune_expired_conversations(current_time)
        self._recent_conversations[int(conversation_id)] = current_time
        self._recent_conversations.move_to_end(int(conversation_id))
        while len(self._recent_conversations) > MAX_RECENT_CONVERSATIONS:
            self._recent_conversations.popitem(last=False)

    def _conversation_is_recent(self, conversation_id: int) -> bool:
        current_time = time.monotonic()
        self._prune_expired_conversations(current_time)
        return int(conversation_id) in self._recent_conversations

    def _prune_expired_conversations(self, current_time: float) -> None:
        """按时间清理旧会话授权，数量上限之外再限制最长回复窗口。"""

        cutoff = current_time - RECENT_CONVERSATION_TTL_SECONDS
        while self._recent_conversations:
            first_conversation_id, submitted_at = next(iter(self._recent_conversations.items()))
            if submitted_at >= cutoff:
                break
            self._recent_conversations.pop(first_conversation_id, None)

    async def _close_websocket(self) -> None:
        websocket = self._websocket
        self._websocket = None
        if websocket is None:
            return
        try:
            await websocket.close()
        except Exception:
            return

    async def _wait_or_stop(self, seconds: float) -> None:
        stop_event = self._stop_event
        if stop_event is None:
            await asyncio.sleep(max(0.0, seconds))
            return
        try:
            await asyncio.wait_for(stop_event.wait(), timeout=max(0.01, float(seconds)))
        except asyncio.TimeoutError:
            return

    def _is_stopping(self) -> bool:
        return self._stop_requested.is_set() or bool(self._stop_event and self._stop_event.is_set())


class MaiBotClientRegistry:
    """管理多条按租户绑定隔离的连接，不保存跨绑定共享 WebSocket。"""

    def __init__(
        self,
        *,
        connection_factory: Callable[..., _ManagedConnection] = MaiBotBindingConnection,
    ):
        self._connection_factory = connection_factory
        self._connections: dict[MaiBotConnectionKey, _ManagedConnection] = {}
        self._lock = threading.RLock()

    def submit(
        self,
        *,
        key: MaiBotConnectionKey,
        config: MaiBotConnectionConfig,
        api_message: Mapping[str, Any],
        conversation_id: int,
        reply_handler: MaiBotReplyHandler,
    ) -> bool:
        connection = self._get_or_replace(key=key, config=config, reply_handler=reply_handler)
        return connection.submit(api_message=api_message, conversation_id=conversation_id)

    def stop(self, key: MaiBotConnectionKey) -> None:
        with self._lock:
            connection = self._connections.pop(key, None)
        if connection is not None:
            connection.stop()

    def stop_all(self) -> None:
        with self._lock:
            connections = list(self._connections.values())
            self._connections.clear()
        for connection in connections:
            connection.stop()

    def _get_or_replace(
        self,
        *,
        key: MaiBotConnectionKey,
        config: MaiBotConnectionConfig,
        reply_handler: MaiBotReplyHandler,
    ) -> _ManagedConnection:
        old_connection: _ManagedConnection | None = None
        with self._lock:
            connection = self._connections.get(key)
            if connection is not None and connection.config == config:
                return connection
            if connection is not None:
                old_connection = self._connections.pop(key)
            new_connection = self._connection_factory(
                key=key,
                config=config,
                reply_handler=reply_handler,
            )
            self._connections[key] = new_connection
        if old_connection is not None:
            old_connection.stop()
        new_connection.start()
        return new_connection


def _extract_conversation_id(package: Mapping[str, Any]) -> int | None:
    payload = package.get("payload")
    if not isinstance(payload, Mapping):
        return None
    message_info = payload.get("message_info")
    if not isinstance(message_info, Mapping):
        return None
    group_info = message_info.get("group_info")
    user_info = message_info.get("user_info")
    token = ""
    if isinstance(group_info, Mapping):
        token = str(group_info.get("group_id") or "").strip()
    if not token and isinstance(user_info, Mapping):
        token = str(user_info.get("user_id") or "").strip()
    match = CONVERSATION_TOKEN_PATTERN.fullmatch(token)
    if not match:
        return None
    conversation_id = int(match.group(1))
    return conversation_id if conversation_id > 0 else None


def _build_ssl_context(ws_url: str, *, verify_ssl: bool) -> ssl.SSLContext | None:
    if not str(ws_url or "").strip().lower().startswith("wss://"):
        return None
    if verify_ssl:
        return ssl.create_default_context()
    return ssl._create_unverified_context()  # noqa: SLF001


maibot_client_registry = MaiBotClientRegistry()


def stop_maibot_connection(tenant_id: int, binding_id: int) -> None:
    """停止一个租户绑定的连接，供配置保存和绑定停用流程调用。"""

    maibot_client_registry.stop(
        MaiBotConnectionKey(
            tenant_id=int(tenant_id),
            binding_id=int(binding_id),
        )
    )


def stop_all_maibot_connections() -> None:
    """应用关闭时停止全部独立连接并释放后台线程。"""

    maibot_client_registry.stop_all()
