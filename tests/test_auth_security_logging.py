"""登录验证码的路由行为与安全事件日志:企业登录、平台登录和换图接口。

服务层(签发、一次性消费、用途绑定、过期、容量淘汰)由 test_image_captcha 覆盖;
这里验证它接进登录入口之后的行为,以及日志里不会出现密码、验证码答案和令牌。
"""

from __future__ import annotations

import logging
import re

import pytest
from sqlalchemy import select

from app.models.saas import TenantSession
from app.services.image_captcha import CAPTCHA_PURPOSE_PLATFORM_LOGIN, CAPTCHA_PURPOSE_TENANT_LOGIN, consume_captcha_challenge
from captcha_helpers import KNOWN_ANSWER, captcha_fields, platform_login_captcha, tenant_login_captcha
from test_public_site_auth import OWNER_PASSWORD, client, session_factory  # noqa: F401  (含 fixture)

PLATFORM_PASSWORD = "Platform-secret-123"
MOBILE = "13800138000"
CAPTCHA_ERROR = "图形验证码错误或已过期"


def _token_of(html: str) -> str:
    match = re.search(r'name="captcha_token" value="([^"]+)"', html)
    assert match, "页面里没有验证码令牌"
    return match.group(1)


@pytest.fixture()
def security_log(caplog):
    caplog.set_level(logging.INFO, logger="app.auth.security")
    return caplog


def _events(caplog) -> list[logging.LogRecord]:
    return [record for record in caplog.records if record.name == "app.auth.security"]


# ---------- 页面 ----------

@pytest.mark.parametrize("path, purpose", [("/login", "tenant_login"), ("/platform/login", "platform_login")])
def test_login_pages_show_a_fresh_challenge_each_time(client, path, purpose) -> None:
    first, second = client.get(path), client.get(path)
    assert first.status_code == 200 and 'data-captcha-image' in first.text and 'name="captcha_answer"' in first.text
    assert f'data-refresh-url="/auth/captcha?purpose={purpose}"' in first.text
    assert _token_of(first.text) != _token_of(second.text)  # 每次打开都是新挑战


@pytest.mark.parametrize(
    "path, purpose",
    [("/login", CAPTCHA_PURPOSE_TENANT_LOGIN), ("/platform/login", CAPTCHA_PURPOSE_PLATFORM_LOGIN), ("/register", "registration")],
)
def test_the_page_really_renders_a_usable_challenge(client, path, purpose) -> None:
    """防回归:被 import 的 Jinja 宏看不到页面上下文,曾经让三个页面的令牌和图片都是空的,登录和注册全部失败。

    只看"表单里有 name=captcha_token"不够,必须用页面上渲染出来的真实令牌和图片去验证。
    """

    html = client.get(path).text
    token = _token_of(html)
    assert len(token) > 100  # 空令牌或占位值过不了
    image = re.search(r'<img src="(data:image/png;base64,[A-Za-z0-9+/=]{200,})"[^>]*data-captcha-image', html)
    assert image, "验证码图片没有渲染成内嵌 PNG"
    # 令牌是页面刚签发并登记过的:签名和登记都有效,只有答案不对(换成别的入口则被判用途不符)
    assert consume_captcha_challenge(token, "00000", purpose=purpose).reason == "answer"
    other = _token_of(client.get(path).text)
    assert consume_captcha_challenge(other, "00000", purpose="registration" if purpose != "registration" else CAPTCHA_PURPOSE_TENANT_LOGIN).valid is False


# ---------- 企业登录 ----------

def test_tenant_login_requires_a_captcha_before_checking_the_password(client, session_factory, security_log) -> None:
    missing = client.post("/login", data={"mobile": MOBILE, "password": OWNER_PASSWORD})
    assert missing.status_code == 400 and CAPTCHA_ERROR in missing.text
    assert f'value="{MOBILE}"' in missing.text and OWNER_PASSWORD not in missing.text  # 只回填账号,不回填密码
    with session_factory() as db:
        assert db.scalars(select(TenantSession)).all() == []  # 密码对但没有验证码:不登录
    (event,) = _events(security_log)
    assert event.getMessage() == "login captcha rejected" and event.reason == "malformed" and event.auth_scope == "tenant"


def test_a_wrong_answer_burns_the_challenge_so_the_right_answer_cannot_be_retried(client, security_log) -> None:
    fields = tenant_login_captcha()
    wrong = client.post("/login", data={"mobile": MOBILE, "password": OWNER_PASSWORD, **fields, "captcha_answer": "99999"})
    assert wrong.status_code == 400 and CAPTCHA_ERROR in wrong.text
    retried = client.post("/login", data={"mobile": MOBILE, "password": OWNER_PASSWORD, **fields})  # 同一张图换上正确答案
    assert retried.status_code == 400 and CAPTCHA_ERROR in retried.text
    assert [record.reason for record in _events(security_log)] == ["answer", "replayed_or_unknown"]


def test_a_challenge_for_another_entrance_is_refused(client) -> None:
    response = client.post("/login", data={"mobile": MOBILE, "password": OWNER_PASSWORD, **platform_login_captcha()})
    assert response.status_code == 400 and CAPTCHA_ERROR in response.text


def test_a_wrong_password_refreshes_the_challenge_and_logs_without_the_secrets(client, security_log) -> None:
    fields = tenant_login_captcha()
    failed = client.post("/login", data={"mobile": MOBILE, "password": "wrong-password-xyz", **fields})
    assert failed.status_code == 401 and "手机号或密码错误" in failed.text
    assert _token_of(failed.text) != fields["captcha_token"]  # 失败后换了新挑战
    (event,) = _events(security_log)
    assert event.getMessage() == "login credentials rejected" and event.reason == "invalid_credentials"
    assert event.identity_hint == "***8000" and event.levelno == logging.WARNING
    everything = " ".join(str(vars(record)) for record in security_log.records)
    for secret in ("wrong-password-xyz", KNOWN_ANSWER, fields["captcha_token"], MOBILE):
        assert secret not in everything, secret  # 密码、答案、令牌和完整手机号都不进日志


def test_a_successful_login_logs_once_and_the_same_form_cannot_be_replayed(client, security_log) -> None:
    fields = tenant_login_captcha()
    ok = client.post("/login", data={"mobile": MOBILE, "password": OWNER_PASSWORD, **fields})
    assert ok.status_code == 303 and ok.headers["location"] == "/dashboard"
    (event,) = _events(security_log)
    assert (event.getMessage(), event.reason, event.levelno) == ("login succeeded", "authenticated", logging.INFO)
    client.cookies.clear()
    replay = client.post("/login", data={"mobile": MOBILE, "password": OWNER_PASSWORD, **fields})
    assert replay.status_code == 400 and CAPTCHA_ERROR in replay.text  # 抓包重放同一份提交不行


# ---------- 平台登录 ----------

def test_platform_login_has_the_same_protection(client, security_log) -> None:
    missing = client.post("/platform/login", data={"username": "platform", "password": PLATFORM_PASSWORD})
    assert missing.status_code == 400 and CAPTCHA_ERROR in missing.text
    wrong = client.post("/platform/login", data={"username": "platform", "password": "nope-nope-nope", **platform_login_captcha()})
    assert wrong.status_code == 401 and "用户名或密码错误" in wrong.text
    ok = client.post("/platform/login", data={"username": "platform", "password": PLATFORM_PASSWORD, **platform_login_captcha()})
    assert ok.status_code == 303 and not ok.headers["location"].startswith("/platform/login")
    events = _events(security_log)
    assert [(e.getMessage(), e.auth_scope) for e in events] == [
        ("login captcha rejected", "platform"), ("login credentials rejected", "platform"), ("login succeeded", "platform"),
    ]
    assert events[-1].identity_hint == "pl***m"  # 用户名只保留首尾


# ---------- 换图接口 ----------

def test_the_refresh_endpoint_issues_a_usable_challenge_only_for_known_purposes(client) -> None:
    response = client.get("/auth/captcha?purpose=tenant_login")  # 未登录也能换图
    assert response.status_code == 200 and response.headers["cache-control"] != "public"
    body = response.json()
    assert body["purpose"] == "tenant_login" and body["image"].startswith("data:image/png;base64,") and "answer" not in body
    assert consume_captcha_challenge(body["token"], "00000", purpose=CAPTCHA_PURPOSE_PLATFORM_LOGIN).valid is False  # 不能挪给别的入口
    assert client.get("/auth/captcha?purpose=whatever").status_code == 400
    assert client.get("/auth/captcha?purpose=").status_code == 400
    fresh = captcha_fields(CAPTCHA_PURPOSE_TENANT_LOGIN)
    assert consume_captcha_challenge(fresh["captcha_token"], fresh["captcha_answer"], purpose=CAPTCHA_PURPOSE_TENANT_LOGIN).valid is True
