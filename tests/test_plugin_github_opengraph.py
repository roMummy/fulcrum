from __future__ import annotations

import base64
import os
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parents[1]
TEST_RUNTIME = PROJECT_ROOT / ".run" / "test-runtime" / "plugin-github-opengraph"

os.environ["APP_ENV"] = "test"
os.environ["DATA_DIR"] = str(TEST_RUNTIME / "data")
os.environ["DATABASE_URL"] = f"sqlite:///{TEST_RUNTIME / 'app.db'}"
os.environ["LOG_DIR"] = str(TEST_RUNTIME / "logs")
os.environ["AUTH_SEED_DEMO_USERS"] = "false"

from app.services.plugin_directory import get_discovered_plugin, load_directory_plugin
from app.services.plugin_runtime.contracts import PluginExecutionRequest
from app.services.plugin_runtime.http_client import PluginHttpResponse


class FakeImageClient:
    """返回固定图片并记录 OpenGraph 请求地址。"""

    def __init__(self, *, content_type: str = "image/png"):
        self.content_type = content_type
        self.urls: list[str] = []

    def request(self, method: str, url: str, **_kwargs) -> PluginHttpResponse:
        assert method == "GET"
        self.urls.append(url)
        return PluginHttpResponse(
            status_code=200,
            headers={"content-type": self.content_type},
            body=f"image-{len(self.urls)}".encode("ascii"),
            url=url,
        )


def test_github_opengraph_matches_current_modes_and_secret_schema() -> None:
    """目录声明使用自动链接匹配，并把散列盐标记为租户敏感配置。"""

    discovered = get_discovered_plugin("github_opengraph", force=True)
    assert discovered is not None and discovered.is_valid
    assert discovered.manifest.plugin_type == "rule"
    assert discovered.matcher.keywords == ("github.com/",)
    assert discovered.secret_fields == ("hash_salt",)
    assert "hash_salt" not in discovered.runtime_config
    assert "telegram" in discovered.capability.channel_types

    loaded = load_directory_plugin("github_opengraph", force=True)
    assert loaded is not None and loaded.instance is not None
    module = loaded.instance._module
    fuzzy_context = type(
        "FuzzyContext",
        (),
        {
            "chat_type": "group",
            "message_type": "text",
            "content": "查看 https://github.com/python/cpython/issues/123。",
            "binding_config": {"match_mode": "fuzzy"},
        },
    )()
    exact_context = type(
        "ExactContext",
        (),
        {
            "chat_type": "direct",
            "message_type": "text",
            "content": "查看 https://github.com/python/cpython",
            "binding_config": {"match_mode": "exact"},
        },
    )()
    exact_url_context = type(
        "ExactUrlContext",
        (),
        {
            "chat_type": "direct",
            "message_type": "text",
            "content": "https://github.com/python/cpython?tab=readme#top",
            "binding_config": {"match_mode": "exact"},
        },
    )()

    assert module.matches_event(fuzzy_context)["matched"] is True
    assert module.matches_event(exact_context)["matched"] is False
    assert module.matches_event(exact_url_context)["matched"] is True
    assert module._normalize_github_url("https://github.com/python/cpython 后附文字") == ""


def test_github_opengraph_returns_limited_standard_images(monkeypatch) -> None:
    """重复链接按顺序去重，数量受限，图片只在内存中转换为标准回复。"""

    loaded = load_directory_plugin("github_opengraph", force=True)
    assert loaded is not None and loaded.instance is not None
    module = loaded.instance._module
    fake_client = FakeImageClient()
    monkeypatch.setattr(module, "PluginHttpClient", lambda: fake_client)
    content = (
        "https://github.com/python/cpython/issues/123，"
        "https://github.com/python/cpython/issues/123 "
        "https://github.com/pallets/flask?tab=readme#readme "
        "https://github.com/encode/httpx "
        "https://github.com.evil.test/owner/repo"
    )
    result = module.execute_action(
        PluginExecutionRequest(
            action="execute",
            config={
                "enabled": True,
                "match_mode": "fuzzy",
                "max_links_per_message": 2,
                "hash_salt": "tenant-secret-salt",
                "request_timeout_seconds": 15,
            },
            payload={
                "content": content,
                "preview_url": "http://127.0.0.1/private",
                "hash_salt": "forged-salt",
            },
        )
    )

    assert result.ok is True and result.status == "completed"
    assert result.output["matched_link_count"] == 2
    assert result.output["preview_count"] == 2
    assert len(result.output["replies"]) == 2
    assert result.output["replies"][0] == {
        "type": "image",
        "base64": base64.b64encode(b"image-1").decode("ascii"),
    }
    assert all(url.startswith("https://opengraph.githubassets.com/") for url in fake_client.urls)
    expected = module._build_opengraph_url(
        "https://github.com/python/cpython/issues/123",
        "tenant-secret-salt",
    )
    assert fake_client.urls[0] == expected
    assert "127.0.0.1" not in " ".join(fake_client.urls)
    source = (PROJECT_ROOT / "plugins/github_opengraph/plugin.py").read_text(encoding="utf-8")
    assert "aiohttp" not in source
    assert "requests" not in source
    assert "GroupBotManager" not in source
    assert "send_image_message" not in source


def test_github_opengraph_rejects_non_image_response(monkeypatch) -> None:
    """OpenGraph 非图片响应不能作为图片消息进入渠道队列。"""

    loaded = load_directory_plugin("github_opengraph", force=True)
    assert loaded is not None and loaded.instance is not None
    module = loaded.instance._module
    monkeypatch.setattr(module, "PluginHttpClient", lambda: FakeImageClient(content_type="text/html"))
    result = module.execute_action(
        PluginExecutionRequest(
            action="execute",
            config={"enabled": True, "match_mode": "exact", "hash_salt": "secret"},
            payload={"content": "https://github.com/python/cpython"},
        )
    )

    assert result.ok is False
    assert result.output["provider_status"] == "preview_failed"
    assert result.output["replies"][0]["type"] == "text"
