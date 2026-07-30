from __future__ import annotations

from pathlib import Path

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class ProviderSettings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="WECHAT_UIA_", extra="ignore")

    provider_name: str = "wechat_windows_uia"
    host: str = "127.0.0.1"
    port: int = 8070
    poll_interval_seconds: float = 2.0
    state_dir: Path = Field(default=Path("data"))
    debug_dump_dir: Path = Field(default=Path("data/debug"))
    log_dir: Path = Field(default=Path("data/logs"))
    main_window_title: str = "微信"
    main_window_class_name: str = "mmui::MainWindow"
    reconnect_attempts: int = 3
    reconnect_delay_seconds: float = 1.0
    session_preview_file: str = "session_previews.json"
    watcher_state_file: str = "watcher_state.json"
    snapshot_file: str = "agent_snapshot.json"
    event_file: str = "session_events.jsonl"
    runtime_log_file: str = "provider.log"
    message_cursor_file: str = "message_cursor_state.json"
    message_cursor_max_ids_per_chat: int = 200
    provider_token: str = ""


settings = ProviderSettings()
