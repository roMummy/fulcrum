"""试用套餐的统一定义:注册开通和机器人额度共用,不依赖套餐编码。

以前注册只认编码正好是 `trial` 的套餐,平台管理员自己建的免费试用套餐(编码随意)
永远不会被用到,注册页只会提示"免费试用暂不可用"。现在按套餐的实际属性判断:
试用天数大于 0 且月付、年付价格都为 0 的套餐就是试用套餐,编码叫什么都行。
"""

from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models.saas import SaasPlan


def is_trial_plan(plan: SaasPlan) -> bool:
    """试用套餐 = 有试用天数且完全免费。"""

    return plan.trial_days > 0 and plan.monthly_price_cents == 0 and plan.yearly_price_cents == 0


def pick_registration_trial_plan(db: Session) -> SaasPlan | None:
    """挑出新注册企业要开通的试用套餐。

    只在"启用中、机器人数量大于 0"的试用套餐里选;有多个时取最早创建的(id 最小),
    结果稳定、可预期。找不到返回 None,由调用方根据 `has_any_trial_plan` 区分原因。
    """

    plans = db.scalars(select(SaasPlan).order_by(SaasPlan.id)).all()
    for plan in plans:
        if is_trial_plan(plan) and plan.status == "active" and plan.bot_limit > 0:
            return plan
    return None


def trial_plan_problem(db: Session) -> str:
    """没有可用试用套餐时,说明具体差在哪一步(用于报错和提示),按"最接近可用"的那个套餐判断。"""

    trial_plans = [plan for plan in db.scalars(select(SaasPlan).order_by(SaasPlan.id)).all() if is_trial_plan(plan)]
    if not trial_plans:
        return "trial_plan_not_found"  # 一个"试用天数>0 且免费"的套餐都没有
    if not any(plan.status == "active" for plan in trial_plans):
        return "trial_plan_disabled"  # 有,但都被停用了
    return "trial_plan_invalid"  # 有启用的,但机器人数量上限是 0
