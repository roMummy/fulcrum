from __future__ import annotations

import json
import re
from datetime import datetime, timedelta
from typing import Any, Mapping

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.tenant_context import require_tenant_id
from app.core.time import ensure_shanghai_datetime, now_shanghai
from app.models.entities import PluginStateRecord
from app.services.plugin_runtime.contracts import PluginExecutionContext


MAX_PLUGIN_STATE_BYTES = 64 * 1024
MAX_NAMESPACE_LENGTH = 80
MAX_STATE_KEY_LENGTH = 191
STATE_LOCATOR_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.:-]*$")


class PluginStateError(ValueError):
    """插件状态定位、容量或租户边界不合法。"""


class PluginStateStore:
    """在当前租户和插件绑定内读写小型 JSON 状态。"""

    def __init__(self, db: Session, context: PluginExecutionContext):
        self.db = db
        self.context = context
        current_tenant_id = require_tenant_id()
        if int(context.tenant_id) != current_tenant_id:
            raise PluginStateError("插件运行上下文与当前租户会话不一致")
        if int(context.binding_id) <= 0:
            raise PluginStateError("插件绑定编号必须为正整数")

    def get(
        self,
        namespace: str,
        state_key: str,
        *,
        now: datetime | None = None,
    ) -> dict[str, Any] | None:
        """读取未过期状态；已过期记录立即删除，避免继续影响插件判断。"""

        normalized_namespace, normalized_key = normalize_state_locator(namespace, state_key)
        record = self._find_record(normalized_namespace, normalized_key)
        if record is None:
            return None
        current = ensure_shanghai_datetime(now)
        if record.expires_at is not None and ensure_shanghai_datetime(record.expires_at) <= current:
            self.db.delete(record)
            self.db.flush()
            return None
        return parse_state_payload(record.payload_json)

    def put(
        self,
        namespace: str,
        state_key: str,
        payload: Mapping[str, Any],
        *,
        ttl_seconds: int | None = None,
        expires_at: datetime | None = None,
        now: datetime | None = None,
    ) -> PluginStateRecord:
        """新增或覆盖状态，并限制单条记录大小和有效期表达。"""

        normalized_namespace, normalized_key = normalize_state_locator(namespace, state_key)
        payload_json = serialize_state_payload(payload)
        resolved_expiry = resolve_state_expiry(
            ttl_seconds=ttl_seconds,
            expires_at=expires_at,
            now=now,
        )
        record = self._find_record(normalized_namespace, normalized_key)
        if record is None:
            record = PluginStateRecord(
                tenant_id=int(self.context.tenant_id),
                bot_plugin_id=int(self.context.binding_id),
                namespace=normalized_namespace,
                state_key=normalized_key,
                payload_json=payload_json,
                expires_at=resolved_expiry,
            )
            self.db.add(record)
        else:
            record.payload_json = payload_json
            record.expires_at = resolved_expiry
        self.db.flush()
        return record

    def delete(self, namespace: str, state_key: str) -> bool:
        """删除一个状态键并返回是否实际找到记录。"""

        normalized_namespace, normalized_key = normalize_state_locator(namespace, state_key)
        record = self._find_record(normalized_namespace, normalized_key)
        if record is None:
            return False
        self.db.delete(record)
        self.db.flush()
        return True

    def delete_namespace(self, namespace: str) -> int:
        """只删除当前绑定的指定命名空间，不跨绑定执行批量清理。"""

        normalized_namespace = normalize_state_namespace(namespace)
        records = self.db.scalars(
            select(PluginStateRecord).where(
                PluginStateRecord.tenant_id == int(self.context.tenant_id),
                PluginStateRecord.bot_plugin_id == int(self.context.binding_id),
                PluginStateRecord.namespace == normalized_namespace,
            )
        ).all()
        for record in records:
            self.db.delete(record)
        self.db.flush()
        return len(records)

    def cleanup_expired(self, *, now: datetime | None = None, limit: int = 500) -> int:
        """分批删除当前绑定的过期状态，避免一次维护事务锁定过多记录。"""

        current = ensure_shanghai_datetime(now)
        bounded_limit = max(1, min(int(limit or 1), 5000))
        records = self.db.scalars(
            select(PluginStateRecord)
            .where(
                PluginStateRecord.tenant_id == int(self.context.tenant_id),
                PluginStateRecord.bot_plugin_id == int(self.context.binding_id),
                PluginStateRecord.expires_at.is_not(None),
                PluginStateRecord.expires_at <= current,
            )
            .order_by(PluginStateRecord.expires_at.asc(), PluginStateRecord.id.asc())
            .limit(bounded_limit)
        ).all()
        for record in records:
            self.db.delete(record)
        self.db.flush()
        return len(records)

    def _find_record(self, namespace: str, state_key: str) -> PluginStateRecord | None:
        """使用租户、绑定、命名空间和键四元组定位唯一状态。"""

        return self.db.scalar(
            select(PluginStateRecord).where(
                PluginStateRecord.tenant_id == int(self.context.tenant_id),
                PluginStateRecord.bot_plugin_id == int(self.context.binding_id),
                PluginStateRecord.namespace == namespace,
                PluginStateRecord.state_key == state_key,
            )
        )


def normalize_state_locator(namespace: str, state_key: str) -> tuple[str, str]:
    """统一校验状态命名空间和键，避免产生不可维护的动态字段。"""

    return normalize_state_namespace(namespace), _normalize_locator_part(
        state_key,
        label="状态键",
        max_length=MAX_STATE_KEY_LENGTH,
    )


def normalize_state_namespace(namespace: str) -> str:
    """校验插件状态命名空间。"""

    return _normalize_locator_part(
        namespace,
        label="状态命名空间",
        max_length=MAX_NAMESPACE_LENGTH,
    )


def serialize_state_payload(payload: Mapping[str, Any]) -> str:
    """把状态序列化为稳定 JSON，并拒绝超出小型状态边界的数据。"""

    if not isinstance(payload, Mapping):
        raise PluginStateError("插件状态必须是 JSON 对象")
    try:
        serialized = json.dumps(dict(payload), ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    except (TypeError, ValueError) as exc:
        raise PluginStateError("插件状态包含不可序列化的数据") from exc
    if len(serialized.encode("utf-8")) > MAX_PLUGIN_STATE_BYTES:
        raise PluginStateError("单条插件状态不能超过 64 KiB")
    return serialized


def parse_state_payload(payload_json: str) -> dict[str, Any]:
    """读取数据库 JSON；损坏记录直接报错，不做空对象隐式降级。"""

    try:
        parsed = json.loads(str(payload_json or ""))
    except json.JSONDecodeError as exc:
        raise PluginStateError("插件状态记录不是合法 JSON") from exc
    if not isinstance(parsed, dict):
        raise PluginStateError("插件状态记录必须是 JSON 对象")
    return parsed


def resolve_state_expiry(
    *,
    ttl_seconds: int | None,
    expires_at: datetime | None,
    now: datetime | None,
) -> datetime | None:
    """把相对和绝对有效期收敛为一个明确时间，禁止同时传入两种表达。"""

    if ttl_seconds is not None and expires_at is not None:
        raise PluginStateError("ttl_seconds 与 expires_at 不能同时设置")
    if expires_at is not None:
        return ensure_shanghai_datetime(expires_at)
    if ttl_seconds is None:
        return None
    normalized_ttl = int(ttl_seconds)
    if normalized_ttl <= 0 or normalized_ttl > 31_536_000:
        raise PluginStateError("插件状态有效期必须在 1 秒到 365 天之间")
    return ensure_shanghai_datetime(now) + timedelta(seconds=normalized_ttl)


def _normalize_locator_part(value: str, *, label: str, max_length: int) -> str:
    normalized = str(value or "").strip()
    if not normalized or len(normalized) > max_length or not STATE_LOCATOR_PATTERN.fullmatch(normalized):
        raise PluginStateError(f"{label}只能包含字母、数字、点、下划线、冒号和连字符")
    return normalized
