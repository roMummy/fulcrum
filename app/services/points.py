from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import datetime, timedelta

from sqlalchemy import desc, func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.core.time import now_shanghai
from app.services.identity_directory import looks_like_raw_id
from app.models.entities import Conversation, Message, PointsAccount, PointsPrisonRecord, PointsTransaction


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

    # ===== 统一口径入口 =====
    # 积分账户唯一键 = 租户 + 群(会话 external_id,如 xxx@chatroom) + 用户(发送人 wxid)。
    # 所有插件(签到、消费、打劫、兑换等)必须通过下面三个方法按“可信上下文”读写,
    # 不得自行拼 group_id,否则会出现同一个人在不同插件里各有一份积分的割裂账户。

    def resolve_account_key(self, context: object) -> tuple[str, str, str | None]:
        """由插件可信上下文解析唯一账户键,返回 (用户ID, 群ID, 昵称)。

        群ID取会话 external_id(私聊同样取会话 external_id,天然区分),
        昵称取入站消息的发送人昵称,仅用于展示,不参与账户定位。
        """

        user_id = str(getattr(context, "sender_id", "") or "").strip()
        conversation = self.db.get(Conversation, int(getattr(context, "conversation_id", 0) or 0))
        group_id = str(conversation.external_id or "").strip() if conversation is not None else ""
        if not user_id or not group_id:
            raise ValueError("积分账户需要可信的发送人和会话")
        message_id = int(getattr(context, "message_id", 0) or 0)
        message = self.db.get(Message, message_id) if message_id else None
        name = str(message.sender_name or "").strip() if message is not None else ""
        return user_id, group_id, name or None

    def earn(self, context: object, points: int, *, source: str, description: str, idempotency_key: str | None = None, metadata: dict[str, object] | None = None) -> PointsResult:
        """按上下文给当前发送人在当前群的账户增加积分。"""

        user_id, group_id, name = self.resolve_account_key(context)
        return self.add_points(user_id, group_id, points, user_name=name, source=source, description=description, metadata=metadata, idempotency_key=idempotency_key)

    def spend(self, context: object, points: int, *, source: str, description: str, idempotency_key: str | None = None, metadata: dict[str, object] | None = None) -> PointsResult:
        """按上下文从当前发送人在当前群的账户扣除积分,余额不足返回 ok=False。"""

        user_id, group_id, name = self.resolve_account_key(context)
        return self.deduct_points(user_id, group_id, points, user_name=name, source=source, description=description, metadata=metadata, idempotency_key=idempotency_key)

    def group_balances(self, group_id: str) -> dict[str, int]:
        """一个群内所有成员的积分余额(用户ID -> 余额),供后台群成员列表展示。"""

        rows = self.db.execute(select(PointsAccount.user_id, PointsAccount.total_points).where(PointsAccount.group_id == group_id)).all()
        return {str(user_id): int(total or 0) for user_id, total in rows}

    def user_totals(self, user_ids: list[str]) -> dict[str, int]:
        """一批用户跨群的积分合计(用户ID -> 总积分),供后台好友页展示。"""

        if not user_ids:
            return {}
        rows = self.db.execute(
            select(PointsAccount.user_id, func.sum(PointsAccount.total_points))
            .where(PointsAccount.user_id.in_(user_ids))
            .group_by(PointsAccount.user_id)
        ).all()
        return {str(user_id): int(total or 0) for user_id, total in rows}

    def balance(self, context: object) -> int:
        """按上下文查询当前发送人在当前群的积分余额。"""

        user_id, group_id, _name = self.resolve_account_key(context)
        account = self.get_user_points(user_id, group_id)
        return int(account["total_points"]) if account else 0

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
        elif user_name and (not account.user_name or (looks_like_raw_id(account.user_name) and not looks_like_raw_id(user_name))):
            # 账户昵称缺失,或此前只存了 wxid 时,用真实昵称回填。
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

    def settle_robbery_success(
        self,
        robber_user_id: str,
        target_user_id: str,
        group_id: str,
        *,
        robbed_points: int,
        tax_points: int,
        robber_user_name: str | None = None,
        target_user_name: str | None = None,
        idempotency_key: str,
    ) -> PointsResult:
        """在一个事务中完成目标扣款、打劫者入账和系统抽成。"""

        normalized_key = self._normalize_idempotency_key(idempotency_key, max_length=175)
        if normalized_key is None:
            raise ValueError("打劫结算必须提供幂等键")
        if robber_user_id == target_user_id:
            return PointsResult(ok=False, message="不能打劫自己。")
        total_points = int(robbed_points)
        tax_amount = int(tax_points)
        actual_amount = total_points - tax_amount
        if total_points <= 0 or tax_amount <= 0 or actual_amount <= 0:
            return PointsResult(ok=False, message="打劫结算金额不合法。")

        final_key = f"{normalized_key}:tax"
        existing = self._find_idempotent_transaction(final_key)
        if existing is not None:
            return self._robbery_result(
                robber_user_id,
                target_user_id,
                group_id,
                actual_amount=actual_amount,
                tax_amount=tax_amount,
                idempotent=True,
                transaction_id=int(existing.id),
            )

        robber = self.ensure_account(robber_user_id, group_id, robber_user_name)
        target = self.ensure_account(target_user_id, group_id, target_user_name)
        system = self.ensure_account(SYSTEM_USER_ID, group_id, "系统")
        if int(target.total_points or 0) < total_points:
            return PointsResult(
                ok=False,
                message=f"目标积分不足，当前仅有 {int(target.total_points or 0)} 积分。",
                account=self._serialize_account(target),
            )

        target.total_points = int(target.total_points or 0) - total_points
        robber.total_points = int(robber.total_points or 0) + actual_amount
        system.total_points = int(system.total_points or 0) + tax_amount
        # 三条流水使用同一个业务键的固定后缀，最终 tax 键承担整个事务的并发仲裁。
        self._create_transaction(
            user_id=target_user_id,
            group_id=group_id,
            points_delta=-total_points,
            balance_after=target.total_points,
            source="rob",
            description=f"被 {robber_user_name or robber_user_id} 打劫",
            counterparty_user_id=robber_user_id,
            metadata={"robbed_points": total_points, "tax_points": tax_amount},
            idempotency_key=f"{normalized_key}:target",
        )
        self._create_transaction(
            user_id=robber_user_id,
            group_id=group_id,
            points_delta=actual_amount,
            balance_after=robber.total_points,
            source="rob",
            description=f"打劫 {target_user_name or target_user_id} 成功",
            counterparty_user_id=target_user_id,
            metadata={"robbed_points": total_points, "tax_points": tax_amount},
            idempotency_key=f"{normalized_key}:robber",
        )
        self._create_transaction(
            user_id=SYSTEM_USER_ID,
            group_id=group_id,
            points_delta=tax_amount,
            balance_after=system.total_points,
            source="rob_tax",
            description="打劫系统结算",
            counterparty_user_id=target_user_id,
            metadata={"robber_user_id": robber_user_id, "robbed_points": total_points},
            idempotency_key=final_key,
        )
        duplicate = self._commit_or_load_duplicate(final_key)
        if duplicate is not None:
            return self._robbery_result(
                robber_user_id,
                target_user_id,
                group_id,
                actual_amount=actual_amount,
                tax_amount=tax_amount,
                idempotent=True,
                transaction_id=int(duplicate.id),
            )
        return self._robbery_result(
            robber_user_id,
            target_user_id,
            group_id,
            actual_amount=actual_amount,
            tax_amount=tax_amount,
        )

    def settle_robbery_failure(
        self,
        robber_user_id: str,
        target_user_id: str,
        group_id: str,
        *,
        penalty_points: int,
        prison_hours: int,
        robber_user_name: str | None = None,
        target_user_name: str | None = None,
        idempotency_key: str,
    ) -> PointsResult:
        """在一个事务中完成失败罚款、目标入账和打劫者入狱。"""

        normalized_key = self._normalize_idempotency_key(idempotency_key, max_length=175)
        if normalized_key is None:
            raise ValueError("打劫失败结算必须提供幂等键")
        penalty = int(penalty_points)
        if penalty <= 0:
            return PointsResult(ok=False, message="打劫罚款必须大于 0。")
        final_key = f"{normalized_key}:target"
        existing = self._find_idempotent_transaction(final_key)
        if existing is not None:
            robber = self.ensure_account(robber_user_id, group_id, robber_user_name)
            target = self.ensure_account(target_user_id, group_id, target_user_name)
            return PointsResult(
                ok=True,
                message="打劫失败结算已处理。",
                extra={
                    "idempotent": True,
                    "transaction_id": int(existing.id),
                    "robber": self._serialize_account(robber),
                    "target": self._serialize_account(target),
                    "penalty_points": penalty,
                },
            )

        robber = self.ensure_account(robber_user_id, group_id, robber_user_name)
        target = self.ensure_account(target_user_id, group_id, target_user_name)
        if int(robber.total_points or 0) < penalty:
            return PointsResult(
                ok=False,
                message=f"积分不足，当前仅有 {int(robber.total_points or 0)} 积分。",
                account=self._serialize_account(robber),
            )
        robber.total_points = int(robber.total_points or 0) - penalty
        target.total_points = int(target.total_points or 0) + penalty
        self._create_transaction(
            user_id=robber_user_id,
            group_id=group_id,
            points_delta=-penalty,
            balance_after=robber.total_points,
            source="rob_penalty",
            description=f"打劫 {target_user_name or target_user_id} 失败罚款",
            counterparty_user_id=target_user_id,
            metadata={"prison_hours": int(prison_hours)},
            idempotency_key=f"{normalized_key}:robber",
        )
        self._create_transaction(
            user_id=target_user_id,
            group_id=group_id,
            points_delta=penalty,
            balance_after=target.total_points,
            source="rob_penalty",
            description=f"收到 {robber_user_name or robber_user_id} 的打劫失败罚款",
            counterparty_user_id=robber_user_id,
            metadata={"prison_hours": int(prison_hours)},
            idempotency_key=final_key,
        )

        current_time = now_shanghai()
        active_prisons = self.db.scalars(
            select(PointsPrisonRecord).where(
                PointsPrisonRecord.user_id == robber_user_id,
                PointsPrisonRecord.group_id == group_id,
                PointsPrisonRecord.status == "imprisoned",
            )
        ).all()
        for item in active_prisons:
            item.status = "expired"
            item.end_at = current_time
        self.db.add(
            PointsPrisonRecord(
                user_id=robber_user_id,
                group_id=group_id,
                status="imprisoned",
                reason=f"打劫 {target_user_name or target_user_id} 失败",
                start_at=current_time,
                end_at=current_time + timedelta(hours=max(1, int(prison_hours))),
            )
        )
        duplicate = self._commit_or_load_duplicate(final_key)
        if duplicate is not None:
            robber = self.ensure_account(robber_user_id, group_id, robber_user_name)
            target = self.ensure_account(target_user_id, group_id, target_user_name)
            return PointsResult(
                ok=True,
                message="打劫失败结算已处理。",
                extra={
                    "idempotent": True,
                    "transaction_id": int(duplicate.id),
                    "robber": self._serialize_account(robber),
                    "target": self._serialize_account(target),
                    "penalty_points": penalty,
                },
            )
        return PointsResult(
            ok=True,
            message="打劫失败结算成功。",
            extra={
                "robber": self._serialize_account(robber),
                "target": self._serialize_account(target),
                "penalty_points": penalty,
            },
        )

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

    def bailout_user(
        self,
        prisoner_user_id: str,
        bailout_user_id: str,
        group_id: str,
        *,
        prisoner_user_name: str | None = None,
        bailout_user_name: str | None = None,
        idempotency_key: str,
    ) -> PointsResult:
        """在一个事务中扣除保释费用并释放目标用户。"""

        normalized_key = self._normalize_idempotency_key(idempotency_key)
        if normalized_key is None:
            raise ValueError("保释必须提供幂等键")
        existing = self._find_idempotent_transaction(normalized_key)
        if existing is not None:
            account = self.ensure_account(bailout_user_id, group_id, bailout_user_name)
            return PointsResult(
                ok=True,
                message="保释已处理。",
                account=self._serialize_account(account),
                extra={"idempotent": True, "transaction_id": int(existing.id)},
            )
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

        account = self.ensure_account(bailout_user_id, group_id, bailout_user_name)
        if int(account.total_points or 0) < BAILOUT_COST:
            return PointsResult(
                ok=False,
                message=f"积分不足，当前仅有 {int(account.total_points or 0)} 积分。",
                account=self._serialize_account(account),
            )
        account.total_points = int(account.total_points or 0) - BAILOUT_COST
        self._create_transaction(
            user_id=bailout_user_id,
            group_id=group_id,
            points_delta=-BAILOUT_COST,
            balance_after=account.total_points,
            source="bailout",
            description=f"保释 {prisoner_user_name or prisoner_user_id}",
            counterparty_user_id=prisoner_user_id,
            metadata={"prison_record_id": int(prison.id)},
            idempotency_key=normalized_key,
        )

        prison.status = "bailed_out"
        prison.bailed_out_by = bailout_user_id
        prison.end_at = now_shanghai()
        duplicate = self._commit_or_load_duplicate(normalized_key)
        if duplicate is not None:
            account = self.ensure_account(bailout_user_id, group_id, bailout_user_name)
            return PointsResult(
                ok=True,
                message="保释已处理。",
                account=self._serialize_account(account),
                extra={"idempotent": True, "transaction_id": int(duplicate.id)},
            )
        return PointsResult(ok=True, message="保释成功。", account=self._serialize_account(account))

    def _robbery_result(
        self,
        robber_user_id: str,
        target_user_id: str,
        group_id: str,
        *,
        actual_amount: int,
        tax_amount: int,
        idempotent: bool = False,
        transaction_id: int | None = None,
    ) -> PointsResult:
        """统一序列化打劫成功后的三个积分账户。"""

        robber = self.ensure_account(robber_user_id, group_id)
        target = self.ensure_account(target_user_id, group_id)
        system = self.ensure_account(SYSTEM_USER_ID, group_id, "系统")
        extra: dict[str, object] = {
            "robber": self._serialize_account(robber),
            "target": self._serialize_account(target),
            "system": self._serialize_account(system),
            "actual_amount": int(actual_amount),
            "tax_amount": int(tax_amount),
        }
        if idempotent:
            extra["idempotent"] = True
        if transaction_id is not None:
            extra["transaction_id"] = int(transaction_id)
        return PointsResult(ok=True, message="打劫成功结算完成。", extra=extra)

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
