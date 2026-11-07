from __future__ import annotations

import json

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models.entities import BotPlugin, Plugin
from app.services.plugin_directory import get_discovered_plugin


def scrub_tenant_plugin_plaintext_secrets(db: Session) -> dict[str, int]:
    """删除当前租户插件普通 JSON 中显式声明的敏感字段。

    该清理只处理历史明文字段，不触碰新密文列。部署升级后新列原本为空；后续租户
    重新录入的可信密文必须保留，避免重复运行维护脚本造成凭证丢失。
    """

    bindings = db.scalars(
        select(BotPlugin)
        .join(Plugin, Plugin.id == BotPlugin.plugin_id)
        .order_by(BotPlugin.id.asc())
    ).all()
    scanned_bindings = 0
    changed_bindings = 0
    removed_fields = 0

    for binding in bindings:
        plugin_code = binding.plugin.code if binding.plugin else ""
        discovered = get_discovered_plugin(plugin_code)
        secret_fields = set(discovered.secret_fields) if discovered else set()
        if not secret_fields:
            continue
        scanned_bindings += 1
        config = _parse_config_json(binding.config_json)
        binding_removed = 0
        for field_name in secret_fields:
            if field_name in config:
                config.pop(field_name, None)
                binding_removed += 1
        if not binding_removed:
            continue
        binding.config_json = _dump_config_json(config)
        changed_bindings += 1
        removed_fields += binding_removed

    db.flush()
    return {
        "scanned_bindings": scanned_bindings,
        "changed_bindings": changed_bindings,
        "removed_fields": removed_fields,
    }


def _parse_config_json(raw_config: str | None) -> dict[str, object]:
    """清理脚本只处理合法 JSON 对象，非法历史内容保持原样并由人工处理。"""

    normalized = str(raw_config or "").strip()
    if not normalized:
        return {}
    try:
        parsed = json.loads(normalized)
    except json.JSONDecodeError:
        return {}
    return parsed if isinstance(parsed, dict) else {}


def _dump_config_json(config: dict[str, object]) -> str | None:
    """使用稳定 JSON 输出，空配置直接清空数据库字段。"""

    return json.dumps(config, ensure_ascii=False, sort_keys=True) if config else None
