from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path


def _load_provider_module():
    """以独立模块名加载 vendor Provider，避免覆盖主应用的 app 包。"""

    vendor_dir = Path(__file__).resolve().parents[1] / "vendors" / "workwechat_provider"
    module_path = vendor_dir / "app.py"
    spec = importlib.util.spec_from_file_location("workwechat_provider_node_client_app", module_path)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    sys.path.insert(0, str(vendor_dir))
    spec.loader.exec_module(module)
    return module


PROVIDER_APP = _load_provider_module()


def test_legacy_channel_credentials_are_not_promoted_to_device_credentials() -> None:
    """旧渠道 token 的权限范围更小，迁移时只能清理并提示重新激活。"""

    migrated, changed = PROVIDER_APP.migrate_legacy_cloud_config(
        {
            "cloud_base_url": "https://cloud.example.com",
            "authorization_code": "wwauth_legacy",
            "client_token": "wwclient_legacy",
            "cloud_channel_id": 9,
        }
    )

    assert changed is True
    assert migrated["authorization_code"] == ""
    assert migrated["client_token"] == ""
    assert migrated["cloud_channel_id"] is None
    assert migrated["legacy_cloud_registration_detected"] is True
    assert not migrated.get("device_token")


def test_activation_saves_device_identity_and_clears_one_time_code(tmp_path, monkeypatch) -> None:
    """激活成功后只保留设备身份，一次性激活码不得继续留在本地配置。"""

    config_path = tmp_path / "config.json"
    activation_code = "pact_test-activation-code"
    config_path.write_text(
        json.dumps(
            {
                "cloud_poll_enabled": True,
                "cloud_base_url": "https://cloud.example.com",
                "activation_code": activation_code,
            }
        ),
        encoding="utf-8",
    )
    monkeypatch.setattr(PROVIDER_APP, "CONFIG_PATH", config_path)
    monkeypatch.setattr(
        PROVIDER_APP,
        "config",
        PROVIDER_APP.ProviderConfig(
            cloud_poll_enabled=True,
            cloud_base_url="https://cloud.example.com",
            activation_code=activation_code,
        ),
    )
    captured: dict = {}

    def fake_cloud_post(path, body, token="", **kwargs):
        captured.update({"path": path, "body": body, "token": token, "kwargs": kwargs})
        return {
            "ok": True,
            "device_id": "pnode_device-a",
            "device_token": "pdev_device-secret",
            "poll_timeout_seconds": 25,
        }

    monkeypatch.setattr(PROVIDER_APP, "cloud_post_json", fake_cloud_post)

    result = PROVIDER_APP.activate_provider_node_if_needed()
    saved = json.loads(config_path.read_text(encoding="utf-8"))

    assert result == {"ok": True, "activated": True, "device_id": "pnode_device-a"}
    assert captured["path"] == "/api/provider/activate"
    assert captured["body"]["activation_code"] == activation_code
    assert saved["activation_code"] == ""
    assert saved["device_id"] == "pnode_device-a"
    assert saved["device_token"] == "pdev_device-secret"


def test_node_job_is_dispatched_to_cloud_selected_local_account(tmp_path, monkeypatch) -> None:
    """客户端只能按任务中的账号键选择 Hook，不能自行回退到默认账号。"""

    monkeypatch.setattr(PROVIDER_APP, "resolve_cloud_jobs_state_dir", lambda: tmp_path)
    monkeypatch.setattr(
        PROVIDER_APP,
        "config",
        PROVIDER_APP.ProviderConfig(
            cloud_poll_enabled=True,
            cloud_base_url="https://cloud.example.com",
            device_token="pdev_secret",
            delivery_confirmation_enabled=False,
        ),
    )
    monkeypatch.setattr(
        PROVIDER_APP,
        "list_local_provider_accounts",
        lambda: [
            {"account_key": "account-a", "status": "online", "_hook_api_base": "http://127.0.0.1:8989"},
            {"account_key": "account-b", "status": "online", "_hook_api_base": "http://127.0.0.1:8990"},
        ],
    )
    hook_calls: list[dict] = []
    reports: list[dict] = []

    def fake_post_json(url, body, *, timeout, headers=None):
        hook_calls.append({"url": url, "body": body, "timeout": timeout, "headers": headers})
        return {"ok": True, "message": "sent"}

    def fake_complete(job_id, result, *, lease_token=""):
        reports.append({"job_id": job_id, "result": result, "lease_token": lease_token})
        return {"ok": True}

    monkeypatch.setattr(PROVIDER_APP, "post_json", fake_post_json)
    monkeypatch.setattr(PROVIDER_APP, "report_cloud_job_complete", fake_complete)

    result = PROVIDER_APP.process_cloud_job(
        {
            "job_id": 101,
            "payload_hash": "hash-101",
            "provider_account_key": "account-b",
            "lease_token": "please_lease-101",
            "payload": {"receiver_id": "R:room-1", "message_type": "text", "content": "hello"},
        }
    )

    assert result["ok"] is True
    assert hook_calls[0]["url"] == "http://127.0.0.1:8990/api"
    assert reports[0]["lease_token"] == "please_lease-101"


def test_replayed_node_job_only_replenishes_completion_report(tmp_path, monkeypatch) -> None:
    """同一 job_id 与 payload_hash 重放时只补完成回报，不重复调用企微 Hook。"""

    monkeypatch.setattr(PROVIDER_APP, "resolve_cloud_jobs_state_dir", lambda: tmp_path)
    monkeypatch.setattr(
        PROVIDER_APP,
        "config",
        PROVIDER_APP.ProviderConfig(device_token="pdev_secret", delivery_confirmation_enabled=False),
    )
    monkeypatch.setattr(
        PROVIDER_APP,
        "list_local_provider_accounts",
        lambda: [{"account_key": "account-a", "status": "online", "_hook_api_base": "http://127.0.0.1:8989"}],
    )
    hook_calls: list[dict] = []
    reports: list[dict] = []
    monkeypatch.setattr(
        PROVIDER_APP,
        "post_json",
        lambda url, body, *, timeout, headers=None: hook_calls.append({"url": url, "body": body}) or {"ok": True},
    )
    monkeypatch.setattr(
        PROVIDER_APP,
        "report_cloud_job_complete",
        lambda job_id, result, *, lease_token="": reports.append(
            {"job_id": job_id, "lease_token": lease_token, "status": result.get("adapter_result", {}).get("status")}
        )
        or {"ok": True},
    )
    job = {
        "job_id": 102,
        "payload_hash": "hash-102",
        "provider_account_key": "account-a",
        "lease_token": "please_lease-102",
        "payload": {"receiver_id": "R:room-1", "message_type": "text", "content": "hello"},
    }

    first = PROVIDER_APP.process_cloud_job(job)
    replay = PROVIDER_APP.process_cloud_job(job)

    assert first["ok"] is True
    assert replay["status"] == "already_processed"
    assert len(hook_calls) == 1
    assert len(reports) == 2


def test_node_job_rejects_missing_or_ambiguous_local_account(tmp_path, monkeypatch) -> None:
    """账号离线或重复配置时必须失败回报，不能把消息发到任意可用企微账号。"""

    monkeypatch.setattr(PROVIDER_APP, "resolve_cloud_jobs_state_dir", lambda: tmp_path)
    monkeypatch.setattr(PROVIDER_APP, "config", PROVIDER_APP.ProviderConfig(device_token="pdev_secret"))
    monkeypatch.setattr(
        PROVIDER_APP,
        "list_local_provider_accounts",
        lambda: [
            {"account_key": "account-a", "status": "online", "_hook_api_base": "http://127.0.0.1:8989"},
            {"account_key": "account-a", "status": "online", "_hook_api_base": "http://127.0.0.1:8990"},
        ],
    )
    failed: list[dict] = []
    monkeypatch.setattr(
        PROVIDER_APP,
        "report_cloud_job_fail",
        lambda job_id, code, result, *, lease_token="": failed.append(
            {"job_id": job_id, "code": code, "lease_token": lease_token}
        )
        or {"ok": False, "job_id": job_id, "status": "failed"},
    )

    result = PROVIDER_APP.process_cloud_job(
        {
            "job_id": 103,
            "payload_hash": "hash-103",
            "provider_account_key": "account-a",
            "lease_token": "please_lease-103",
            "payload": {"receiver_id": "R:room-1", "message_type": "text", "content": "hello"},
        }
    )

    assert result["status"] == "provider_account_not_uniquely_available"
    assert failed == [
        {
            "job_id": 103,
            "code": "provider_account_not_uniquely_available",
            "lease_token": "please_lease-103",
        }
    ]


def test_poll_401_stops_node_loop_and_retry_delay_has_jitter(monkeypatch) -> None:
    """设备凭证失效立即停止轮询；限流和服务端错误使用可预测的抖动退避。"""

    PROVIDER_APP.cloud_ws_stop_event.clear()
    monkeypatch.setattr(
        PROVIDER_APP,
        "config",
        PROVIDER_APP.ProviderConfig(
            cloud_poll_enabled=True,
            cloud_base_url="https://cloud.example.com",
            device_token="pdev_invalid",
        ),
    )
    monkeypatch.setattr(
        PROVIDER_APP,
        "cloud_post_json",
        lambda *args, **kwargs: {"ok": False, "http_status": 401, "detail": "device_token_invalid"},
    )

    result = PROVIDER_APP._poll_cloud_jobs_once_locked()

    assert result["error"] == "device_reactivation_required"
    assert PROVIDER_APP.cloud_ws_stop_event.is_set()
    assert PROVIDER_APP.compute_cloud_retry_delay(1, 429, random_value=0.5) == 5.0
    assert PROVIDER_APP.compute_cloud_retry_delay(3, 500, random_value=0.5) == 8.0


def test_sensitive_node_credentials_are_redacted_from_log_payload() -> None:
    """日志快照不得包含激活码、设备密钥或嵌套 secret 原文。"""

    redacted = PROVIDER_APP.redact_sensitive_payload(
        {
            "activation_code": "pact_plain-secret",
            "device_token": "pdev_plain-secret",
            "details": {"password": "plain-password"},
        }
    )

    serialized = json.dumps(redacted, ensure_ascii=False)
    assert "plain-secret" not in serialized
    assert "plain-password" not in serialized
