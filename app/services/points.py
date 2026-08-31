from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import datetime, timedelta

from sqlalchemy import desc, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.core.time import now_shanghai
from app.models.entities import PointsAccount, PointsPrisonRecord, PointsTransaction


SYSTEM_USER_ID = "SYSTEM"
BAILOUT_COST = 30


@dataclass(frozen=True)
class PointsResult:
    ok: bool
    message: str
    account: dict[str, object] | None = None
    extra: dict[str, object] | None = None


class PointsService:
    def __init__(self, db: Session):
        self.db = db

    def get_user_points(self, user_id: str, group_id: str) -> dict[str, object] | None:
        account = self._find_account(user_id, group_id)
        if account is None:
            return None
        return self._serialize_account(account)

    def ensure_account(self, user_id: str, group_id: str, user_name: str | None = None) -> PointsAccount:
        account = self._find_account(user_id, group_id)
        if account is None:
            account = PointsAccount(user_id=user_id, group_id=group_id, user_name=user_name, total_points=0)
            self.db.add(account)
            self.db.flush()
        elif user_name and not account.user_name:
            account.user_name = user_name
        return account

    def add_points(
        self,
        user_id: str,
        group_id: str,
        points: int,
        *,
        user_name: str | None = None,
        source: str = "other",
        description: str | None = None,
        counterparty_user_id: str | None = None,
        metadata: dict[str, object] | None = None,
        idempotency_key: str | None = None,
    ) -> PointsResult:
        if points <= 0:
            return PointsResult(ok=False, message="积分必须大于 0。")
        normalized_key = self._normalize_idempotency_key(idempotency_key)
        existing = self._find_idempotent_transaction(normalized_key)
        if existing is not None:
            account = self.ensure_account(user_id, group_id, user_name)
            return PointsResult(
                ok=True,
                message="积分已处理。",
                account=self._serialize_account(account),
                extra={"idempotent": True, "transaction_id": int(existing.id)},
            )
        account = self.ensure_account(user_id, group_id, user_name)
        account.total_points = int(account.total_points or 0) + points
        self._create_transaction(
            user_id=user_id,
            group_id=group_id,
            points_delta=points,
            balance_after=account.total_points,
            source=source,
            description=description,
            counterparty_user_id=counterparty_user_id,
            metadata=metadata,
            idempotency_key=normalized_key,
        )
        duplicate = self._commit_or_load_duplicate(normalized_key)
        if duplicate is not None:
            account = self.ensure_account(user_id, group_id, user_name)
            return PointsResult(
                ok=True,
                message="积分已处理。",
                account=self._serialize_account(account),
                extra={"idempotent": True, "transaction_id": int(duplicate.id)},
            )
        return PointsResult(ok=True, message="积分增加成功。", account=self._serialize_account(account))

    def deduct_points(
        self,
        user_id: str,
        group_id: str,
        points: int,
        *,
        user_name: str | None = None,
        source: str = "other",
        description: str | None = None,
        counterparty_user_id: str | None = None,
        metadata: dict[str, object] | None = None,
        idempotency_key: str | None = None,
    ) -> PointsResult:
        if points <= 0:
            return PointsResult(ok=False, message="积分必须大于 0。")
        normalized_key = self._normalize_idempotency_key(idempotency_key)
        existing = self._find_idempotent_transaction(normalized_key)
        if existing is not None:
            account = self.ensure_account(user_id, group_id, user_name)
            return PointsResult(
                ok=True,
                message="积分已处理。",
                account=self._serialize_account(account),
                extra={"idempotent": True, "transaction_id": int(existing.id)},
            )
        account = self.ensure_account(user_id, group_id, user_name)
        if int(account.total_points or 0) < points:
            return PointsResult(
                ok=False,
                message=f"积分不足，当前仅有 {int(account.total_points or 0)} 积分。",
                account=self._serialize_account(account),
            )
        account.total_points = int(account.total_points or 0) - points
        self._create_transaction(
            user_id=user_id,
            group_id=group_id,
            points_delta=-points,
            balance_after=account.total_points,
            source=source,
            description=description,
            counterparty_user_id=counterparty_user_id,
            metadata=metadata,
            idempotency_key=normalized_key,
        )
        duplicate = self._commit_or_load_duplicate(normalized_key)
        if duplicate is not None:
            account = self.ensure_account(user_id, group_id, user_name)
            return PointsResult(
                ok=True,
                message="积分已处理。",
                account=self._serialize_account(account),
                extra={"idempotent": True, "transaction_id": int(duplicate.id)},
            )
        return PointsResult(ok=True, message="积分扣除成功。", account=self._serialize_account(account))

    def transfer_points(
        self,
        from_user_id: str,
        to_user_id: str,
        group_id: str,
        points: int,
        *,
        from_user_name: str | None = None,
        to_user_name: str | None = None,
        description: str | None = None,
        source: str = "trade",
        metadata: dict[str, object] | None = None,
        idempotency_key: str | None = None,
    ) -> PointsResult:
        if points <= 0:
            return PointsResult(ok=False, message="积分必须大于 0。")
        if from_user_id == to_user_id:
            return PointsResult(ok=False, message="不能给自己转账。")

        normalized_key = self._normalize_idempotency_key(idempotency_key, max_length=170)
        credit_key = f"{normalized_key}:credit" if normalized_key else None
        existing = self._find_idempotent_transaction(credit_key)
        if existing is not None:
            from_account = self.ensure_account(from_user_id, group_id, from_user_name)
            to_account = self.ensure_account(to_user_id, group_id, to_user_name)
            return PointsResult(
                ok=True,
                message="积分转账已处理。",
                extra={
                    "idempotent": True,
                    "transaction_id": int(existing.id),
                    "from_user": self._serialize_account(from_account),
                    "to_user": self._serialize_account(to_account),
                },
            )

        from_account = self.ensure_account(from_user_id, group_id, from_user_name)
        if from_user_id != SYSTEM_USER_ID and int(from_account.total_points or 0) < points:
            return PointsResult(
                ok=False,
                message=f"积分不足，当前仅有 {int(from_account.total_points or 0)} 积分。",
                account=self._serialize_account(from_account),
            )

        to_account = self.ensure_account(to_user_id, group_id, to_user_name)
        if from_user_id != SYSTEM_USER_ID:
            from_account.total_points = int(from_account.total_points or 0) - points
            self._create_transaction(
                user_id=from_user_id,
                group_id=group_id,
                points_delta=-points,
                balance_after=from_account.total_points,
                source=source,
                description=description,
                counterparty_user_id=to_user_id,
                metadata=metadata,
                idempotency_key=f"{normalized_key}:debit" if normalized_key else None,
            )
        to_account.total_points = int(to_account.total_points or 0) + points
        self._create_transaction(
            user_id=to_user_id,
            group_id=group_id,
            points_delta=points,
            balance_after=to_account.total_points,
            source=source,
            description=description,
            counterparty_user_id=from_user_id,
            metadata=metadata,
            idempotency_key=credit_key,
        )
        duplicate = self._commit_or_load_duplicate(credit_key)
        if duplicate is not None:
            from_account = self.ensure_account(from_user_id, group_id, from_user_name)
            to_account = self.ensure_account(to_user_id, group_id, to_user_name)
            return PointsResult(
                ok=True,
                message="积分转账已处理。",
                extra={
                    "idempotent": True,
                    "transaction_id": int(duplicate.id),
                    "from_user": self._serialize_account(from_account),
                    "to_user": self._serialize_account(to_account),
                },
            )
        return PointsResult(
            ok=True,
            message="积分转账成功。",
            extra={"from_user": self._serialize_account(from_account), "to_user": self._serialize_account(to_account)},
        )

    def get_user_transactions(self, user_id: str, group_id: str, limit: int = 5) -> list[dict[str, object]]:
        rows = self.db.scalars(
            select(PointsTransaction)
            .where(PointsTransaction.user_id == user_id, PointsTransaction.group_id == group_id)
            .order_by(desc(PointsTransaction.id))
            .limit(limit)
        ).all()
        return [self._serialize_transaction(item) for item in rows]

    def get_points_ranking(self, group_id: str, limit: int = 10) -> list[dict[str, object]]:
        rows = self.db.scalars(
            select(PointsAccount)
            .where(PointsAccount.group_id == group_id)
            .order_by(desc(PointsAccount.total_points), PointsAccount.id.asc())
            .limit(limit)
        ).all()
        return [self._serialize_account(item) for item in rows]

    def check_prison_status(self, user_id: str, group_id: str) -> dict[str, object] | None:
        now = now_shanghai()
        record = self.db.scalar(
            select(PointsPrisonRecord)
            .where(
                PointsPrisonRecord.user_id == user_id,
                PointsPrisonRecord.group_id == group_id,
                PointsPrisonRecord.status == "imprisoned",
                PointsPrisonRecord.end_at > now,
            )
            .order_by(desc(PointsPrisonRecord.id))
        )
        if record is None:
            return None
        return {
            "id": record.id,
            "user_id": record.user_id,
            "group_id": record.group_id,
            "status": record.status,
            "reason": record.reason,
            "start_at": record.start_at,
            "end_at": record.end_at,
            "bailed_out_by": record.bailed_out_by,
        }

    def imprison_user(self, user_id: str, group_id: str, hours: int, reason: str | None = None) -> PointsResult:
        now = now_shanghai()
        current = self.db.scalars(
            select(PointsPrisonRecord).where(
                PointsPrisonRecord.user_id == user_id,
                PointsPrisonRecord.group_id == group_id,
                PointsPrisonRecord.status == "imprisoned",
            )
        ).all()
        for item in current:
            item.status = "expired"
        record = PointsPrisonRecord(
            user_id=user_id,
            group_id=group_id,
            status="imprisoned",
            reason=reason,
            start_at=now,
            end_at=now + timedelta(hours=max(hours, 1)),
        )
        self.db.add(record)
        self.db.commit()
        return PointsResult(ok=True, message="已关押用户。")

    def bailout_user(self, prisoner_user_id: str, bailout_user_id: str, group_id: str) -> PointsResult:
        prison = self.db.scalar(
            select(PointsPrisonRecord)
            .where(
                PointsPrisonRecord.user_id == prisoner_user_id,
                PointsPrisonRecord.group_id == group_id,
                PointsPrisonRecord.status == "imprisoned",
                PointsPrisonRecord.end_at > now_shanghai(),
            )
            .order_by(desc(PointsPrisonRecord.id))
        )
        if prison is None:
            return PointsResult(ok=False, message="目标当前未在服刑。")

        deduct_result = self.deduct_points(
            bailout_user_id,
            group_id,
            BAILOUT_COST,
            source="bailout",
            description=f"保释 {prisoner_user_id}",
            counterparty_user_id=prisoner_user_id,
        )
        if not deduct_result.ok:
            return deduct_result

        prison.status = "bailed_out"
        prison.bailed_out_by = bailout_user_id
        prison.end_at = now_shanghai()
        self.db.commit()
        return PointsResult(ok=True, message="保释成功。")

    def _find_account(self, user_id: str, group_id: str) -> PointsAccount | None:
        return self.db.scalar(select(PointsAccount).where(PointsAccount.user_id == user_id, PointsAccount.group_id == group_id))

    def _create_transaction(
        self,
        *,
        user_id: str,
        group_id: str,
        points_delta: int,
        balance_after: int,
        source: str,
        description: str | None,
        counterparty_user_id: str | None,
        metadata: dict[str, object] | None,
        idempotency_key: str | None,
    ) -> None:
        self.db.add(
            PointsTransaction(
                user_id=user_id,
                group_id=group_id,
                counterparty_user_id=counterparty_user_id,
                points_delta=points_delta,
                balance_after=balance_after,
                source=source,
                idempotency_key=idempotency_key,
                description=description,
                metadata_json=json.dumps(metadata or {}, ensure_ascii=False, sort_keys=True),
            )
        )

    def _find_idempotent_transaction(self, idempotency_key: str | None) -> PointsTransaction | None:
        """在当前租户会话内查找已完成流水，空键保持普通非幂等行为。"""

        if not idempotency_key:
            return None
        return self.db.scalar(
            select(PointsTransaction).where(PointsTransaction.idempotency_key == idempotency_key)
        )

    def _commit_or_load_duplicate(self, idempotency_key: str | None) -> PointsTransaction | None:
        """提交积分事务；并发命中唯一键时回滚本次变更并返回既有流水。"""

        try:
            self.db.commit()
            return None
        except IntegrityError:
            self.db.rollback()
            existing = self._find_idempotent_transaction(idempotency_key)
            if existing is None:
                # 非幂等键冲突或数据库约束异常不能伪装成成功，交给上层记录失败。
                raise
            return existing

    @staticmethod
    def _normalize_idempotency_key(value: str | None, *, max_length: int = 191) -> str | None:
        """限制业务幂等键长度；过长键直接拒绝，不能静默截断造成碰撞。"""

        normalized = str(value or "").strip()
        if not normalized:
            return None
        if len(normalized) > max_length:
            raise ValueError("积分流水幂等键超过长度限制")
        return normalized

    @staticmethod
    def _serialize_account(account: PointsAccount) -> dict[str, object]:
        return {
            "id": account.id,
            "user_id": account.user_id,
            "group_id": account.group_id,
            "user_name": account.user_name,
            "total_points": int(account.total_points or 0),
        }

    @staticmethod
    def _serialize_transaction(item: PointsTransaction) -> dict[str, object]:
        metadata: dict[str, object] = {}
        raw = str(item.metadata_json or "").strip()
        if raw:
            try:
                parsed = json.loads(raw)
            except json.JSONDecodeError:
                parsed = {}
            if isinstance(parsed, dict):
                metadata = parsed
        return {
            "id": item.id,
            "group_id": item.group_id,
            "user_id": item.user_id,
            "counterparty_user_id": item.counterparty_user_id,
            "points": int(item.points_delta or 0),
            "balance_after": int(item.balance_after or 0),
            "source": item.source,
            "idempotency_key": item.idempotency_key,
            "description": item.description,
            "metadata": metadata,
            "created_at": item.created_at,
        }
