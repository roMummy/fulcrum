from __future__ import annotations

import hashlib
import json
import math
from dataclasses import dataclass, field
from datetime import timedelta
from typing import Any, Mapping

from sqlalchemy import delete, or_, select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.core.time import ensure_shanghai_datetime, now_shanghai
from app.models.cultivation import (
    CultivationClan,
    CultivationCooldown,
    CultivationInventory,
    CultivationPlayer,
    CultivationTransaction,
)
from app.models.entities import PointsAccount, PointsTransaction
from app.services.points import PointsService


REALM_ORDER = ("炼气", "筑基", "金丹", "元婴", "化神", "合体", "大乘", "渡劫", "真仙")
REALM_RULES: dict[str, dict[str, Any]] = {
    "炼气": {"threshold": 1_000, "pill": "筑基丹", "pill_rate": 0.70, "hard_rate": 0.10},
    "筑基": {"threshold": 5_000, "pill": "金元丹", "pill_rate": 0.50, "hard_rate": 0.05},
    "金丹": {"threshold": 50_000, "pill": "结婴丹", "pill_rate": 0.40, "hard_rate": 0.03},
    "元婴": {"threshold": 200_000, "pill": "化神丹", "pill_rate": 0.30, "hard_rate": 0.02},
    "化神": {"threshold": 1_000_000, "pill": "合体丹", "pill_rate": 0.20, "hard_rate": 0.01},
    "合体": {"threshold": 5_000_000, "pill": "大乘丹", "pill_rate": 0.10, "hard_rate": 0.005},
    "大乘": {"threshold": 10_000_000, "pill": "渡劫丹", "pill_rate": 0.10, "hard_rate": 0.003},
    "渡劫": {"threshold": 50_000_000, "pill": "飞升丹", "pill_rate": 0.10, "hard_rate": 0.001},
    "真仙": {"threshold": 0, "pill": "", "pill_rate": 0.0, "hard_rate": 0.0},
}
SPIRIT_ROOTS: tuple[tuple[str, float], ...] = (
    ("废灵根", 0.8),
    ("凡灵根", 1.0),
    ("地灵根", 1.2),
    ("天灵根", 2.0),
)
SHOP_ITEMS: dict[str, tuple[str, int]] = {
    "筑基丹": ("丹药", 200),
    "金元丹": ("丹药", 1_000),
    "结婴丹": ("丹药", 10_000),
    "化神丹": ("丹药", 40_000),
    "合体丹": ("丹药", 200_000),
    "大乘丹": ("丹药", 1_000_000),
    "渡劫丹": ("丹药", 2_000_000),
    "飞升丹": ("丹药", 10_000_000),
    "洗髓丹": ("丹药", 2_000),
    "改灵丹": ("丹药", 20_000),
    "天灵露": ("丹药", 200_000),
    "回气丹": ("丹药", 200),
    "聚灵符": ("材料", 100),
}
MATERIAL_TIERS: dict[str, tuple[str, ...]] = {
    "T1": ("灵草", "玄土", "青木藤"),
    "T2": ("赤炎石", "寒晶", "紫电砂"),
    "T3": ("太一金精", "昆仑玉髓"),
}
RECIPES: dict[str, dict[str, Any]] = {
    "聚灵符": {"materials": {"灵草": 3, "玄土": 1}, "stones": 10, "rate": 0.90},
    "回气丹": {"materials": {"灵草": 5, "玄土": 2}, "stones": 20, "rate": 0.85},
    "洗髓丹": {"materials": {"灵草": 10, "玄土": 5}, "stones": 50, "rate": 0.60},
    "改灵丹": {"materials": {"赤炎石": 8, "寒晶": 8}, "stones": 100, "rate": 0.30},
    "天灵露": {"materials": {"太一金精": 4, "昆仑玉髓": 4}, "stones": 200, "rate": 0.10},
}
ROOT_UPGRADES: dict[str, tuple[str, str, float, int]] = {
    "洗髓丹": ("废灵根", "凡灵根", 0.60, 30),
    "改灵丹": ("凡灵根", "地灵根", 0.30, 60),
    "天灵露": ("地灵根", "天灵根", 0.10, 120),
}
READ_ONLY_COMMANDS = {"修仙帮助", "我的状态", "排行榜", "坊市", "乾坤袋"}


@dataclass(frozen=True)
class CultivationCommandContext:
    """保存插件层交叉验证后允许事务服务使用的最小业务上下文。"""

    tenant_id: int
    binding_id: int
    conversation_id: int
    message_id: int
    member_external_id: str
    display_name: str
    group_external_id: str


@dataclass(frozen=True)
class CultivationRules:
    """保存当前租户绑定可调节的收益、保护和冷却参数。"""

    max_cultivate_hours: int = 8
    base_rate_per_hour: int = 100
    unstable_minutes: int = 15
    injured_minutes: int = 60
    point_to_stone_rate: int = 10
    ranking_limit: int = 10
    sign_reward_stones: int = 50
    expedition_cooldown_seconds: int = 86_400
    alchemy_cooldown_seconds: int = 600
    cooldowns: Mapping[str, int] = field(default_factory=dict)


@dataclass(frozen=True)
class CultivationResult:
    """返回一个可直接转换为标准文本回复的业务结果。"""

    ok: bool
    code: str
    text: str
    details: Mapping[str, Any] = field(default_factory=dict)


class CultivationService:
    """实现群内文字修仙的单一事务路径，不访问缓存或具体渠道 Provider。"""

    def __init__(self, db: Session, context: CultivationCommandContext):
        self.db = db
        self.context = context

    def execute(self, command: str, arguments: str, rules: CultivationRules) -> CultivationResult:
        """按正式命令集合执行一次原子操作，并在写操作前处理幂等和冷却。"""

        normalized_command = str(command or "").strip()
        normalized_arguments = str(arguments or "").strip()
        if normalized_command == "修仙帮助":
            return CultivationResult(True, "help", self._help_text())
        if normalized_command in READ_ONLY_COMMANDS:
            return self._execute_read_only(normalized_command, rules)

        previous = self._load_completed_result(normalized_command)
        if previous is not None:
            return previous
        cooldown_seconds = self._cooldown_seconds(normalized_command, rules)
        remaining = self._cooldown_remaining(normalized_command)
        if remaining > 0:
            return CultivationResult(False, "cooldown", f"此法仍在调息，还需等待 {remaining} 秒。")

        handlers = {
            "注册修仙": lambda: self._register(normalized_arguments, normalized_command, cooldown_seconds),
            "积分购石": lambda: self._points_to_stones(normalized_arguments, normalized_command, rules, cooldown_seconds),
            "积分换灵石": lambda: self._points_to_stones(normalized_arguments, normalized_command, rules, cooldown_seconds),
            "闭关": lambda: self._start_cultivation(normalized_command, cooldown_seconds),
            "出关": lambda: self._finish_cultivation(normalized_command, rules, cooldown_seconds),
            "聚灵": lambda: self._gather(normalized_arguments, normalized_command, cooldown_seconds),
            "修仙签到": lambda: self._sign(normalized_command, rules, cooldown_seconds),
            "购买": lambda: self._buy(normalized_arguments, normalized_command, cooldown_seconds),
            "使用": lambda: self._use_item(normalized_arguments, normalized_command, rules, cooldown_seconds),
            "服用": lambda: self._use_item(normalized_arguments, normalized_command, rules, cooldown_seconds),
            "突破": lambda: self._breakthrough(False, normalized_command, rules, cooldown_seconds),
            "强行突破": lambda: self._breakthrough(True, normalized_command, rules, cooldown_seconds),
            "劫掠": lambda: self._rob(normalized_arguments, normalized_command, rules, cooldown_seconds),
            "赠与": lambda: self._gift_stones(normalized_arguments, normalized_command, cooldown_seconds),
            "赠送": lambda: self._gift_item(normalized_arguments, normalized_command, cooldown_seconds),
            "创建门派": lambda: self._create_clan(normalized_arguments, normalized_command, cooldown_seconds),
            "加入门派": lambda: self._join_clan(normalized_arguments, normalized_command, cooldown_seconds),
            "退出门派": lambda: self._exit_clan(normalized_command, cooldown_seconds),
            "出门历练": lambda: self._expedition(normalized_command, rules, cooldown_seconds),
            "炼丹": lambda: self._alchemy(normalized_arguments, normalized_command, rules, cooldown_seconds),
        }
        handler = handlers.get(normalized_command)
        if handler is None:
            return CultivationResult(False, "command_unknown", "未识别的修仙命令，请发送“修仙帮助”。")
        return handler()

    def _execute_read_only(self, command: str, rules: CultivationRules) -> CultivationResult:
        if command == "坊市":
            lines = ["坊市："]
            for name, (item_type, price) in SHOP_ITEMS.items():
                lines.append(f"{name} [{item_type}] {price} 灵石")
            return CultivationResult(True, "shop", "\n".join(lines))
        player = self._lock_player(self.context.member_external_id)
        if player is None:
            return self._not_registered()
        self._refresh_status(player)
        if command == "我的状态":
            clan_name = str(player.clan.name) if player.clan is not None else "无门派"
            return CultivationResult(
                True,
                "status",
                (
                    f"道号：{player.dao_name}\n"
                    f"境界：{player.realm}{int(player.realm_level)}层\n"
                    f"灵根：{player.spirit_root}\n"
                    f"修为：{int(player.cultivation_points)}\n"
                    f"灵石：{int(player.spirit_stones)}\n"
                    f"状态：{self._status_label(player.status)}\n"
                    f"门派：{clan_name}"
                ),
                {"player_id": int(player.id)},
            )
        if command == "排行榜":
            players = self.db.scalars(
                select(CultivationPlayer).where(
                    CultivationPlayer.tenant_id == self.context.tenant_id,
                    CultivationPlayer.bot_plugin_id == self.context.binding_id,
                    CultivationPlayer.conversation_id == self.context.conversation_id,
                )
            ).all()
            players.sort(key=lambda item: self._ranking_key(item), reverse=True)
            lines = ["修仙排行榜："]
            for index, item in enumerate(players[: rules.ranking_limit], start=1):
                lines.append(
                    f"{index}. {item.dao_name} {item.realm}{int(item.realm_level)}层 "
                    f"修为 {int(item.cultivation_points)}"
                )
            return CultivationResult(True, "ranking", "\n".join(lines))
        inventory = self.db.scalars(
            select(CultivationInventory)
            .where(CultivationInventory.player_id == int(player.id), CultivationInventory.quantity > 0)
            .order_by(CultivationInventory.item_type.asc(), CultivationInventory.item_code.asc())
        ).all()
        lines = ["乾坤袋："]
        if not inventory:
            lines.append("空")
        else:
            lines.extend(f"{item.item_code} × {int(item.quantity)}" for item in inventory)
        return CultivationResult(True, "inventory", "\n".join(lines))

    def _register(self, dao_name: str, action: str, cooldown_seconds: int) -> CultivationResult:
        normalized_name = str(dao_name or "").strip()
        if not normalized_name or len(normalized_name) > 32 or any(char in normalized_name for char in "\r\n\t"):
            return CultivationResult(False, "dao_name_invalid", "命令格式：注册修仙 道号，道号不超过 32 个字符。")
        existing = self._lock_player(self.context.member_external_id)
        if existing is not None:
            return CultivationResult(True, "already_registered", f"你已入道，道号为 {existing.dao_name}。")
        same_name = self.db.scalar(
            select(CultivationPlayer.id).where(
                CultivationPlayer.tenant_id == self.context.tenant_id,
                CultivationPlayer.bot_plugin_id == self.context.binding_id,
                CultivationPlayer.conversation_id == self.context.conversation_id,
                CultivationPlayer.dao_name == normalized_name,
            )
        )
        if same_name is not None:
            return CultivationResult(False, "dao_name_exists", "当前群已有同名道号，请更换后重试。")
        root_name, _root_multiplier = SPIRIT_ROOTS[
            self._deterministic_index("spirit-root", len(SPIRIT_ROOTS))
        ]
        player = CultivationPlayer(
            tenant_id=self.context.tenant_id,
            bot_plugin_id=self.context.binding_id,
            conversation_id=self.context.conversation_id,
            member_external_id=self.context.member_external_id,
            display_name=self.context.display_name,
            dao_name=normalized_name,
            realm="炼气",
            realm_level=1,
            spirit_root=root_name,
            cultivation_points=0,
            spirit_stones=0,
            status="idle",
        )
        self.db.add(player)
        self.db.flush()
        return self._finish(
            player,
            action,
            f"入道成功。\n道号：{normalized_name}\n灵根：{root_name}",
            "registered",
            cooldown_seconds,
        )

    def _points_to_stones(
        self,
        arguments: str,
        action: str,
        rules: CultivationRules,
        cooldown_seconds: int,
    ) -> CultivationResult:
        player = self._lock_player(self.context.member_external_id)
        if player is None:
            return self._not_registered()
        amount = self._parse_positive_int(arguments)
        if amount is None or amount % rules.point_to_stone_rate != 0:
            return CultivationResult(
                False,
                "amount_invalid",
                f"兑换积分必须是 {rules.point_to_stone_rate} 的正整数倍。",
            )
        point_account = self.db.scalar(
            select(PointsAccount)
            .where(
                PointsAccount.user_id == self.context.member_external_id,
                PointsAccount.group_id == self.context.group_external_id,
            )
            .with_for_update()
        )
        if point_account is None or int(point_account.total_points or 0) < amount:
            current = int(point_account.total_points or 0) if point_account is not None else 0
            return CultivationResult(False, "points_insufficient", f"积分不足，当前仅有 {current} 积分。")
        gained = amount // rules.point_to_stone_rate
        player.spirit_stones = int(player.spirit_stones) + gained
        self._record_asset(player, action, "spirit_stones", "", gained, int(player.spirit_stones), "stones")
        # 积分服务会提交当前同一个 SQLAlchemy Session。这里先写入灵石流水、
        # 命令结果和冷却但不单独提交，使积分扣减与修仙资产增加共用一次数据库
        # 提交；任何唯一键冲突都会整体回滚，不能产生“积分已扣但灵石未到账”。
        result = self._finish(
            player,
            action,
            f"积分化石成功，消耗 {amount} 积分，获得 {gained} 灵石。",
            "points_exchanged",
            cooldown_seconds,
            commit=False,
        )
        point_result = PointsService(self.db).deduct_points(
            self.context.member_external_id,
            self.context.group_external_id,
            amount,
            user_name=self.context.display_name,
            source="cultivation_exchange",
            description="修仙积分兑换灵石",
            metadata={"binding_id": self.context.binding_id, "message_id": self.context.message_id},
            idempotency_key=self._business_key(action, "points"),
        )
        if not point_result.ok:
            self.db.rollback()
            return CultivationResult(False, "points_failed", point_result.message)
        return result

    def _start_cultivation(self, action: str, cooldown_seconds: int) -> CultivationResult:
        player = self._lock_player(self.context.member_external_id)
        if player is None:
            return self._not_registered()
        self._refresh_status(player)
        if player.status != "idle":
            return CultivationResult(False, "status_blocked", f"当前处于{self._status_label(player.status)}，无法闭关。")
        player.status = "cultivating"
        player.cultivation_started_at = now_shanghai()
        return self._finish(player, action, "已开始闭关，闭关期间不会被劫掠。", "cultivating", cooldown_seconds)

    def _finish_cultivation(
        self,
        action: str,
        rules: CultivationRules,
        cooldown_seconds: int,
    ) -> CultivationResult:
        player = self._lock_player(self.context.member_external_id)
        if player is None:
            return self._not_registered()
        if player.status != "cultivating" or player.cultivation_started_at is None:
            return CultivationResult(False, "not_cultivating", "当前不在闭关状态。")
        current = now_shanghai()
        started_at = ensure_shanghai_datetime(player.cultivation_started_at)
        duration_hours = max(0.0, min((current - started_at).total_seconds() / 3600.0, rules.max_cultivate_hours))
        gain = int(
            duration_hours
            * rules.base_rate_per_hour
            * self._spirit_root_multiplier(player.spirit_root)
            * self._realm_yield_multiplier(player)
        )
        player.cultivation_points = int(player.cultivation_points) + gain
        self._auto_level(player)
        player.status = "unstable"
        player.status_until = current + timedelta(minutes=rules.unstable_minutes)
        player.cultivation_started_at = None
        self._record_asset(
            player,
            action,
            "cultivation_points",
            "",
            gain,
            int(player.cultivation_points),
            "cultivation",
        )
        return self._finish(
            player,
            action,
            (
                f"出关完成，闭关 {duration_hours:.1f} 小时，获得 {gain} 修为。\n"
                f"当前境界：{player.realm}{int(player.realm_level)}层，修为 {int(player.cultivation_points)}。"
            ),
            "cultivation_finished",
            cooldown_seconds,
        )

    def _gather(self, arguments: str, action: str, cooldown_seconds: int) -> CultivationResult:
        player = self._lock_player(self.context.member_external_id)
        if player is None:
            return self._not_registered()
        self._refresh_status(player)
        if player.status == "cultivating":
            return CultivationResult(False, "status_blocked", "闭关中不能聚灵。")
        amount = self._parse_positive_int(arguments)
        if amount is None or int(player.spirit_stones) < amount:
            return CultivationResult(False, "stones_insufficient", "灵石不足或数量不合法。")
        gain = int(amount * 10 * self._realm_yield_multiplier(player))
        player.spirit_stones = int(player.spirit_stones) - amount
        player.cultivation_points = int(player.cultivation_points) + gain
        self._auto_level(player)
        self._record_asset(player, action, "spirit_stones", "", -amount, int(player.spirit_stones), "stone")
        self._record_asset(
            player,
            action,
            "cultivation_points",
            "",
            gain,
            int(player.cultivation_points),
            "cultivation",
        )
        return self._finish(
            player,
            action,
            f"聚灵完成，消耗 {amount} 灵石，获得 {gain} 修为。",
            "gathered",
            cooldown_seconds,
        )

    def _sign(self, action: str, rules: CultivationRules, cooldown_seconds: int) -> CultivationResult:
        player = self._lock_player(self.context.member_external_id)
        if player is None:
            return self._not_registered()
        today = now_shanghai().date()
        if player.last_sign_date == today:
            return CultivationResult(False, "already_signed", "今日已经修仙签到，请明日再来。")
        player.last_sign_date = today
        player.spirit_stones = int(player.spirit_stones) + rules.sign_reward_stones
        self._record_asset(
            player,
            action,
            "spirit_stones",
            "",
            rules.sign_reward_stones,
            int(player.spirit_stones),
            "sign",
        )
        return self._finish(
            player,
            action,
            f"签到成功，获得 {rules.sign_reward_stones} 灵石。",
            "signed",
            cooldown_seconds,
        )

    def _buy(self, arguments: str, action: str, cooldown_seconds: int) -> CultivationResult:
        item_name, quantity = self._parse_item_quantity(arguments)
        if item_name is None or quantity is None:
            return CultivationResult(False, "command_invalid", "命令格式：购买 物品 数量。")
        item = SHOP_ITEMS.get(item_name)
        if item is None:
            return CultivationResult(False, "item_unknown", "坊市中没有这个物品。")
        player = self._lock_player(self.context.member_external_id)
        if player is None:
            return self._not_registered()
        item_type, unit_price = item
        total_price = unit_price * quantity
        if int(player.spirit_stones) < total_price:
            return CultivationResult(False, "stones_insufficient", "灵石不足。")
        player.spirit_stones = int(player.spirit_stones) - total_price
        inventory = self._adjust_item(player, item_name, item_type, quantity)
        self._record_asset(player, action, "spirit_stones", "", -total_price, int(player.spirit_stones), "stone")
        self._record_asset(player, action, "item", item_name, quantity, int(inventory.quantity), "item")
        return self._finish(
            player,
            action,
            f"购买成功：{item_name} × {quantity}，消耗 {total_price} 灵石。",
            "purchased",
            cooldown_seconds,
        )

    def _use_item(
        self,
        arguments: str,
        action: str,
        rules: CultivationRules,
        cooldown_seconds: int,
    ) -> CultivationResult:
        item_name = str(arguments or "").strip()
        if not item_name:
            return CultivationResult(False, "command_invalid", f"命令格式：{action} 物品名。")
        player = self._lock_player(self.context.member_external_id)
        if player is None:
            return self._not_registered()
        self._refresh_status(player)
        inventory = self._inventory_row(player, item_name, lock=True)
        if inventory is None or int(inventory.quantity) <= 0:
            return CultivationResult(False, "item_insufficient", "乾坤袋中没有这个物品。")

        result_text = ""
        result_code = "item_used"
        if item_name == "聚灵符":
            gain = int(500 * self._realm_yield_multiplier(player))
            player.cultivation_points = int(player.cultivation_points) + gain
            self._auto_level(player)
            self._record_asset(
                player,
                action,
                "cultivation_points",
                "",
                gain,
                int(player.cultivation_points),
                "cultivation",
            )
            result_text = f"聚灵符生效，获得 {gain} 修为。"
        elif item_name == "回气丹":
            if player.status not in {"injured", "unstable"}:
                return CultivationResult(False, "status_not_required", "当前状态不需要服用回气丹。")
            player.status = "idle"
            player.status_until = None
            result_text = "服下回气丹，气息恢复平稳。"
        elif item_name in ROOT_UPGRADES:
            source_root, target_root, success_rate, injured_minutes = ROOT_UPGRADES[item_name]
            if player.spirit_root != source_root:
                return CultivationResult(False, "root_not_applicable", "当前灵根不适用这枚丹药。")
            if self._deterministic_unit(f"root:{item_name}") < success_rate:
                player.spirit_root = target_root
                result_text = f"灵根蜕变成功，当前灵根：{target_root}。"
                result_code = "root_upgraded"
            else:
                player.status = "injured"
                player.status_until = now_shanghai() + timedelta(minutes=injured_minutes)
                result_text = f"灵根淬炼失败，受伤保护 {injured_minutes} 分钟。"
                result_code = "root_upgrade_failed"
        else:
            return CultivationResult(False, "item_not_usable", "这个物品不能直接使用。")

        inventory.quantity = int(inventory.quantity) - 1
        self._record_asset(player, action, "item", item_name, -1, int(inventory.quantity), "item")
        return self._finish(player, action, result_text, result_code, cooldown_seconds)

    def _breakthrough(
        self,
        force: bool,
        action: str,
        rules: CultivationRules,
        cooldown_seconds: int,
    ) -> CultivationResult:
        player = self._lock_player(self.context.member_external_id)
        if player is None:
            return self._not_registered()
        self._refresh_status(player)
        if player.status == "cultivating":
            return CultivationResult(False, "status_blocked", "闭关中不能突破。")
        if player.realm == "真仙":
            return CultivationResult(False, "highest_realm", "已至真仙，不再有更高境界。")
        if int(player.realm_level) < 10:
            return CultivationResult(False, "realm_level_insufficient", "需达到当前境界 10 层后才能突破。")
        realm_rule = REALM_RULES[player.realm]
        base_cost = int(realm_rule["threshold"]) * 9
        required_points = base_cost * (2 if force else 1)
        if int(player.cultivation_points) < required_points:
            return CultivationResult(False, "cultivation_insufficient", f"突破需要 {required_points} 修为。")

        pill_inventory: CultivationInventory | None = None
        if not force:
            pill_name = str(realm_rule["pill"])
            pill_inventory = self._inventory_row(player, pill_name, lock=True)
            if pill_inventory is None or int(pill_inventory.quantity) <= 0:
                return CultivationResult(False, "pill_insufficient", f"突破需要 {pill_name}。")
            pill_inventory.quantity = int(pill_inventory.quantity) - 1
            self._record_asset(player, action, "item", pill_name, -1, int(pill_inventory.quantity), "pill")

        player.cultivation_points = int(player.cultivation_points) - required_points
        self._record_asset(
            player,
            action,
            "cultivation_points",
            "",
            -required_points,
            int(player.cultivation_points),
            "cultivation",
        )
        success_rate = float(realm_rule["hard_rate"] if force else realm_rule["pill_rate"])
        success = self._deterministic_unit("force-break" if force else "pill-break") < success_rate
        if success:
            next_realm = REALM_ORDER[REALM_ORDER.index(player.realm) + 1]
            player.realm = next_realm
            player.realm_level = 2 if force else 1
            result_text = f"突破成功，晋升至 {next_realm}{int(player.realm_level)}层。"
            result_code = "breakthrough_succeeded"
        else:
            player.status = "injured" if force else "unstable"
            duration = rules.injured_minutes if force else rules.unstable_minutes
            player.status_until = now_shanghai() + timedelta(minutes=duration)
            result_text = f"突破失败，进入{self._status_label(player.status)} {duration} 分钟。"
            result_code = "breakthrough_failed"
        return self._finish(player, action, result_text, result_code, cooldown_seconds)

    def _rob(
        self,
        target_token: str,
        action: str,
        rules: CultivationRules,
        cooldown_seconds: int,
    ) -> CultivationResult:
        attacker = self._lock_player(self.context.member_external_id)
        if attacker is None:
            return self._not_registered()
        defender, error = self._resolve_target_player(target_token, exclude_self=True)
        if error is not None or defender is None:
            return error or CultivationResult(False, "target_not_found", "未找到劫掠目标。")
        defender = self._lock_player_by_id(int(defender.id))
        if defender is None:
            return CultivationResult(False, "target_not_found", "未找到劫掠目标。")
        self._refresh_status(attacker)
        self._refresh_status(defender)
        if attacker.status != "idle":
            return CultivationResult(False, "attacker_status_blocked", "当前状态不能劫掠。")
        if defender.status in {"cultivating", "injured"}:
            return CultivationResult(False, "target_protected", "目标正在闭关或受伤保护中。")
        if defender.realm == "炼气":
            return CultivationResult(False, "novice_protected", "炼气期受新手保护，暂不可劫掠。")
        if attacker.clan_id is not None and attacker.clan_id == defender.clan_id:
            return CultivationResult(False, "same_clan", "同门之间不可劫掠。")

        success_rate = 0.5
        if self._ranking_key(attacker) > self._ranking_key(defender):
            success_rate += 0.2
        elif self._ranking_key(attacker) < self._ranking_key(defender):
            success_rate -= 0.2
        if self._deterministic_unit("rob") >= success_rate:
            return self._finish(attacker, action, "劫掠失败，对方有所防备。", "rob_failed", cooldown_seconds)
        amount = max(0, int(int(defender.spirit_stones) * 0.2))
        if amount <= 0:
            return CultivationResult(False, "target_empty", "目标没有可劫掠的灵石。")
        defender.spirit_stones = int(defender.spirit_stones) - amount
        attacker.spirit_stones = int(attacker.spirit_stones) + amount
        defender.status = "injured"
        defender.status_until = now_shanghai() + timedelta(minutes=rules.injured_minutes)
        self._record_asset(
            defender,
            action,
            "spirit_stones",
            "",
            -amount,
            int(defender.spirit_stones),
            "defender",
            counterparty=attacker,
        )
        self._record_asset(
            attacker,
            action,
            "spirit_stones",
            "",
            amount,
            int(attacker.spirit_stones),
            "attacker",
            counterparty=defender,
        )
        return self._finish(
            attacker,
            action,
            f"劫掠成功，从 {defender.dao_name} 处获得 {amount} 灵石。",
            "rob_succeeded",
            cooldown_seconds,
        )

    def _gift_stones(self, arguments: str, action: str, cooldown_seconds: int) -> CultivationResult:
        parts = str(arguments or "").split()
        if len(parts) != 2:
            return CultivationResult(False, "command_invalid", "命令格式：赠与 目标 数量。")
        amount = self._parse_positive_int(parts[1])
        if amount is None:
            return CultivationResult(False, "amount_invalid", "赠与数量必须是正整数。")
        giver = self._lock_player(self.context.member_external_id)
        if giver is None:
            return self._not_registered()
        receiver, error = self._resolve_target_player(parts[0], exclude_self=True)
        if error is not None or receiver is None:
            return error or CultivationResult(False, "target_not_found", "未找到赠与目标。")
        receiver = self._lock_player_by_id(int(receiver.id))
        if receiver is None:
            return CultivationResult(False, "target_not_found", "未找到赠与目标。")
        if int(giver.spirit_stones) < amount:
            return CultivationResult(False, "stones_insufficient", "灵石不足。")
        giver.spirit_stones = int(giver.spirit_stones) - amount
        receiver.spirit_stones = int(receiver.spirit_stones) + amount
        self._record_asset(giver, action, "spirit_stones", "", -amount, int(giver.spirit_stones), "giver", counterparty=receiver)
        self._record_asset(receiver, action, "spirit_stones", "", amount, int(receiver.spirit_stones), "receiver", counterparty=giver)
        return self._finish(
            giver,
            action,
            f"已向 {receiver.dao_name} 赠与 {amount} 灵石。",
            "stones_gifted",
            cooldown_seconds,
        )

    def _gift_item(self, arguments: str, action: str, cooldown_seconds: int) -> CultivationResult:
        parts = str(arguments or "").split()
        if len(parts) != 3:
            return CultivationResult(False, "command_invalid", "命令格式：赠送 目标 物品 数量。")
        amount = self._parse_positive_int(parts[2])
        if amount is None:
            return CultivationResult(False, "amount_invalid", "赠送数量必须是正整数。")
        giver = self._lock_player(self.context.member_external_id)
        if giver is None:
            return self._not_registered()
        receiver, error = self._resolve_target_player(parts[0], exclude_self=True)
        if error is not None or receiver is None:
            return error or CultivationResult(False, "target_not_found", "未找到赠送目标。")
        receiver = self._lock_player_by_id(int(receiver.id))
        if receiver is None:
            return CultivationResult(False, "target_not_found", "未找到赠送目标。")
        item_name = parts[1]
        giver_item = self._inventory_row(giver, item_name, lock=True)
        if giver_item is None or int(giver_item.quantity) < amount:
            return CultivationResult(False, "item_insufficient", "乾坤袋中物品不足。")
        giver_item.quantity = int(giver_item.quantity) - amount
        receiver_item = self._adjust_item(receiver, item_name, giver_item.item_type, amount)
        self._record_asset(giver, action, "item", item_name, -amount, int(giver_item.quantity), "giver", counterparty=receiver)
        self._record_asset(receiver, action, "item", item_name, amount, int(receiver_item.quantity), "receiver", counterparty=giver)
        return self._finish(
            giver,
            action,
            f"已向 {receiver.dao_name} 赠送 {item_name} × {amount}。",
            "item_gifted",
            cooldown_seconds,
        )

    def _create_clan(self, name: str, action: str, cooldown_seconds: int) -> CultivationResult:
        normalized_name = str(name or "").strip()
        if not normalized_name or len(normalized_name) > 32:
            return CultivationResult(False, "clan_name_invalid", "命令格式：创建门派 名称，名称不超过 32 个字符。")
        player = self._lock_player(self.context.member_external_id)
        if player is None:
            return self._not_registered()
        if REALM_ORDER.index(player.realm) < REALM_ORDER.index("元婴"):
            return CultivationResult(False, "realm_insufficient", "达到元婴境后才能创建门派。")
        if player.clan_id is not None:
            return CultivationResult(False, "already_in_clan", "你已经加入门派。")
        existing = self.db.scalar(
            select(CultivationClan.id).where(
                CultivationClan.tenant_id == self.context.tenant_id,
                CultivationClan.bot_plugin_id == self.context.binding_id,
                CultivationClan.conversation_id == self.context.conversation_id,
                CultivationClan.name == normalized_name,
            )
        )
        if existing is not None:
            return CultivationResult(False, "clan_exists", "当前群已有同名门派。")
        clan = CultivationClan(
            tenant_id=self.context.tenant_id,
            bot_plugin_id=self.context.binding_id,
            conversation_id=self.context.conversation_id,
            name=normalized_name,
            leader_member_external_id=self.context.member_external_id,
        )
        self.db.add(clan)
        self.db.flush()
        player.clan_id = int(clan.id)
        return self._finish(player, action, f"门派“{normalized_name}”创建成功。", "clan_created", cooldown_seconds)

    def _join_clan(self, name: str, action: str, cooldown_seconds: int) -> CultivationResult:
        normalized_name = str(name or "").strip()
        if not normalized_name:
            return CultivationResult(False, "command_invalid", "命令格式：加入门派 名称。")
        player = self._lock_player(self.context.member_external_id)
        if player is None:
            return self._not_registered()
        clan = self.db.scalar(
            select(CultivationClan)
            .where(
                CultivationClan.tenant_id == self.context.tenant_id,
                CultivationClan.bot_plugin_id == self.context.binding_id,
                CultivationClan.conversation_id == self.context.conversation_id,
                CultivationClan.name == normalized_name,
            )
            .with_for_update()
        )
        if clan is None:
            return CultivationResult(False, "clan_not_found", "当前群没有这个门派。")
        player.clan_id = int(clan.id)
        return self._finish(player, action, f"已加入门派“{clan.name}”。", "clan_joined", cooldown_seconds)

    def _exit_clan(self, action: str, cooldown_seconds: int) -> CultivationResult:
        player = self._lock_player(self.context.member_external_id)
        if player is None:
            return self._not_registered()
        if player.clan_id is None:
            return CultivationResult(False, "not_in_clan", "当前没有加入门派。")
        clan = self.db.get(CultivationClan, int(player.clan_id))
        clan_name = str(clan.name) if clan is not None else "当前门派"
        if clan is not None and clan.leader_member_external_id == self.context.member_external_id:
            self.db.execute(
                update(CultivationPlayer)
                .where(CultivationPlayer.clan_id == int(clan.id))
                .values(clan_id=None)
            )
            self.db.execute(delete(CultivationClan).where(CultivationClan.id == int(clan.id)))
            player.clan_id = None
            text = f"已解散门派“{clan_name}”。"
            code = "clan_dissolved"
        else:
            player.clan_id = None
            text = f"已退出门派“{clan_name}”。"
            code = "clan_exited"
        return self._finish(player, action, text, code, cooldown_seconds)

    def _expedition(
        self,
        action: str,
        rules: CultivationRules,
        cooldown_seconds: int,
    ) -> CultivationResult:
        player = self._lock_player(self.context.member_external_id)
        if player is None:
            return self._not_registered()
        self._refresh_status(player)
        if player.status != "idle":
            return CultivationResult(False, "status_blocked", "当前状态不能出门历练。")
        current = now_shanghai()
        if player.last_expedition_at is not None:
            elapsed = (current - ensure_shanghai_datetime(player.last_expedition_at)).total_seconds()
            if elapsed < rules.expedition_cooldown_seconds:
                return CultivationResult(
                    False,
                    "expedition_cooldown",
                    f"今日历练已完成，还需等待 {math.ceil(rules.expedition_cooldown_seconds - elapsed)} 秒。",
                )

        event_index = self._deterministic_index("expedition", 5)
        event_name = ("洞府遗址", "古老传承", "天材地宝", "大妖横行", "灵脉枯竭")[event_index]
        stone_delta = 0
        cultivation_delta = 0
        materials: dict[str, int] = {}
        if event_name == "洞府遗址":
            stone_delta = 80 + self._deterministic_index("expedition-stones", 121)
            materials[MATERIAL_TIERS["T1"][self._deterministic_index("expedition-mat", 3)]] = 2
        elif event_name == "古老传承":
            cultivation_delta = int((100 + self._deterministic_index("expedition-cult", 401)) * self._realm_yield_multiplier(player))
        elif event_name == "天材地宝":
            stone_delta = 30 + self._deterministic_index("expedition-treasure", 51)
            tier = "T2" if REALM_ORDER.index(player.realm) >= REALM_ORDER.index("金丹") else "T1"
            materials[MATERIAL_TIERS[tier][self._deterministic_index("expedition-tier", len(MATERIAL_TIERS[tier]))]] = 1
        elif event_name == "大妖横行":
            if self._deterministic_unit("expedition-monster") < 0.55:
                stone_delta = max(20, int(player.spirit_stones * 0.2))
                materials["赤炎石"] = 1
            else:
                stone_delta = -min(int(player.spirit_stones), max(10, int(player.spirit_stones * 0.1)))
                player.status = "injured"
                player.status_until = current + timedelta(minutes=rules.injured_minutes)
        player.spirit_stones = int(player.spirit_stones) + stone_delta
        player.cultivation_points = int(player.cultivation_points) + cultivation_delta
        self._auto_level(player)
        player.last_expedition_at = current
        if stone_delta:
            self._record_asset(player, action, "spirit_stones", "", stone_delta, int(player.spirit_stones), "stone")
        if cultivation_delta:
            self._record_asset(
                player,
                action,
                "cultivation_points",
                "",
                cultivation_delta,
                int(player.cultivation_points),
                "cultivation",
            )
        for item_name, quantity in materials.items():
            row = self._adjust_item(player, item_name, "材料", quantity)
            self._record_asset(player, action, "item", item_name, quantity, int(row.quantity), f"item:{item_name}")
        material_text = "、".join(f"{name}×{quantity}" for name, quantity in materials.items()) or "无"
        return self._finish(
            player,
            action,
            f"历练遭遇：{event_name}\n灵石变化：{stone_delta}\n修为变化：{cultivation_delta}\n材料：{material_text}",
            "expedition_completed",
            cooldown_seconds,
        )

    def _alchemy(
        self,
        arguments: str,
        action: str,
        rules: CultivationRules,
        cooldown_seconds: int,
    ) -> CultivationResult:
        item_name, quantity = self._parse_item_quantity(arguments)
        if item_name is None or quantity is None or quantity > 20:
            return CultivationResult(False, "command_invalid", "命令格式：炼丹 物品 数量，单次最多 20 个。")
        recipe = RECIPES.get(item_name)
        if recipe is None:
            return CultivationResult(False, "recipe_unknown", "尚未掌握这个丹方。")
        player = self._lock_player(self.context.member_external_id)
        if player is None:
            return self._not_registered()
        current = now_shanghai()
        if player.last_alchemy_at is not None:
            elapsed = (current - ensure_shanghai_datetime(player.last_alchemy_at)).total_seconds()
            if elapsed < rules.alchemy_cooldown_seconds:
                return CultivationResult(
                    False,
                    "alchemy_cooldown",
                    f"丹炉仍在冷却，还需等待 {math.ceil(rules.alchemy_cooldown_seconds - elapsed)} 秒。",
                )
        stone_cost = int(recipe["stones"]) * quantity
        if int(player.spirit_stones) < stone_cost:
            return CultivationResult(False, "stones_insufficient", "炼丹所需灵石不足。")
        required_rows: dict[str, CultivationInventory] = {}
        for material_name, unit_quantity in dict(recipe["materials"]).items():
            required = int(unit_quantity) * quantity
            row = self._inventory_row(player, material_name, lock=True)
            if row is None or int(row.quantity) < required:
                return CultivationResult(False, "materials_insufficient", f"材料不足：{material_name} 需要 {required}。")
            required_rows[material_name] = row

        player.spirit_stones = int(player.spirit_stones) - stone_cost
        self._record_asset(player, action, "spirit_stones", "", -stone_cost, int(player.spirit_stones), "stone")
        for material_name, row in required_rows.items():
            consumed = int(dict(recipe["materials"])[material_name]) * quantity
            row.quantity = int(row.quantity) - consumed
            self._record_asset(player, action, "item", material_name, -consumed, int(row.quantity), f"material:{material_name}")
        success_count = sum(
            1
            for index in range(quantity)
            if self._deterministic_unit(f"alchemy:{item_name}:{index}") < float(recipe["rate"])
        )
        if success_count > 0:
            product = self._adjust_item(player, item_name, SHOP_ITEMS.get(item_name, ("丹药", 0))[0], success_count)
            self._record_asset(player, action, "item", item_name, success_count, int(product.quantity), "product")
        failed_count = quantity - success_count
        if failed_count > 0:
            player.status = "unstable"
            player.status_until = current + timedelta(minutes=rules.unstable_minutes)
        player.last_alchemy_at = current
        return self._finish(
            player,
            action,
            f"炼丹完成，成丹 {success_count}，失败 {failed_count}。",
            "alchemy_completed",
            cooldown_seconds,
        )

    def _lock_player(self, member_external_id: str) -> CultivationPlayer | None:
        """锁定当前群内玩家，所有资产写操作都从这个统一入口开始。"""

        return self.db.scalar(
            select(CultivationPlayer)
            .where(
                CultivationPlayer.tenant_id == self.context.tenant_id,
                CultivationPlayer.bot_plugin_id == self.context.binding_id,
                CultivationPlayer.conversation_id == self.context.conversation_id,
                CultivationPlayer.member_external_id == str(member_external_id),
            )
            .with_for_update()
        )

    def _lock_player_by_id(self, player_id: int) -> CultivationPlayer | None:
        """按主键加锁时仍重复校验租户、绑定和群，阻断跨群目标注入。"""

        return self.db.scalar(
            select(CultivationPlayer)
            .where(
                CultivationPlayer.id == int(player_id),
                CultivationPlayer.tenant_id == self.context.tenant_id,
                CultivationPlayer.bot_plugin_id == self.context.binding_id,
                CultivationPlayer.conversation_id == self.context.conversation_id,
            )
            .with_for_update()
        )

    def _resolve_target_player(
        self,
        token: str,
        *,
        exclude_self: bool,
    ) -> tuple[CultivationPlayer | None, CultivationResult | None]:
        normalized = str(token or "").strip().lstrip("@")
        if not normalized or len(normalized.split()) != 1:
            return None, CultivationResult(False, "target_invalid", "请提供一个明确的目标账号或道号。")
        conditions = [
            CultivationPlayer.member_external_id == normalized,
            CultivationPlayer.dao_name == normalized,
            CultivationPlayer.display_name == normalized,
        ]
        query = select(CultivationPlayer).where(
            CultivationPlayer.tenant_id == self.context.tenant_id,
            CultivationPlayer.bot_plugin_id == self.context.binding_id,
            CultivationPlayer.conversation_id == self.context.conversation_id,
            or_(*conditions),
        )
        players = self.db.scalars(query).all()
        if exclude_self:
            players = [item for item in players if item.member_external_id != self.context.member_external_id]
        unique = {int(item.id): item for item in players}
        # 显示名允许重复，因此只有查询结果唯一时才能继续。出现重名时要求用户
        # 改用渠道账号 ID，不能为了便利而猜测目标并错误转移玩家资产。
        if len(unique) != 1:
            message = "未找到目标。" if not unique else "目标名称不唯一，请使用账号 ID。"
            return None, CultivationResult(False, "target_not_unique", message)
        return next(iter(unique.values())), None

    def _inventory_row(
        self,
        player: CultivationPlayer,
        item_code: str,
        *,
        lock: bool,
    ) -> CultivationInventory | None:
        query = select(CultivationInventory).where(
            CultivationInventory.tenant_id == self.context.tenant_id,
            CultivationInventory.player_id == int(player.id),
            CultivationInventory.item_code == str(item_code),
        )
        if lock:
            query = query.with_for_update()
        return self.db.scalar(query)

    def _adjust_item(
        self,
        player: CultivationPlayer,
        item_code: str,
        item_type: str,
        delta: int,
    ) -> CultivationInventory:
        row = self._inventory_row(player, item_code, lock=True)
        if row is None:
            if delta < 0:
                raise ValueError("库存不足")
            row = CultivationInventory(
                tenant_id=self.context.tenant_id,
                player_id=int(player.id),
                item_code=str(item_code),
                item_type=str(item_type),
                quantity=0,
            )
            self.db.add(row)
            self.db.flush()
        new_quantity = int(row.quantity) + int(delta)
        if new_quantity < 0:
            raise ValueError("库存不足")
        row.quantity = new_quantity
        return row

    def _record_asset(
        self,
        player: CultivationPlayer,
        action: str,
        resource_type: str,
        resource_code: str,
        amount: int,
        balance_after: int,
        suffix: str,
        *,
        counterparty: CultivationPlayer | None = None,
    ) -> None:
        """记录一条不可重复的资产流水，余额字段用于事后审计与约束校验。"""

        self.db.add(
            CultivationTransaction(
                tenant_id=self.context.tenant_id,
                player_id=int(player.id),
                counterparty_player_id=int(counterparty.id) if counterparty is not None else None,
                message_id=self.context.message_id,
                transaction_type=str(action),
                resource_type=str(resource_type),
                resource_code=str(resource_code),
                amount=int(amount),
                balance_after=int(balance_after),
                idempotency_key=self._business_key(action, suffix),
            )
        )

    def _finish(
        self,
        player: CultivationPlayer,
        action: str,
        text: str,
        code: str,
        cooldown_seconds: int,
        *,
        commit: bool = True,
    ) -> CultivationResult:
        """保存可重放的命令结果，并作为整次命令事务的唯一提交出口。"""

        result = CultivationResult(True, code, text, {"player_id": int(player.id)})
        self.db.add(
            CultivationTransaction(
                tenant_id=self.context.tenant_id,
                player_id=int(player.id),
                message_id=self.context.message_id,
                transaction_type="command_result",
                resource_type="event",
                resource_code=str(action),
                amount=0,
                balance_after=0,
                idempotency_key=self._result_key(action),
                metadata_json=json.dumps(
                    {"ok": result.ok, "code": result.code, "text": result.text, "details": dict(result.details)},
                    ensure_ascii=False,
                    sort_keys=True,
                ),
            )
        )
        self._set_cooldown(action, cooldown_seconds)
        if not commit:
            return result
        try:
            self.db.commit()
            return result
        except IntegrityError:
            # 多 Worker 同时消费同一入站消息时，只允许一个事务写入结果键。
            # 失败事务整体回滚后读取赢家保存的结果，调用方仍得到稳定回复。
            self.db.rollback()
            existing = self._load_completed_result(action)
            if existing is not None:
                return existing
            raise

    def _load_completed_result(self, action: str) -> CultivationResult | None:
        """读取同一入站消息已完成的结果，避免重试再次修改任何资产。"""

        row = self.db.scalar(
            select(CultivationTransaction).where(
                CultivationTransaction.tenant_id == self.context.tenant_id,
                CultivationTransaction.idempotency_key == self._result_key(action),
            )
        )
        if row is None or not row.metadata_json:
            return None
        try:
            payload = json.loads(row.metadata_json)
        except json.JSONDecodeError:
            return None
        if not isinstance(payload, dict):
            return None
        return CultivationResult(
            bool(payload.get("ok", True)),
            str(payload.get("code") or "completed"),
            str(payload.get("text") or "命令已处理。"),
            payload.get("details") if isinstance(payload.get("details"), dict) else {},
        )

    def _set_cooldown(self, action: str, seconds: int) -> None:
        normalized_seconds = max(0, min(int(seconds or 0), 604_800))
        if normalized_seconds <= 0:
            return
        available_at = now_shanghai() + timedelta(seconds=normalized_seconds)
        # 冷却记录位于数据库而非进程内缓存，多 Worker 和服务重启后都遵循同一
        # 可用时间；行锁保证本次命令只会向后更新当前玩家的同一动作。
        row = self.db.scalar(
            select(CultivationCooldown)
            .where(
                CultivationCooldown.tenant_id == self.context.tenant_id,
                CultivationCooldown.bot_plugin_id == self.context.binding_id,
                CultivationCooldown.conversation_id == self.context.conversation_id,
                CultivationCooldown.member_external_id == self.context.member_external_id,
                CultivationCooldown.action == str(action),
            )
            .with_for_update()
        )
        if row is None:
            row = CultivationCooldown(
                tenant_id=self.context.tenant_id,
                bot_plugin_id=self.context.binding_id,
                conversation_id=self.context.conversation_id,
                member_external_id=self.context.member_external_id,
                action=str(action),
                available_at=available_at,
            )
            self.db.add(row)
        else:
            row.available_at = available_at

    def _cooldown_remaining(self, action: str) -> int:
        row = self.db.scalar(
            select(CultivationCooldown).where(
                CultivationCooldown.tenant_id == self.context.tenant_id,
                CultivationCooldown.bot_plugin_id == self.context.binding_id,
                CultivationCooldown.conversation_id == self.context.conversation_id,
                CultivationCooldown.member_external_id == self.context.member_external_id,
                CultivationCooldown.action == str(action),
            )
        )
        if row is None:
            return 0
        remaining = (ensure_shanghai_datetime(row.available_at) - now_shanghai()).total_seconds()
        return max(0, math.ceil(remaining))

    def _refresh_status(self, player: CultivationPlayer) -> None:
        if player.status not in {"injured", "unstable"} or player.status_until is None:
            return
        if ensure_shanghai_datetime(player.status_until) <= now_shanghai():
            player.status = "idle"
            player.status_until = None

    def _auto_level(self, player: CultivationPlayer) -> None:
        threshold = int(REALM_RULES[player.realm]["threshold"])
        if threshold <= 0 or int(player.realm_level) >= 10:
            return
        calculated = min(10, int(player.cultivation_points) // threshold + 1)
        player.realm_level = max(int(player.realm_level), calculated)

    def _ranking_key(self, player: CultivationPlayer) -> tuple[int, int, int]:
        return (
            REALM_ORDER.index(player.realm),
            int(player.realm_level),
            int(player.cultivation_points),
        )

    def _realm_yield_multiplier(self, player: CultivationPlayer) -> float:
        realm_bonus = 1.0 + REALM_ORDER.index(player.realm) * 0.1
        layer_bonus = 1.0 + max(0, int(player.realm_level) - 1) * 0.02
        return realm_bonus * layer_bonus

    @staticmethod
    def _spirit_root_multiplier(root_name: str) -> float:
        for name, multiplier in SPIRIT_ROOTS:
            if name == root_name:
                return multiplier
        return 1.0

    def _deterministic_unit(self, salt: str) -> float:
        """根据可信消息身份生成稳定随机数，使重试不会得到不同结算结果。"""

        raw = (
            f"{self.context.tenant_id}:{self.context.binding_id}:{self.context.conversation_id}:"
            f"{self.context.message_id}:{self.context.member_external_id}:{salt}"
        )
        digest = hashlib.sha256(raw.encode("utf-8")).digest()
        return int.from_bytes(digest[:8], "big") / float(2**64 - 1)

    def _deterministic_index(self, salt: str, size: int) -> int:
        return min(size - 1, int(self._deterministic_unit(salt) * size))

    def _cooldown_seconds(self, command: str, rules: CultivationRules) -> int:
        if command == "出门历练":
            return rules.expedition_cooldown_seconds
        if command == "炼丹":
            return rules.alchemy_cooldown_seconds
        return max(0, int(rules.cooldowns.get(command, 0)))

    def _business_key(self, action: str, suffix: str) -> str:
        return (
            f"cultivation:{self.context.binding_id}:{self.context.conversation_id}:"
            f"{self.context.message_id}:{action}:{suffix}"
        )[:191]

    def _result_key(self, action: str) -> str:
        return self._business_key(action, "result")

    @staticmethod
    def _parse_positive_int(value: str) -> int | None:
        try:
            parsed = int(str(value or "").strip())
        except (TypeError, ValueError):
            return None
        return parsed if parsed > 0 else None

    def _parse_item_quantity(self, value: str) -> tuple[str | None, int | None]:
        parts = str(value or "").split()
        if len(parts) != 2:
            return None, None
        return parts[0], self._parse_positive_int(parts[1])

    @staticmethod
    def _status_label(status: str) -> str:
        return {
            "idle": "空闲",
            "cultivating": "闭关",
            "injured": "受伤保护",
            "unstable": "气息不稳",
        }.get(str(status), str(status))

    @staticmethod
    def _not_registered() -> CultivationResult:
        return CultivationResult(False, "not_registered", "尚未踏入仙途，请发送：注册修仙 道号。")

    @staticmethod
    def _help_text() -> str:
        return (
            "修仙命令：\n"
            "注册修仙 道号｜我的状态｜闭关｜出关｜聚灵 数量｜排行榜｜修仙签到\n"
            "坊市｜购买 物品 数量｜乾坤袋｜使用 物品｜突破｜强行突破\n"
            "积分购石 积分数｜劫掠 目标｜赠与 目标 数量｜赠送 目标 物品 数量\n"
            "创建门派 名称｜加入门派 名称｜退出门派｜出门历练｜炼丹 物品 数量"
        )
