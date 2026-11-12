from __future__ import annotations

import pytest

import app.services.image_captcha as image_captcha
from app.services.image_captcha import (
    CAPTCHA_PURPOSE_PLATFORM_LOGIN,
    CAPTCHA_PURPOSE_TENANT_LOGIN,
    consume_captcha_challenge,
    create_captcha_challenge,
)


@pytest.fixture(autouse=True)
def clear_captcha_cache() -> None:
    """每个测试前后清理进程内验证码缓存，避免随机挑战相互影响。"""

    with image_captcha._CACHE_LOCK:
        image_captcha._CHALLENGE_CACHE.clear()
    yield
    with image_captcha._CACHE_LOCK:
        image_captcha._CHALLENGE_CACHE.clear()


def test_captcha_challenge_is_signed_and_consumed_once() -> None:
    """正确答案只能消费一次，令牌正文不能直接暴露验证码答案。"""

    challenge = create_captcha_challenge(
        purpose=CAPTCHA_PURPOSE_TENANT_LOGIN,
        answer="23456",
    )

    assert challenge.image_data_uri.startswith("data:image/png;base64,")
    assert "23456" not in challenge.token
    first = consume_captcha_challenge(
        challenge.token,
        "23456",
        purpose=CAPTCHA_PURPOSE_TENANT_LOGIN,
    )
    second = consume_captcha_challenge(
        challenge.token,
        "23456",
        purpose=CAPTCHA_PURPOSE_TENANT_LOGIN,
    )

    assert first.valid is True
    assert first.reason == "accepted"
    assert second.valid is False
    assert second.reason == "replayed_or_unknown"


def test_wrong_answer_consumes_the_challenge() -> None:
    """错误答案也会销毁挑战，不能用同一张图在线穷举答案。"""

    challenge = create_captcha_challenge(
        purpose=CAPTCHA_PURPOSE_TENANT_LOGIN,
        answer="23456",
    )

    wrong = consume_captcha_challenge(
        challenge.token,
        "23457",
        purpose=CAPTCHA_PURPOSE_TENANT_LOGIN,
    )
    retry = consume_captcha_challenge(
        challenge.token,
        "23456",
        purpose=CAPTCHA_PURPOSE_TENANT_LOGIN,
    )

    assert wrong.valid is False
    assert wrong.reason == "answer"
    assert retry.valid is False
    assert retry.reason == "replayed_or_unknown"


def test_challenge_cannot_cross_login_purposes() -> None:
    """租户登录令牌不能拿到平台登录入口继续使用。"""

    challenge = create_captcha_challenge(
        purpose=CAPTCHA_PURPOSE_TENANT_LOGIN,
        answer="23456",
    )

    result = consume_captcha_challenge(
        challenge.token,
        "23456",
        purpose=CAPTCHA_PURPOSE_PLATFORM_LOGIN,
    )

    assert result.valid is False
    assert result.reason == "signature"


def test_expired_challenge_is_rejected(monkeypatch) -> None:
    """短时挑战超过有效期后不能再恢复登录流程。"""

    current_time = 1_700_000_000
    monkeypatch.setattr(image_captcha.time, "time", lambda: current_time)
    challenge = create_captcha_challenge(
        purpose=CAPTCHA_PURPOSE_TENANT_LOGIN,
        answer="23456",
        ttl_seconds=60,
    )

    monkeypatch.setattr(image_captcha.time, "time", lambda: current_time + 61)
    result = consume_captcha_challenge(
        challenge.token,
        "23456",
        purpose=CAPTCHA_PURPOSE_TENANT_LOGIN,
    )

    assert result.valid is False
    assert result.reason == "expired"


def test_cache_evicts_oldest_challenges_when_capacity_is_reached(monkeypatch) -> None:
    """缓存达到容量上限时淘汰最旧挑战，攻击请求不能造成无界内存增长。"""

    monkeypatch.setattr(image_captcha.settings, "captcha_cache_max_entries", 2)
    challenges = [
        create_captcha_challenge(
            purpose=CAPTCHA_PURPOSE_TENANT_LOGIN,
            answer=str(23456 + index),
        )
        for index in range(3)
    ]

    evicted = consume_captcha_challenge(
        challenges[0].token,
        "23456",
        purpose=CAPTCHA_PURPOSE_TENANT_LOGIN,
    )
    retained = consume_captcha_challenge(
        challenges[2].token,
        "23458",
        purpose=CAPTCHA_PURPOSE_TENANT_LOGIN,
    )

    assert evicted.valid is False
    assert evicted.reason == "replayed_or_unknown"
    assert retained.valid is True

