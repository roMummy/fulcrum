"""项目统一的 Kitesurf 截图能力：插件直接调用，HTTP 接口复用同一流程。"""

from __future__ import annotations

import asyncio
import base64
import binascii
from dataclasses import dataclass
from io import BytesIO
import json
import math
import threading
import time
from typing import Any, Literal
from urllib.parse import urlsplit

from PIL import Image, UnidentifiedImageError
from pydantic import BaseModel, ConfigDict, Field, model_validator
from websockets.asyncio.client import connect as websocket_connect
from websockets.exceptions import WebSocketException

from app.core.logging import get_logger
from app.core.settings import settings
from app.core.tenant_context import require_tenant_id
from app.services.safe_fetch import UnsafeUrlError, ensure_public_http_url


logger = get_logger(__name__)
MAX_IMAGE_BYTES = 12 * 1024 * 1024
MAX_IMAGE_PIXELS = 16 * 1024 * 1024
MAX_IMAGE_HEIGHT = 8192
MAX_IMAGE_WIDTH = 2048
# CDP 用 Base64 返回图片，额度包含编码膨胀和少量协议字段，限制在解码前生效。
MAX_CDP_MESSAGE_BYTES = (MAX_IMAGE_BYTES * 4 // 3) + 64 * 1024
_slots = threading.BoundedSemaphore(settings.kitesurf_max_concurrency)


class ScreenshotRequest(BaseModel):
    """所有入口共用的截图参数；拒绝额外字段，避免绕过全局配置和租户边界。"""

    model_config = ConfigDict(extra="forbid", strict=True)

    url: str | None = Field(default=None, max_length=8192)
    html: str | None = Field(default=None, max_length=1024 * 1024)
    width: int = Field(default=1280, ge=320, le=MAX_IMAGE_WIDTH)
    height: int = Field(default=900, ge=200, le=4096)
    full_page: bool = True
    format: Literal["png", "jpeg"] = "png"
    quality: int = Field(default=85, ge=1, le=100)
    wait_ms: int = Field(default=0, ge=0, le=3000)
    timeout_seconds: float | None = Field(default=None, ge=1, le=120)

    @model_validator(mode="after")
    def validate_source(self) -> ScreenshotRequest:
        """URL 与 HTML 必须明确二选一，空白内容不能被当成空页成功截图。"""

        if (self.url is None) == (self.html is None):
            raise ValueError("url 与 html 必须且只能提供一个")
        source = self.url if self.url is not None else self.html
        if not str(source or "").strip():
            raise ValueError("截图内容不能为空")
        return self


class ScreenshotError(RuntimeError):
    """可安全返回给调用方的稳定错误，不携带引擎原始响应或页面内容。"""

    def __init__(self, code: str, message: str, status_code: int = 502):
        super().__init__(message)
        self.code = code
        self.message = message
        self.status_code = status_code


@dataclass(frozen=True)
class ScreenshotResult:
    """只返回图片及必要元数据，不共享浏览器状态或公开截图下载地址。"""

    content: bytes
    media_type: str
    width: int
    height: int
    elapsed_ms: int

    def as_image_reply(self) -> dict[str, str]:
        """交给已有标准图片回复与出站文件流程处理租户归属和渠道发送。"""

        # 延迟导入避免公共服务初始化时反向加载整个插件目录与运行时注册表。
        from app.services.plugin_runtime.replies import image_reply

        return image_reply(base64_data=base64.b64encode(self.content).decode("ascii"))


class _CdpClient:
    """单次调用独占的顺序 CDP 客户端，事件不能被误认成命令的执行结果。"""

    def __init__(self, websocket: Any):
        self.websocket = websocket
        self.sequence = 0

    async def call(self, method: str, params: dict | None = None, *, session_id: str = "") -> dict:
        self.sequence += 1
        sequence = self.sequence
        command: dict[str, Any] = {"id": sequence, "method": method, "params": params or {}}
        if session_id:
            command["sessionId"] = session_id
        await self.websocket.send(json.dumps(command, ensure_ascii=False, allow_nan=False))
        while True:
            frame = await self.websocket.recv()
            try:
                response = json.loads(frame)
            except (ValueError, TypeError) as exc:
                raise ScreenshotError("screenshot_protocol_error", "截图引擎返回了无效响应") from exc
            if not isinstance(response, dict):
                raise ScreenshotError("screenshot_protocol_error", "截图引擎返回了无效响应")
            if response.get("id") != sequence:
                # 页面加载期间会穿插 lifecycle、网络和 Target 事件，继续等当前编号。
                continue
            result = response.get("result")
            if response.get("error") or not isinstance(result, dict) or result.get("error"):
                # Kitesurf 个别命令会在 result 内报告错误并附透明占位图，必须先检查错误。
                raise ScreenshotError("screenshot_engine_error", "截图引擎执行失败")
            return result


def _engine_url() -> str:
    """全局 CDP 地址可位于内网，但不能夹带凭证、查询参数或片段。"""

    url = str(settings.kitesurf_ws_url or "").strip()
    if not url:
        raise ScreenshotError("screenshot_not_configured", "尚未配置 Kitesurf 截图服务", 503)
    try:
        parsed = urlsplit(url)
        valid = (
            parsed.scheme in {"ws", "wss"} and parsed.hostname and parsed.port != 0
            and parsed.username is None and parsed.password is None
            and not parsed.query and not parsed.fragment
        )
    except ValueError:
        valid = False
    if not valid:
        raise ScreenshotError("screenshot_config_invalid", "Kitesurf 服务配置不正确", 503)
    return url


def _required_identifier(result: dict, key: str) -> str:
    """没有明确会话编号时立即失败，不能落回其他调用的默认上下文。"""

    value = result.get(key)
    if not isinstance(value, str) or not value:
        raise ScreenshotError("screenshot_protocol_error", "截图引擎未返回独立会话")
    return value


def _full_page_size(metrics: dict, request: ScreenshotRequest) -> tuple[int, int]:
    """整页按实际布局计算范围，超限明确拒绝，不裁掉尾部后仍声称整页成功。"""

    size = metrics.get("cssContentSize")
    if not isinstance(size, dict):
        raise ScreenshotError("screenshot_protocol_error", "截图引擎未返回页面尺寸")
    dimensions = []
    for key, minimum in (("width", request.width), ("height", request.height)):
        value = size.get(key)
        if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value) or value <= 0:
            raise ScreenshotError("screenshot_protocol_error", "截图引擎返回的页面尺寸无效")
        dimensions.append(max(minimum, math.ceil(value)))
    width, height = dimensions
    if width > MAX_IMAGE_WIDTH or height > MAX_IMAGE_HEIGHT or width * height > MAX_IMAGE_PIXELS:
        raise ScreenshotError("screenshot_too_large", "页面过大，请使用视口截图或缩小内容范围", 413)
    return width, height


def _decode_image(data: object, *, format: str, width: int, height: int) -> bytes:
    """解码前限制大小，再验证格式、实际尺寸与完整性，拒绝错误占位图。"""

    if not isinstance(data, str) or not data or len(data) > MAX_IMAGE_BYTES * 4 // 3:
        raise ScreenshotError("screenshot_image_invalid", "截图引擎返回的图片为空或过大")
    try:
        content = base64.b64decode(data, validate=True)
        if not content or len(content) > MAX_IMAGE_BYTES:
            raise ValueError("invalid image size")
        with Image.open(BytesIO(content)) as image:
            if image.format != format.upper() or image.size != (width, height):
                raise ValueError("unexpected image format or dimensions")
            image.verify()
    except (ValueError, OSError, binascii.Error, UnidentifiedImageError, Image.DecompressionBombError) as exc:
        raise ScreenshotError("screenshot_image_invalid", "截图引擎返回的图片无效") from exc
    return content


async def _capture(url: str, request: ScreenshotRequest, timeout: float) -> tuple[bytes, int, int]:
    """每次独占连接、Cookie 上下文与页面，退出时回收资源，禁止跨租户复用页面。"""

    async with websocket_connect(
        url, open_timeout=timeout, close_timeout=1, max_size=MAX_CDP_MESSAGE_BYTES,
        max_queue=4, ping_interval=None, proxy=None,
    ) as websocket:
        cdp = _CdpClient(websocket)
        browser_context_id = ""
        try:
            browser_context_id = _required_identifier(await cdp.call("Target.createBrowserContext", {"disposeOnDetach": True}), "browserContextId")
            target_id = _required_identifier(await cdp.call("Target.createTarget", {"url": "about:blank", "browserContextId": browser_context_id}), "targetId")
            session_id = _required_identifier(await cdp.call("Target.attachToTarget", {"targetId": target_id, "flatten": True}), "sessionId")
            await cdp.call("Page.enable", session_id=session_id)
            await cdp.call("Emulation.setDeviceMetricsOverride", {"width": request.width, "height": request.height, "deviceScaleFactor": 1, "mobile": False}, session_id=session_id)
            if request.url is not None:
                navigation = await cdp.call("Page.navigate", {"url": request.url.strip()}, session_id=session_id)
                if navigation.get("errorText") or navigation.get("isDownload"):
                    raise ScreenshotError("screenshot_navigation_failed", "无法加载需要截图的页面")
            else:
                # 官方 Target 的页面 frameId 与 targetId 一致；读取 frameTree 避免依赖隐式约定。
                tree = await cdp.call("Page.getFrameTree", session_id=session_id)
                frame_tree = tree.get("frameTree")
                frame = frame_tree.get("frame") if isinstance(frame_tree, dict) else None
                if not isinstance(frame, dict):
                    raise ScreenshotError("screenshot_protocol_error", "截图引擎未返回页面框架")
                frame_id = _required_identifier(frame, "id")
                await cdp.call("Page.setDocumentContent", {"frameId": frame_id, "html": request.html}, session_id=session_id)
            if request.wait_ms:
                await asyncio.sleep(request.wait_ms / 1000)
            while True:
                # 先等字体、图片等关键资源，再读取整页布局，避免按资源到达前的高度截断长页。
                # 每次只查询当前独占 target；轮询本身仍受外层总截止时间约束。
                resources = await cdp.call("Kitesurf.hasPendingCriticalResources", {"targetId": target_id})
                if resources.get("pending") is False:
                    break
                if resources.get("pending") is not True:
                    raise ScreenshotError("screenshot_protocol_error", "截图引擎未返回资源加载状态")
                await asyncio.sleep(0.05)
            width, height = request.width, request.height
            if request.full_page:
                metrics = await cdp.call("Page.getLayoutMetrics", session_id=session_id)
                width, height = _full_page_size(metrics, request)
            options: dict[str, Any] = {
                "format": request.format, "captureBeyondViewport": request.full_page,
                "clip": {"x": 0, "y": 0, "width": width, "height": height, "scale": 1},
            }
            if request.format == "jpeg":
                options["quality"] = request.quality
            # Kitesurf captureScreenshot 本身等待当前截图区域的字体、图片等资源。
            captured = await cdp.call("Page.captureScreenshot", options, session_id=session_id)
            return _decode_image(captured.get("data"), format=request.format, width=width, height=height), width, height
        finally:
            if browser_context_id:
                try:
                    # 总超时触发后仅允许额外一秒做资源释放，连接随后也有关闭超时。
                    await asyncio.wait_for(cdp.call("Target.disposeBrowserContext", {"browserContextId": browser_context_id}), timeout=1)
                except Exception:
                    # 连接关闭同样会清理本次独占状态；不得让清理错误覆盖原始业务错误。
                    logger.debug("screenshot context cleanup interrupted")


def capture_screenshot(request: ScreenshotRequest) -> ScreenshotResult:
    """同步公共入口；异步站内调用应放在线程池，不阻塞 ASGI 事件循环。"""

    tenant_id = require_tenant_id()
    if not isinstance(request, ScreenshotRequest):
        raise TypeError("截图参数必须使用 ScreenshotRequest")
    started = time.monotonic()
    url = _engine_url()
    timeout = min(request.timeout_seconds or settings.kitesurf_timeout_seconds, settings.kitesurf_timeout_seconds)
    if not _slots.acquire(blocking=False):
        raise ScreenshotError("screenshot_busy", "截图服务繁忙，请稍后重试", 429)
    try:
        if request.url is not None:
            try:
                ensure_public_http_url(request.url)
            except (UnsafeUrlError, ValueError) as exc:
                raise ScreenshotError("screenshot_url_invalid", "截图地址必须是无凭证的公网 HTTP 或 HTTPS 地址", 400) from exc
        remaining = timeout - (time.monotonic() - started)
        if remaining <= 0:
            raise TimeoutError
        content, width, height = asyncio.run(asyncio.wait_for(_capture(url, request, remaining), timeout=remaining))
    except (asyncio.TimeoutError, TimeoutError) as exc:
        raise ScreenshotError("screenshot_timeout", "截图服务响应超时", 504) from exc
    except (WebSocketException, OSError) as exc:
        raise ScreenshotError("screenshot_unavailable", "无法连接 Kitesurf 截图服务", 502) from exc
    finally:
        _slots.release()
    elapsed_ms = round((time.monotonic() - started) * 1000)
    logger.info("screenshot completed", extra={"tenant_id": tenant_id, "width": width, "height": height, "elapsed_ms": elapsed_ms})
    return ScreenshotResult(content=content, media_type=f"image/{request.format}", width=width, height=height, elapsed_ms=elapsed_ms)
