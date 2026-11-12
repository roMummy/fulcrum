"""测试里通过登录验证码的辅助:直接向验证码服务签发一个"已知答案"的挑战。

不在应用里留任何测试后门:挑战登记在和路由同一个进程内缓存里,路由按正常流程消费,
所以走的是真实的校验路径(用途绑定、一次性消费都生效)。每次调用都会签发一个全新的挑战,
同一组字段只能用一次——重复提交同一组字段会被正确地拒绝。
"""

from __future__ import annotations

from app.services.image_captcha import (
    CAPTCHA_PURPOSE_PLATFORM_LOGIN,
    CAPTCHA_PURPOSE_REGISTRATION,
    CAPTCHA_PURPOSE_TENANT_LOGIN,
    create_captcha_challenge,
)

KNOWN_ANSWER = "23456"


def captcha_fields(purpose: str) -> dict[str, str]:
    """返回提交表单需要的 captcha_token / captcha_answer(答案已知,挑战仍是一次性的)。"""

    challenge = create_captcha_challenge(purpose=purpose, answer=KNOWN_ANSWER)
    return {"captcha_token": challenge.token, "captcha_answer": KNOWN_ANSWER}


def tenant_login_captcha() -> dict[str, str]:
    return captcha_fields(CAPTCHA_PURPOSE_TENANT_LOGIN)


def platform_login_captcha() -> dict[str, str]:
    return captcha_fields(CAPTCHA_PURPOSE_PLATFORM_LOGIN)


def registration_captcha() -> dict[str, str]:
    return captcha_fields(CAPTCHA_PURPOSE_REGISTRATION)
