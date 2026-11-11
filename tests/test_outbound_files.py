from __future__ import annotations

from types import SimpleNamespace
import unittest
from unittest.mock import patch

from app.services.outbound_files import build_public_base_url


def _fake_request(*, scheme: str = "http", netloc: str = "127.0.0.1:8180", headers: dict[str, str] | None = None):
    return SimpleNamespace(headers=headers or {}, url=SimpleNamespace(scheme=scheme, netloc=netloc))


class OutboundFilesTests(unittest.TestCase):
    def test_build_public_base_url_prefers_configured_external_base(self):
        # 云端企微 provider 会独立下载出站文件；这里必须优先使用显式外部基址，
        # 避免后台从 127.0.0.1 登录时生成 provider 无法访问的本机地址。
        with patch("app.services.outbound_files.settings.public_base_url", "http://192.168.1.142:8180/"):
            result = build_public_base_url(_fake_request(netloc="127.0.0.1:8180"))

        self.assertEqual(result, "http://192.168.1.142:8180")

    def test_build_public_base_url_falls_back_to_forwarded_headers(self):
        # 未配置 PUBLIC_BASE_URL 时保留原有反代头行为，兼容 nginx / frp 等部署方式。
        request = _fake_request(
            headers={
                "X-Forwarded-Proto": "https",
                "X-Forwarded-Host": "mba.example.com",
                "host": "127.0.0.1:8180",
            }
        )
        with patch("app.services.outbound_files.settings.public_base_url", ""):
            result = build_public_base_url(request)

        self.assertEqual(result, "https://mba.example.com")


if __name__ == "__main__":
    unittest.main()
