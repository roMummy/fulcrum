from __future__ import annotations

import hashlib
import json
import math
from dataclasses import dataclass
from datetime import datetime, timedelta

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.core.tenant_context import require_tenant_id
from app.core.time import ensure_shanghai_datetime
from app.models.entities import PluginStateRecord
from app.services.plugin_runtime.contracts import PluginExecutionContext
from app.services.plugin_runtime.state_store import PluginStateError, parse_state_payload


ALLOWED_RATE_LIMIT_SCOPES = {"tenant", "binding", "user", "group"}
RATE_LIMIT_NAMESPACE = "rate_limit"


@dataclass(frozen=True)
class PluginRateLimitDecision:
    """返回一次限流消耗后的明确结果。"""

    allowed: bool
    limit: int
    remaining: int
    retry_after_seconds: int
    reset_at: datetime


class PluginRateLimiter:
    """使用租户插件状态表实现跨进程固定窗口限流。"""

    def __init__(self, db: Session, context: PluginExecutionContext):
        self.db = db
        self.context = context
        current_tenant_id = require_tenant_id()
        if int(context.tenant_id) != current_tenant_id:
            raise PluginStateError("限流上下文与当前租户会话不一致")
        if int(context.binding_id) <= 0:
            raise PluginStateError("限流需要有效的插件绑定编号")

    def consume(
        self,
        scope: str,
        *,
        identity: str | int | None = None,
        limit: int,
        window_seconds: int,
        now: datetime | None = None,
    ) -> PluginRateLimitDecision:
        """消耗一次额度，同一窗口超限后保持拒绝直到窗口结束。"""

        normalized_scope = str(scope or "").strip().lower()
        if normalized_scope not in ALLOWED_RATE_LIMIT_SCOPES:
            raise PluginStateError("限流范围必须是 tenant、binding、user 或 group")
        normalized_limit = int(limit)
        normalized_window = int(window_seconds)
        if normalized_limit <= 0 or normalized_limit > 1_000_000:
            raise PluginStateError("限流次数必须在 1 到 1000000 之间")
        if normalized_window <= 0 or normalized_window > 86_400:
            raise PluginStateError("限流窗口必须在 1 秒到 24 小时之间")

        bucket_key = self._build_bucket_key(normalized_scope, identity)
        current = ensure_shanghai_datetime(now)
        record = self._lock_record(bucket_key)
        if record is None:
            record = self._create_bucket(bucket_key, current, normalized_window)

        expiry = ensure_shanghai_datetime(record.expires_at)
        payload = parse_state_payload(record.payload_json)
        if expiry <= current:
            # 窗口结束后复用同一行，避免高频身份不断累积历史记录。
            count = 0
            expiry = current + timedelta(seconds=normalized_window)
        else:
            count = _coerce_non_negative_int(payload.get("count"))

        allowed = count < normalized_limit
        if allowed:
            count += 1
            record.payload_json = json.dumps(
                {"count": count, "scope": normalized_scope},
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
            )
            record.expires_at = expiry
            self.db.flush()

        retry_after = max(0, math.ceil((expiry - current).total_seconds())) if not allowed else 0
        return PluginRateLimitDecision(
            allowed=allowed,
            limit=normalized_limit,
            remaining=max(0, normalized_limit - count),
            retry_after_seconds=retry_after,
            reset_at=expiry,
        )

    def _build_bucket_key(self, scope: str, identity: str | int | None) -> str:
        """哈希用户和群标识，限流表不直接保存外部联系人或群编号。"""

        if scope == "tenant":
            raw_identity = f"tenant:{int(self.context.tenant_id)}"
        elif scope == "binding":
            raw_identity = f"binding:{int(self.context.binding_id)}"
        else:
            normalized_identity = str(identity or "").strip()
            if not normalized_identity:
                raise PluginStateError(f"{scope} 限流必须提供 identity")
            raw_identity = f"{scope}:{normalized_identity}"
        digest = hashlib.sha256(raw_identity.encode("utf-8")).hexdigest()
        return f"{scope}:{digest}"

    def _lock_record(self, bucket_key: str) -> PluginStateRecord | None:
        """在支持行锁的数据库中串行化同一分桶的计数更新。"""

        return self.db.scalar(
            select(PluginStateRecord)
            .where(
                PluginStateRecord.tenant_id == int(self.context.tenant_id),
                PluginStateRecord.bot_plugin_id == int(self.context.binding_id),
                PluginStateRecord.namespace == RATE_LIMIT_NAMESPACE,
                PluginStateRecord.state_key == bucket_key,
            )
            .with_for_update()
        )

    def _create_bucket(
        self,
        bucket_key: str,
        current: datetime,
        window_seconds: int,
    ) -> PluginStateRecord:
        """创建首次分桶；唯一键竞争时回读另一个事务已经创建的记录。"""

        record = PluginStateRecord(
            tenant_id=int(self.context.tenant_id),
            bot_plugin_id=int(self.context.binding_id),
            namespace=RATE_LIMIT_NAMESPACE,
            state_key=bucket_key,
            payload_json='{"count":0}',
            expires_at=current + timedelta(seconds=window_seconds),
        )
        try:
            # SAVEPOINT 只包围首次插入。若并发事务抢先建立了同一个唯一分桶，
            # 外层插件事务不会整体回滚，随后可以锁定并复用已存在的记录。
            with self.db.begin_nested():
                self.db.add(record)
                self.db.flush()
            return record
        except IntegrityError:
            existing = self._lock_record(bucket_key)
            if existing is None:
                raise PluginStateError("限流分桶创建冲突后无法读取记录")
            return existing


def _coerce_non_negative_int(value: object) -> int:
    try:
        normalized = int(value)
    except (TypeError, ValueError):
        return 0
    return max(0, normalized)
