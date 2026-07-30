import base64
import binascii
import hashlib
import hmac

from app.core.settings import settings


def _key_stream(length: int) -> bytes:
    digest = hashlib.sha256(settings.secret_key.encode("utf-8")).digest()
    repeats = (length // len(digest)) + 1
    return (digest * repeats)[:length]


def _looks_like_reasonable_secret(value: str) -> bool:
    text = str(value or "")
    if not text:
        return False
    return all(32 <= ord(ch) <= 126 for ch in text)


def encrypt_secret(value: str | None) -> str | None:
    if not value:
        return None
    raw = value.encode("utf-8")
    encrypted = bytes(left ^ right for left, right in zip(raw, _key_stream(len(raw))))
    return base64.urlsafe_b64encode(encrypted).decode("ascii")


def decrypt_secret(value: str | None) -> str | None:
    if not value:
        return None
    normalized = str(value).strip()
    if not normalized:
        return None
    try:
        raw = base64.urlsafe_b64decode(normalized.encode("ascii"))
        decrypted = bytes(left ^ right for left, right in zip(raw, _key_stream(len(raw))))
        text = decrypted.decode("utf-8")
        return text if _looks_like_reasonable_secret(text) else None
    except (ValueError, binascii.Error):
        # 非 base64 格式时，按历史明文处理。
        return normalized
    except UnicodeDecodeError:
        # 可解码但无法还原为有效 UTF-8，通常是密钥不一致造成的旧密文，避免将乱码当明文使用。
        return None


def mask_secret(value: str | None) -> str:
    if not value:
        return "-"
    if len(value) <= 6:
        return "*" * len(value)
    return f"{value[:3]}***{value[-3:]}"


def sign_webhook_payload(secret: str, body: bytes, timestamp: str = "") -> str:
    message = f"{timestamp}.".encode("utf-8") + body
    return hmac.new(secret.encode("utf-8"), message, hashlib.sha256).hexdigest()


def verify_webhook_signature(secret: str | None, body: bytes, signature: str | None, timestamp: str = "") -> bool:
    if not secret:
        return True
    if not signature:
        return False
    expected = sign_webhook_payload(secret, body, timestamp=timestamp)
    normalized = signature.removeprefix("sha256=").strip()
    return hmac.compare_digest(expected, normalized)


def build_telegram_webhook_secret(bot_token: str | None, channel_id: int | str) -> str | None:
    if not bot_token:
        return None
    material = f"telegram:{channel_id}:{bot_token}".encode("utf-8")
    digest = hashlib.sha256(material).hexdigest()
    return f"tg-{digest[:32]}"


def verify_telegram_webhook_secret(expected_secret: str | None, actual_secret: str | None) -> bool:
    if not expected_secret:
        return True
    if not actual_secret:
        return False
    return hmac.compare_digest(expected_secret, actual_secret.strip())
