"""插件的平台管控:全平台停用、可见范围(套餐/租户/仅平台)、使用情况统计。

插件目录是全平台共享的,租户只管理自己机器人上的绑定。平台方需要的管控放在这里:

- 全平台停用:插件出问题时一键止损。已绑定的机器人不再执行它,绑定、配置、加密密钥和状态数据全部保留,恢复后原样继续;
- 可见范围:所有租户 / 只有指定套餐 / 只有指定租户 / 仅平台可见(内部或测试中的插件,比如不对外开放的私有插件)。
  租户看不到、不能绑定,已有绑定也不会执行;
- 使用情况:每个插件被多少租户、多少机器人使用,平台后台据此判断停用或调整的影响面。

判定规则宁可拦也不放:状态不是 active、可见范围不认识、套餐或租户名单里没有,都按"不可用"处理。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Iterable

from sqlalchemy import case, distinct, func, or_, select
from sqlalchemy.orm import Session

from app.models.entities import Bot, BotPlugin, Plugin, PluginGrant
from app.models.saas import PlatformAuditLog, SaasPlan, Tenant

STATUS_ACTIVE = "active"
STATUS_DISABLED = "disabled"
STATUSES = (STATUS_ACTIVE, STATUS_DISABLED)

VISIBILITY_ALL = "all"
VISIBILITY_PLANS = "plans"
VISIBILITY_TENANTS = "tenants"
VISIBILITY_PLATFORM_ONLY = "platform_only"
VISIBILITIES = (VISIBILITY_ALL, VISIBILITY_PLANS, VISIBILITY_TENANTS, VISIBILITY_PLATFORM_ONLY)

VISIBILITY_LABELS = {
    VISIBILITY_ALL: "所有租户",
    VISIBILITY_PLANS: "指定套餐",
    VISIBILITY_TENANTS: "指定租户",
    VISIBILITY_PLATFORM_ONLY: "仅平台可见",
}

SCOPE_PLAN = "plan"
SCOPE_TENANT = "tenant"


class PluginGovernanceError(ValueError):
    """平台对插件的设置不合法(未知状态、没选套餐/租户等)。"""


def is_available(
    *,
    platform_status: str,
    visibility: str,
    grants: Iterable[tuple[str, int]],
    tenant_id: int,
    plan_id: int | None,
) -> bool:
    """这个租户能不能使用这个插件。不认识的状态或可见范围一律按不可用处理。"""

    if platform_status != STATUS_ACTIVE:
        return False
    if visibility == VISIBILITY_ALL:
        return True
    granted = set(grants)
    if visibility == VISIBILITY_PLANS:
        return plan_id is not None and (SCOPE_PLAN, int(plan_id)) in granted
    if visibility == VISIBILITY_TENANTS:
        return (SCOPE_TENANT, int(tenant_id)) in granted
    return False


def _current_plan_id(db: Session, tenant_id: int) -> int | None:
    from app.services.billing import get_current_subscription

    subscription = get_current_subscription(db, tenant_id=int(tenant_id))
    return int(subscription.plan_id) if subscription is not None and subscription.plan_id else None


def blocked_plugin_codes(db: Session, tenant_id: int) -> frozenset[str]:
    """这个租户当前不能使用的插件编码:被平台停用的、对它不可见的。

    绝大多数情况下没有任何受限插件,此时只有一次很轻的查询;租户的套餐只有存在"指定套餐"的插件时才会去查。
    """

    restricted = db.execute(
        select(Plugin.id, Plugin.code, Plugin.platform_status, Plugin.visibility).where(
            or_(Plugin.platform_status != STATUS_ACTIVE, Plugin.visibility != VISIBILITY_ALL)
        )
    ).all()
    if not restricted:
        return frozenset()
    grants: dict[int, set[tuple[str, int]]] = {}
    for plugin_id, scope_type, scope_id in db.execute(
        select(PluginGrant.plugin_id, PluginGrant.scope_type, PluginGrant.scope_id).where(PluginGrant.plugin_id.in_([row.id for row in restricted]))
    ).all():
        grants.setdefault(int(plugin_id), set()).add((str(scope_type), int(scope_id)))
    plan_id = _current_plan_id(db, tenant_id) if any(row.visibility == VISIBILITY_PLANS for row in restricted) else None
    return frozenset(
        row.code
        for row in restricted
        if not is_available(
            platform_status=row.platform_status,
            visibility=row.visibility,
            grants=grants.get(int(row.id), ()),
            tenant_id=int(tenant_id),
            plan_id=plan_id,
        )
    )


def tenant_visible_plugins(db: Session, tenant_id: int) -> list[Plugin]:
    """租户页面能看到的插件:目录里还在的、没被平台停用、对这个租户可见的。"""

    blocked = blocked_plugin_codes(db, tenant_id)
    plugins = db.scalars(select(Plugin).where(Plugin.missing_at.is_(None)).order_by(Plugin.id.desc())).all()
    return [plugin for plugin in plugins if plugin.code not in blocked]


@dataclass(frozen=True)
class PluginUsage:
    tenants: int = 0
    bindings: int = 0
    enabled: int = 0


def usage_summary(db: Session) -> dict[int, PluginUsage]:
    """每个插件被多少租户、多少机器人使用(不含已删除的机器人)。需要平台会话,跨租户统计。"""

    rows = db.execute(
        select(
            BotPlugin.plugin_id,
            func.count(distinct(BotPlugin.tenant_id)),
            func.count(BotPlugin.id),
            func.sum(case((BotPlugin.status == "enabled", 1), else_=0)),
        )
        .where(BotPlugin.status != "unbound", BotPlugin.bot_id.in_(select(Bot.id).where(Bot.deleted_at.is_(None))))
        .group_by(BotPlugin.plugin_id)
    ).all()
    return {int(plugin_id): PluginUsage(int(tenants or 0), int(bindings or 0), int(enabled or 0)) for plugin_id, tenants, bindings, enabled in rows}


def grants_of(db: Session, plugin_id: int) -> tuple[list[int], list[int]]:
    """返回 (套餐 id 列表, 租户 id 列表)。"""

    plans: list[int] = []
    tenants: list[int] = []
    for scope_type, scope_id in db.execute(select(PluginGrant.scope_type, PluginGrant.scope_id).where(PluginGrant.plugin_id == plugin_id)).all():
        (plans if scope_type == SCOPE_PLAN else tenants).append(int(scope_id))
    return sorted(plans), sorted(tenants)


def record_plugin_audit(db: Session, actor_user_id: int | None, action: str, plugin: Plugin, detail: str) -> None:
    db.add(PlatformAuditLog(actor_user_id=actor_user_id, action=action, target_type="plugin", target_id=int(plugin.id), detail=detail[:500]))


def set_platform_status(db: Session, plugin: Plugin, *, status: str, note: str = "", actor_user_id: int | None = None) -> None:
    """对全平台停用或恢复一个插件。只改状态,不动任何租户的绑定数据。调用方负责提交。"""

    if status not in STATUSES:
        raise PluginGovernanceError("未知的插件状态")
    plugin.platform_status = status
    plugin.status_note = (note or "").strip()[:255] or None if status == STATUS_DISABLED else None
    record_plugin_audit(db, actor_user_id, "plugin.status", plugin, f"code={plugin.code};status={status}")


def set_visibility(
    db: Session,
    plugin: Plugin,
    *,
    visibility: str,
    plan_ids: Iterable[int] = (),
    tenant_ids: Iterable[int] = (),
    actor_user_id: int | None = None,
) -> None:
    """设置插件对哪些租户可见。指定套餐/租户时必须至少选一个,并且必须是真实存在的。调用方负责提交。"""

    if visibility not in VISIBILITIES:
        raise PluginGovernanceError("未知的可见范围")
    wanted: set[tuple[str, int]] = set()
    if visibility == VISIBILITY_PLANS:
        ids = {int(item) for item in plan_ids}
        if not ids:
            raise PluginGovernanceError("请至少选择一个套餐")
        found = set(db.scalars(select(SaasPlan.id).where(SaasPlan.id.in_(ids))).all())
        if found != ids:
            raise PluginGovernanceError("选择的套餐不存在")
        wanted = {(SCOPE_PLAN, item) for item in ids}
    elif visibility == VISIBILITY_TENANTS:
        ids = {int(item) for item in tenant_ids}
        if not ids:
            raise PluginGovernanceError("请至少选择一个租户")
        found = set(db.scalars(select(Tenant.id).where(Tenant.id.in_(ids))).all())
        if found != ids:
            raise PluginGovernanceError("选择的租户不存在")
        wanted = {(SCOPE_TENANT, item) for item in ids}
    existing = {(grant.scope_type, int(grant.scope_id)): grant for grant in db.scalars(select(PluginGrant).where(PluginGrant.plugin_id == plugin.id)).all()}
    for key, grant in existing.items():
        if key not in wanted:
            db.delete(grant)
    for scope_type, scope_id in wanted - set(existing):
        db.add(PluginGrant(plugin_id=plugin.id, scope_type=scope_type, scope_id=scope_id))
    plugin.visibility = visibility
    detail = ";".join(f"{scope_type}={scope_id}" for scope_type, scope_id in sorted(wanted))
    record_plugin_audit(db, actor_user_id, "plugin.visibility", plugin, f"code={plugin.code};visibility={visibility}" + (f";{detail}" if detail else ""))
