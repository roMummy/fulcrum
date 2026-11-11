"""触发规则引擎:规则的校验、保存、列表展示,以及收到事件后的匹配与执行。"""

from __future__ import annotations

import json
import random
from dataclasses import dataclass
from datetime import timedelta
from typing import Any, Mapping

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.logging import get_logger
from app.core.time import ensure_shanghai_datetime, now_shanghai
from app.models.plugin_business import GroupTriggerRule
from app.services.plugin_runtime.replies import PluginReplyError
from app.services.trigger_rules import media
from app.services.trigger_rules.catalog import (
    BuildContext,
    EventView,
    RuleField,
    get_action,
    get_trigger,
    unknown_placeholders,
    variables_of,
)

logger = get_logger(__name__)

MAX_RULES_PER_GROUP = 50
MAX_ACTIONS_PER_RULE = 6
MAX_FIELD_LENGTH = 1000
PICK_MODES = (("all", "全部依次发送"), ("random", "每次随机发一条"))


class RuleError(ValueError):
    """规则填写不完整或不合法;消息可直接展示给用户。"""


@dataclass
class RuleDraft:
    """用户提交、尚未保存的规则。actions 里每项是 {"type": 动作类型, <字段名>: 值}。"""

    name: str
    trigger_type: str
    trigger_params: dict[str, Any]
    actions: list[dict[str, Any]]
    pick_mode: str = "all"
    cooldown_seconds: int = 0
    priority: int = 100
    is_enabled: bool = True


def _check_field(item: RuleField, value: Any, *, tenant_id: int, owner: str) -> str:
    """校验并规整一个填写项,返回要保存的字符串。"""

    text = str(value or "").strip()
    if not text:
        if item.required:
            raise RuleError(f"{owner}:请填写「{item.label}」")
        return ""
    if len(text) > MAX_FIELD_LENGTH:
        raise RuleError(f"{owner}:「{item.label}」太长了")
    if item.kind == "url" and not text.lower().startswith(("http://", "https://")):
        raise RuleError(f"{owner}:「{item.label}」必须以 http:// 或 https:// 开头")
    if item.kind == "select" and text not in {key for key, _ in item.options}:
        raise RuleError(f"{owner}:「{item.label}」的选项不正确")
    if item.kind == "file":
        try:
            media.resolve(tenant_id, text, item.media_kind)
        except media.MediaError as exc:
            raise RuleError(f"{owner}:「{item.label}」{exc}") from exc
    return text


def validate_draft(draft: RuleDraft, *, tenant_id: int) -> RuleDraft:
    """校验整条规则并返回规整后的副本;任何问题都抛 RuleError(中文提示)。"""

    name = str(draft.name or "").strip()
    if not name:
        raise RuleError("请给这条规则起个名字")
    if len(name) > 100:
        raise RuleError("规则名字太长了")
    trigger = get_trigger(draft.trigger_type)
    if trigger is None:
        raise RuleError("请选择触发条件")
    params = {item.name: _check_field(item, draft.trigger_params.get(item.name, item.default), tenant_id=tenant_id, owner="触发条件") for item in trigger.fields}
    if not draft.actions:
        raise RuleError("请至少添加一条回复内容")
    if len(draft.actions) > MAX_ACTIONS_PER_RULE:
        raise RuleError(f"一条规则最多 {MAX_ACTIONS_PER_RULE} 条回复")
    actions: list[dict[str, Any]] = []
    for index, raw in enumerate(draft.actions, start=1):
        action = get_action(str(raw.get("type") or ""))
        if action is None:
            raise RuleError(f"第 {index} 条回复:请选择回复类型")
        owner = f"第 {index} 条回复({action.label})"
        item = {"type": action.key}
        for field_def in action.fields:
            item[field_def.name] = _check_field(field_def, raw.get(field_def.name), tenant_id=tenant_id, owner=owner)
            if field_def.variables:
                unknown = unknown_placeholders(item[field_def.name], trigger)
                if unknown:
                    usable = "、".join("{" + v.name + "}" for v in variables_of(trigger))
                    raise RuleError(f"{owner}:「{field_def.label}」里的 {{{unknown[0]}}} 在「{trigger.label}」下不能用,可用的变量有 {usable}")
        actions.append(item)
    if draft.pick_mode not in {key for key, _ in PICK_MODES}:
        raise RuleError("请选择回复方式")
    return RuleDraft(
        name=name,
        trigger_type=trigger.key,
        trigger_params=params,
        actions=actions,
        pick_mode=draft.pick_mode,
        cooldown_seconds=max(0, min(int(draft.cooldown_seconds or 0), 86_400)),
        priority=max(1, min(int(draft.priority or 100), 9_999)),
        is_enabled=bool(draft.is_enabled),
    )


def media_names(actions: list[Mapping[str, Any]]) -> set[str]:
    """一组动作里引用的全部素材文件名。"""

    names: set[str] = set()
    for item in actions:
        action = get_action(str(item.get("type") or ""))
        if action is None:
            continue
        names.update(str(item.get(f.name) or "") for f in action.fields if f.kind == "file" and item.get(f.name))
    return names


def rule_actions(rule: GroupTriggerRule) -> list[dict[str, Any]]:
    try:
        value = json.loads(rule.actions_json or "[]")
    except ValueError:
        return []
    return [item for item in value if isinstance(item, dict)] if isinstance(value, list) else []


def rule_params(rule: GroupTriggerRule) -> dict[str, Any]:
    try:
        value = json.loads(rule.trigger_json or "{}")
    except ValueError:
        return {}
    return value if isinstance(value, dict) else {}


def list_rules(db: Session, chatroom_id: int) -> list[GroupTriggerRule]:
    return list(db.scalars(select(GroupTriggerRule).where(GroupTriggerRule.chatroom_id == chatroom_id).order_by(GroupTriggerRule.priority.asc(), GroupTriggerRule.id.asc())).all())


def get_rule(db: Session, chatroom_id: int, rule_id: int) -> GroupTriggerRule | None:
    rule = db.get(GroupTriggerRule, rule_id)
    return rule if rule is not None and rule.chatroom_id == chatroom_id else None


def _apply(rule: GroupTriggerRule, draft: RuleDraft) -> None:
    rule.name = draft.name
    rule.trigger_type = draft.trigger_type
    rule.trigger_json = json.dumps(draft.trigger_params, ensure_ascii=False)
    rule.actions_json = json.dumps(draft.actions, ensure_ascii=False)
    rule.pick_mode = draft.pick_mode
    rule.cooldown_seconds = draft.cooldown_seconds
    rule.priority = draft.priority
    rule.is_enabled = draft.is_enabled


def create_rule(db: Session, *, tenant_id: int, chatroom_id: int, draft: RuleDraft) -> GroupTriggerRule:
    if len(list_rules(db, chatroom_id)) >= MAX_RULES_PER_GROUP:
        raise RuleError(f"每个群最多 {MAX_RULES_PER_GROUP} 条规则")
    draft = validate_draft(draft, tenant_id=tenant_id)
    rule = GroupTriggerRule(tenant_id=tenant_id, chatroom_id=chatroom_id)
    _apply(rule, draft)
    db.add(rule)
    db.flush()
    return rule


def update_rule(db: Session, rule: GroupTriggerRule, draft: RuleDraft) -> None:
    """保存修改,并清理这次修改后不再被引用的旧素材文件。"""

    draft = validate_draft(draft, tenant_id=int(rule.tenant_id))
    before = media_names(rule_actions(rule))
    _apply(rule, draft)
    db.flush()
    for name in before - media_names(draft.actions):
        media.delete(int(rule.tenant_id), name)


def delete_rule(db: Session, rule: GroupTriggerRule) -> None:
    for name in media_names(rule_actions(rule)):
        media.delete(int(rule.tenant_id), name)
    db.delete(rule)
    db.flush()


def rule_summary(rule: GroupTriggerRule) -> dict[str, Any]:
    """列表页展示用:把规则翻译成「当 … 时,回复 …」的人话。"""

    trigger = get_trigger(rule.trigger_type)
    actions = [get_action(str(item.get("type") or "")) for item in rule_actions(rule)]
    return {
        "id": int(rule.id),
        "name": rule.name,
        "enabled": bool(rule.is_enabled),
        "trigger": trigger.summarize(rule_params(rule)) if trigger else "触发条件已不存在",
        "actions": [action.summarize(item) if action else "未知回复" for action, item in zip(actions, rule_actions(rule))],
        "mode": dict(PICK_MODES).get(rule.pick_mode, ""),
        "cooldown": int(rule.cooldown_seconds or 0),
    }


def execute_rules(
    db: Session,
    *,
    tenant_id: int,
    chatroom_id: int,
    view: EventView,
) -> list[dict[str, Any]] | None:
    """用一个入站事件匹配本群规则,返回要发送的标准回复。

    按优先级从小到大,命中的第一条规则生效(同一件事不会触发多条规则重复回复)。
    返回 None 表示没有任何规则命中,[] 表示命中但这次不回复(冷却中或动作全部失败)。
    某个动作构造失败(素材丢失等)只跳过那一个动作并记日志,不影响其他动作。
    """

    now = now_shanghai()
    for rule in list_rules(db, chatroom_id):
        if not rule.is_enabled:
            continue
        trigger = get_trigger(rule.trigger_type)
        if trigger is None or view.message_type not in trigger.event_types:
            continue
        variables = trigger.match(rule_params(rule), view)
        if variables is None:
            continue
        cooldown = int(rule.cooldown_seconds or 0)
        if cooldown and rule.last_triggered_at is not None and now - ensure_shanghai_datetime(rule.last_triggered_at) < timedelta(seconds=cooldown):
            return []
        # 声明了但这次事件没取到的变量按空字符串处理,不会把 {xxx} 原样发到群里
        context = BuildContext(
            tenant_id=tenant_id,
            variables={
                **{item.name: "" for item in trigger.variables},
                **variables,
                "group": view.group_name,
                "time": now.strftime("%Y-%m-%d %H:%M"),
            },
        )
        actions = rule_actions(rule)
        if rule.pick_mode == "random" and actions:
            actions = [random.choice(actions)]
        replies: list[dict[str, Any]] = []
        for item in actions:
            action = get_action(str(item.get("type") or ""))
            if action is None:
                continue
            try:
                replies.append(action.build(item, context))
            except (PluginReplyError, media.MediaError, ValueError) as exc:
                logger.warning("触发规则动作构造失败", extra={"rule_id": rule.id, "action": action.key, "error": str(exc)})
        if replies:
            rule.last_triggered_at = now
            db.flush()
        return replies
    return None
