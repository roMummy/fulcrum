"""非开发环境必须配置真实 SECRET_KEY:默认值写在公开源码里,不拦截就等于没有保护。"""

from __future__ import annotations

import os
import tempfile

os.environ.setdefault("LOG_DIR", tempfile.mkdtemp(prefix="secret-guard-logs-"))

import pytest

from app.core.settings import Settings

STRONG = "k" * 48


def _settings(**values) -> Settings:
    return Settings(_env_file=None, **values)


@pytest.mark.parametrize("key", ["change-me", "change-this-secret-key", "Change-Me-Now", "", "   ", "short-key", "a" * 31])
def test_production_refuses_placeholder_or_weak_keys(key) -> None:
    with pytest.raises(RuntimeError, match="SECRET_KEY"):
        _settings(app_env="prod", secret_key=key).validate_runtime_secrets()


def test_production_accepts_a_strong_key() -> None:
    _settings(app_env="prod", secret_key=STRONG).validate_runtime_secrets()
    _settings(app_env="production", secret_key=STRONG).validate_runtime_secrets()


@pytest.mark.parametrize("env", ["dev", "local", "demo", "test", " TEST "])
def test_development_environments_keep_working_with_the_default_key(env) -> None:
    _settings(app_env=env, secret_key="change-me").validate_runtime_secrets()


def test_the_shipped_defaults_are_only_acceptable_in_development() -> None:
    defaults = _settings()
    assert defaults.is_dev_like  # 默认 APP_ENV 是 dev:开箱即用
    defaults.validate_runtime_secrets()
    with pytest.raises(RuntimeError):
        _settings(app_env="prod").validate_runtime_secrets()  # 只改成生产、没配密钥 -> 拒绝
