"""公开注册使用的短时签名 PNG 图形验证码。"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import secrets
import struct
import time
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


@dataclass(frozen=True)
class CaptchaChallenge:
    """可交给注册模板的验证码令牌和内嵌 PNG，不暴露正确答案。"""

    token: str
    image_data_uri: str
    expires_at: int


def _sign(value: str) -> str:
    """使用应用密钥给验证码载荷签名，阻止浏览器伪造答案摘要或有效期。"""

    return hmac.new(
        settings.secret_key.encode("utf-8"),
        f"registration-captcha:{value}".encode("utf-8"),
        hashlib.sha256,
    ).hexdigest()


def _answer_digest(nonce: str, answer: str) -> str:
    """把答案绑定到随机 nonce，令牌中只保留不可逆摘要。"""

    return hmac.new(
        settings.secret_key.encode("utf-8"),
        f"registration-captcha-answer:{nonce}:{answer}".encode("utf-8"),
        hashlib.sha256,
    ).hexdigest()


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


def create_captcha_challenge(*, answer: str | None = None, ttl_seconds: int = 300) -> CaptchaChallenge:
    """创建一次短时验证码；显式答案参数仅用于定向测试验证签名算法。"""

    resolved_answer = answer if answer is not None else "".join(secrets.choice("23456789") for _ in range(5))
    if len(resolved_answer) != 5 or any(value not in DIGIT_BITMAPS for value in resolved_answer):
        raise ValueError("captcha_answer_invalid")
    nonce = secrets.token_urlsafe(18)
    expires_at = int(time.time()) + max(60, min(int(ttl_seconds), 600))
    payload = {
        "n": nonce,
        "e": expires_at,
        "d": _answer_digest(nonce, resolved_answer),
    }
    encoded_payload = base64.urlsafe_b64encode(
        json.dumps(payload, separators=(",", ":"), sort_keys=True).encode("utf-8")
    ).decode("ascii").rstrip("=")
    token = f"{encoded_payload}.{_sign(encoded_payload)}"
    image_data_uri = "data:image/png;base64," + base64.b64encode(_render_png(resolved_answer)).decode("ascii")
    return CaptchaChallenge(token=token, image_data_uri=image_data_uri, expires_at=expires_at)


def verify_captcha(token: str, submitted_answer: str) -> bool:
    """验证签名、有效期和答案摘要，任何畸形输入都统一返回失败。"""

    normalized_token = (token or "").strip()
    normalized_answer = "".join((submitted_answer or "").split())
    if len(normalized_token) > 2048 or len(normalized_answer) != 5 or not normalized_answer.isdigit():
        return False
    try:
        encoded_payload, signature = normalized_token.split(".", 1)
        if not hmac.compare_digest(signature, _sign(encoded_payload)):
            return False
        padding = "=" * (-len(encoded_payload) % 4)
        payload = json.loads(base64.urlsafe_b64decode(encoded_payload + padding))
        nonce = str(payload["n"])
        expires_at = int(payload["e"])
        expected_digest = str(payload["d"])
    except (KeyError, TypeError, ValueError, json.JSONDecodeError):
        return False
    if expires_at < int(time.time()):
        return False
    return hmac.compare_digest(expected_digest, _answer_digest(nonce, normalized_answer))
