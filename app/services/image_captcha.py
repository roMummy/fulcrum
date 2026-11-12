"""公开注册使用的短时签名 PNG 图形验证码。"""

from __future__ import annotations

import base64
import binascii
import hashlib
import hmac
import json
import secrets
import struct
from collections import OrderedDict
import time
from threading import Lock
import zlib
from dataclasses import dataclass

from app.core.settings import settings


# 5x7 点阵只保存数字形状，生成的 PNG 不含可直接从 HTML 源码读取的验证码文本节点。
DIGIT_BITMAPS = {
    "0": ("11111", "10001", "10011", "10101", "11001", "10001", "11111"),
    "1": ("00100", "01100", "00100", "00100", "00100", "00100", "01110"),
    "2": ("11110", "00001", "00001", "11110", "10000", "10000", "11111"),
    "3": ("11110", "00001", "00001", "01110", "00001", "00001", "11110"),
    "4": ("10010", "10010", "10010", "11111", "00010", "00010", "00010"),
    "5": ("11111", "10000", "10000", "11110", "00001", "00001", "11110"),
    "6": ("01111", "10000", "10000", "11110", "10001", "10001", "01110"),
    "7": ("11111", "00001", "00010", "00100", "01000", "01000", "01000"),
    "8": ("01110", "10001", "10001", "01110", "10001", "10001", "01110"),
    "9": ("01110", "10001", "10001", "01111", "00001", "00001", "11110"),
}

CAPTCHA_PURPOSE_REGISTRATION = "registration"
CAPTCHA_PURPOSE_TENANT_LOGIN = "tenant_login"
CAPTCHA_PURPOSE_PLATFORM_LOGIN = "platform_login"
CAPTCHA_PURPOSES = frozenset(
    {
        CAPTCHA_PURPOSE_REGISTRATION,
        CAPTCHA_PURPOSE_TENANT_LOGIN,
        CAPTCHA_PURPOSE_PLATFORM_LOGIN,
    }
)


@dataclass(frozen=True)
class CaptchaVerification:
    """验证码消费结果；浏览器只看到统一失败文案，日志可使用内部原因定位问题。"""

    valid: bool
    reason: str


@dataclass(frozen=True)
class _StoredChallenge:
    """有界缓存中的最小状态，不保存图片、答案或令牌原文。"""

    purpose: str
    expires_at: int


# 验证码的“只能使用一次”需要短时状态，但攻击请求不能借此制造数据库记录。
# OrderedDict 负责按最早签发顺序淘汰，Lock 保证同一进程内并发请求不会重复消费同一令牌。
_CHALLENGE_CACHE: OrderedDict[str, _StoredChallenge] = OrderedDict()
_CACHE_LOCK = Lock()


@dataclass(frozen=True)
class CaptchaChallenge:
    """可交给注册模板的验证码令牌和内嵌 PNG，不暴露正确答案。"""

    token: str
    image_data_uri: str
    expires_at: int
    purpose: str


def _normalize_purpose(purpose: str) -> str:
    """校验验证码用途，阻止一个入口签发的挑战被另一个入口接受。"""

    normalized = (purpose or "").strip().lower()
    if normalized not in CAPTCHA_PURPOSES:
        raise ValueError("captcha_purpose_invalid")
    return normalized


def _sign(purpose: str, value: str) -> str:
    """使用应用密钥和用途给验证码载荷签名，阻止跨入口伪造令牌。"""

    return hmac.new(
        settings.secret_key.encode("utf-8"),
        f"captcha:{purpose}:{value}".encode("utf-8"),
        hashlib.sha256,
    ).hexdigest()


def _answer_digest(purpose: str, nonce: str, answer: str) -> str:
    """把答案绑定到随机 nonce，令牌中只保留不可逆摘要。"""

    return hmac.new(
        settings.secret_key.encode("utf-8"),
        f"captcha-answer:{purpose}:{nonce}:{answer}".encode("utf-8"),
        hashlib.sha256,
    ).hexdigest()


def _token_cache_key(purpose: str, token: str) -> str:
    """只用令牌摘要作为缓存键，避免短时内存状态意外持有浏览器凭据原文。"""

    return f"{purpose}:{hashlib.sha256(token.encode('utf-8')).hexdigest()}"


def _cache_capacity() -> int:
    """把部署配置收敛到有限范围，避免错误环境变量令缓存失去上限。"""

    return max(1, min(int(settings.captcha_cache_max_entries or 1), 100_000))


def _cache_ttl(ttl_seconds: int | None) -> int:
    """把验证码有效期限制在 60 到 600 秒之间。"""

    configured = settings.captcha_ttl_seconds if ttl_seconds is None else ttl_seconds
    return max(60, min(int(configured), 600))


def _prune_cache_locked(now: int) -> None:
    """调用方必须持有锁；先删除过期挑战，再淘汰超出容量的最旧挑战。"""

    expired_keys = [
        key for key, stored in _CHALLENGE_CACHE.items() if stored.expires_at < now
    ]
    for key in expired_keys:
        _CHALLENGE_CACHE.pop(key, None)
    capacity = _cache_capacity()
    while len(_CHALLENGE_CACHE) > capacity:
        _CHALLENGE_CACHE.popitem(last=False)


def _remember_challenge(token: str, purpose: str, expires_at: int) -> None:
    """把新挑战放入有界缓存；该过程不接触数据库。"""

    now = int(time.time())
    key = _token_cache_key(purpose, token)
    with _CACHE_LOCK:
        _prune_cache_locked(now)
        _CHALLENGE_CACHE[key] = _StoredChallenge(purpose=purpose, expires_at=expires_at)
        _CHALLENGE_CACHE.move_to_end(key)
        _prune_cache_locked(now)


def _png_chunk(chunk_type: bytes, data: bytes) -> bytes:
    """构造带 CRC 的标准 PNG 数据块。"""

    payload = chunk_type + data
    return struct.pack(">I", len(data)) + payload + struct.pack(">I", zlib.crc32(payload) & 0xFFFFFFFF)


def _render_png(answer: str) -> bytes:
    """用标准库把点阵数字绘制为带噪点的 RGB PNG。"""

    width, height = 166, 54
    pixels = bytearray([246, 249, 248] * width * height)
    random_source = secrets.SystemRandom()

    def set_pixel(x: int, y: int, color: tuple[int, int, int]) -> None:
        if x < 0 or x >= width or y < 0 or y >= height:
            return
        offset = (y * width + x) * 3
        pixels[offset : offset + 3] = bytes(color)

    # 先铺浅色噪点，再绘制数字，既保持可读性，也避免整张图只有规则点阵。
    for _ in range(420):
        set_pixel(
            random_source.randrange(width),
            random_source.randrange(height),
            random_source.choice(((199, 220, 215), (211, 222, 234), (225, 214, 198))),
        )

    scale = 5
    digit_width = 5 * scale
    spacing = 6
    start_x = 8
    for index, digit in enumerate(answer):
        bitmap = DIGIT_BITMAPS[digit]
        offset_x = start_x + index * (digit_width + spacing) + random_source.randrange(-1, 2)
        offset_y = 9 + random_source.randrange(-2, 3)
        color = random_source.choice(((25, 78, 72), (31, 66, 98), (43, 55, 62)))
        for row_index, row in enumerate(bitmap):
            for column_index, enabled in enumerate(row):
                if enabled != "1":
                    continue
                for dy in range(scale):
                    for dx in range(scale):
                        set_pixel(
                            offset_x + column_index * scale + dx,
                            offset_y + row_index * scale + dy,
                            color,
                        )

    raw_rows = b"".join(
        b"\x00" + bytes(pixels[row * width * 3 : (row + 1) * width * 3])
        for row in range(height)
    )
    return b"".join(
        (
            b"\x89PNG\r\n\x1a\n",
            _png_chunk(b"IHDR", struct.pack(">IIBBBBB", width, height, 8, 2, 0, 0, 0)),
            _png_chunk(b"IDAT", zlib.compress(raw_rows, level=9)),
            _png_chunk(b"IEND", b""),
        )
    )


def create_captcha_challenge(
    *,
    purpose: str = CAPTCHA_PURPOSE_REGISTRATION,
    answer: str | None = None,
    ttl_seconds: int | None = None,
) -> CaptchaChallenge:
    """创建并登记一次短时验证码；显式答案参数仅用于定向测试验证签名算法。"""

    normalized_purpose = _normalize_purpose(purpose)
    resolved_answer = answer if answer is not None else "".join(secrets.choice("23456789") for _ in range(5))
    if len(resolved_answer) != 5 or any(value not in DIGIT_BITMAPS for value in resolved_answer):
        raise ValueError("captcha_answer_invalid")
    nonce = secrets.token_urlsafe(18)
    expires_at = int(time.time()) + _cache_ttl(ttl_seconds)
    payload = {
        "n": nonce,
        "e": expires_at,
        "p": normalized_purpose,
        "d": _answer_digest(normalized_purpose, nonce, resolved_answer),
    }
    encoded_payload = base64.urlsafe_b64encode(
        json.dumps(payload, separators=(",", ":"), sort_keys=True).encode("utf-8")
    ).decode("ascii").rstrip("=")
    token = f"{encoded_payload}.{_sign(normalized_purpose, encoded_payload)}"
    _remember_challenge(token, normalized_purpose, expires_at)
    image_data_uri = "data:image/png;base64," + base64.b64encode(_render_png(resolved_answer)).decode("ascii")
    return CaptchaChallenge(
        token=token,
        image_data_uri=image_data_uri,
        expires_at=expires_at,
        purpose=normalized_purpose,
    )


def consume_captcha_challenge(
    token: str,
    submitted_answer: str,
    *,
    purpose: str,
) -> CaptchaVerification:
    """验证并一次性消费挑战；任何答案都会销毁已登记令牌，防止在线穷举。"""

    try:
        normalized_purpose = _normalize_purpose(purpose)
    except ValueError:
        return CaptchaVerification(False, "purpose")
    normalized_token = (token or "").strip()
    if not normalized_token or len(normalized_token) > 2048:
        return CaptchaVerification(False, "malformed")
    try:
        encoded_payload, signature = normalized_token.split(".", 1)
        if not encoded_payload or not signature:
            return CaptchaVerification(False, "malformed")
        if not hmac.compare_digest(signature, _sign(normalized_purpose, encoded_payload)):
            return CaptchaVerification(False, "signature")
        padding = "=" * (-len(encoded_payload) % 4)
        payload = json.loads(base64.urlsafe_b64decode(encoded_payload + padding))
        nonce = str(payload["n"])
        expires_at = int(payload["e"])
        payload_purpose = str(payload["p"])
        expected_digest = str(payload["d"])
    except (binascii.Error, KeyError, TypeError, ValueError, json.JSONDecodeError):
        return CaptchaVerification(False, "malformed")
    if payload_purpose != normalized_purpose:
        return CaptchaVerification(False, "purpose")
    now = int(time.time())
    if expires_at < int(time.time()):
        with _CACHE_LOCK:
            _CHALLENGE_CACHE.pop(_token_cache_key(normalized_purpose, normalized_token), None)
        return CaptchaVerification(False, "expired")

    # 先从缓存移除再检查答案，错误答案也会消耗挑战；锁内的 pop 让并发请求只有一个能继续。
    cache_key = _token_cache_key(normalized_purpose, normalized_token)
    with _CACHE_LOCK:
        _prune_cache_locked(now)
        stored = _CHALLENGE_CACHE.pop(cache_key, None)
    if stored is None:
        return CaptchaVerification(False, "replayed_or_unknown")
    if stored.purpose != normalized_purpose or stored.expires_at < now:
        return CaptchaVerification(False, "expired")

    normalized_answer = "".join((submitted_answer or "").split())
    if len(normalized_answer) != 5 or not normalized_answer.isdigit():
        return CaptchaVerification(False, "answer")
    if not hmac.compare_digest(
        expected_digest,
        _answer_digest(normalized_purpose, nonce, normalized_answer),
    ):
        return CaptchaVerification(False, "answer")
    return CaptchaVerification(True, "accepted")
