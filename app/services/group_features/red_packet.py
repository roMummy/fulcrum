"""自动抢红包:群里出现红包时,等待设定的延迟(毫秒)后,让手机领取。

协议(微桥 8080 文档第 12 节):红包事件 red_packet_received 带 red_packet_id;
领取命令 receive_red_packet 只有这一个参数,同一个 red_packet_id 也用来去重。

时间怎么保证:
- 数据库里任务的 available_at 只有秒级精度(datetime,毫秒会被截断),不能用来控制毫秒延迟;
  所以这里用进程内定时器:到点时才把领取命令入队,并立刻唤醒挂起中的手机轮询(见 wechat_webridge.wakeup)。
- 实际领取时刻 = 事件到达后的调度等待(后台 worker 空闲时每 0.5 秒看一次队列,通常 0~0.5 秒)+ 设定的延迟 + 手机执行命令的时间。
- 延迟期间服务重启会丢掉这个还没入队的领取(几秒内的窗口,红包本身不受影响,只是这次没抢);为此不做持久化的复杂方案。

领取结果是业务结果(领到多少、已被领完、已过期),不是任务故障,由 wechat_webridge.service 记日志并记为完成。
领取命令的回报只说明"已打开",没有金额:领取成功后这里再安排几次红包详情查询(schedule_detail),
拿到我领到的金额(或确认已被领完)后回填收款记录。
"""

from __future__ import annotations

import threading
from typing import Any, Callable

from sqlalchemy.exc import IntegrityError

from app.core.db import TenantSessionLocal
from app.core.logging import get_logger
from app.core.tenant_context import tenant_scope
from app.core.tenant_db import tenant_session
from app.models.entities import Channel
from app.services import payment_ledger
from app.services.group_features.dispatch import FeatureContext, register_handler
from app.services.group_features.registry import ConfigField, GroupFeature, register
from app.services.wechat_webridge import service as webridge_service
from app.services.wechat_webridge import wakeup

logger = get_logger(__name__)

CHANNEL_TYPE = "wechat_webridge"
COMMAND_NAME = "receive_red_packet"
# 领取命令比普通出站(90)优先:手机一次轮询领到好几条命令时先抢红包。
CLAIM_PRIORITY = 150
DEFAULT_DELAY_MS = 1000
MAX_DELAY_MS = 60_000

FEATURE = register(
    GroupFeature(
        key="red_packet",
        name="自动抢红包",
        description="群里出现红包时,等待设定的延迟后自动领取。领取用的是机器人这个微信账号,领到的钱进入该账号的零钱。",
        event_types=("red_packet",),
        channel_types=(CHANNEL_TYPE,),
        fields=(
            ConfigField(
                "delay_ms", "领取延迟(毫秒)", kind="number", default=DEFAULT_DELAY_MS, minimum=0, maximum=MAX_DELAY_MS, group="领取设置",
                hint="收到红包后等待这么久再领取;0 表示尽快。实际时刻还会加上系统调度和手机执行的时间(通常不到 1 秒)",
            ),
        ),
    )
)


def _schedule(delay_seconds: float, func: Callable[..., None], *args: Any) -> threading.Timer:
    """在 delay_seconds 之后于独立线程里执行 func;守护线程,不阻塞调用方也不阻止进程退出。"""

    timer = threading.Timer(max(float(delay_seconds), 0.0), func, args=args)
    timer.daemon = True
    timer.start()
    return timer


def _is_own_packet(channel: Channel, sender_id: str) -> bool:
    """机器人账号自己发的红包不能自己领,跳过(否则只会得到一次必然失败的领取)。"""

    runtime = webridge_service._runtime(channel).get("webridge")
    own = str((runtime or {}).get("account_id") or "") if isinstance(runtime, dict) else ""
    return bool(own) and own == sender_id


def _enqueue(tenant_id: int, channel_id: int, command_name: str, args: dict[str, Any], request_id: str, priority: int) -> int | None:
    """把一条命令入队并唤醒手机轮询;同一个 request_id 已经入过队返回 None。在定时器线程里运行,自己开租户会话。"""

    with tenant_scope(tenant_id), tenant_session(TenantSessionLocal) as db:
        channel = db.get(Channel, int(channel_id))
        if channel is None or channel.deleted_at is not None:
            return None
        try:
            # request_id 决定去重键,任务表对 (租户, 去重键) 有唯一约束:同一个命令只入一次。
            with db.begin_nested():
                job = webridge_service.enqueue_command(db, channel, command_name=command_name, args=args, request_id=request_id, priority=priority)
        except IntegrityError:
            return None
        db.commit()
        job_id = int(job.id)
    # 命令已提交,叫醒挂起中的手机轮询,不必等下一次 1 秒检查。
    wakeup.notify(channel_id)
    return job_id


def claim(tenant_id: int, channel_id: int, conversation_id: int, red_packet_id: str) -> None:
    """到点后把领取命令入队。出错只记日志。"""

    try:
        job_id = _enqueue(tenant_id, channel_id, COMMAND_NAME, {"red_packet_id": red_packet_id}, f"redpacket_{red_packet_id}", CLAIM_PRIORITY)
        if job_id is None:
            logger.info("red packet claim not queued", extra={"channel_id": channel_id, "red_packet_id": red_packet_id})
            return
        logger.info("red packet claim queued", extra={"channel_id": channel_id, "conversation_id": conversation_id, "red_packet_id": red_packet_id, "job_id": job_id})
    except Exception:
        logger.exception("red packet claim enqueue failed", extra={"channel_id": channel_id, "red_packet_id": red_packet_id})


# 领取之后查红包详情的次数与等待时间(秒):手机端"打开红包"是异步的,刚打开时详情里我的金额可能还没有。
DETAIL_COMMAND = "red_packet_detail"
DETAIL_DELAYS = (2.0, 4.0, 8.0)
MAX_DETAIL_ATTEMPTS = len(DETAIL_DELAYS)
DETAIL_PRIORITY = 120


def detail_request_id(red_packet_id: str, attempt: int) -> str:
    return f"redpacket_detail_{red_packet_id}_{attempt}"


def schedule_detail(tenant_id: int, channel_id: int, red_packet_id: str, attempt: int) -> None:
    """安排第 attempt 次(从 1 开始)查询红包详情:到点才入队,查到金额就不再继续(见 payment_ledger.apply_red_packet_detail)。"""

    if attempt < 1 or attempt > MAX_DETAIL_ATTEMPTS:
        return
    _schedule(DETAIL_DELAYS[attempt - 1], _enqueue_detail, tenant_id, channel_id, red_packet_id, attempt)


def _enqueue_detail(tenant_id: int, channel_id: int, red_packet_id: str, attempt: int) -> None:
    try:
        job_id = _enqueue(tenant_id, channel_id, DETAIL_COMMAND, {"red_packet_id": red_packet_id}, detail_request_id(red_packet_id, attempt), DETAIL_PRIORITY)
        if job_id is not None:
            logger.info("red packet detail queued", extra={"channel_id": channel_id, "red_packet_id": red_packet_id, "attempt": attempt, "job_id": job_id})
    except Exception:
        logger.exception("red packet detail enqueue failed", extra={"channel_id": channel_id, "red_packet_id": red_packet_id, "attempt": attempt})


def handle(ctx: FeatureContext, args: str) -> list[dict[str, Any]] | None:
    """收到红包事件:按群里设置的延迟安排领取。不是红包事件返回 None(与本能力无关)。"""

    if ctx.channel.channel_type != CHANNEL_TYPE or str(ctx.message.get("message_type") or "") != "red_packet":
        return None
    payment = ctx.details.get("payment")
    red_packet_id = str((payment or {}).get("red_packet_id") or "").strip() if isinstance(payment, dict) else ""
    if not red_packet_id:
        return None
    if _is_own_packet(ctx.channel, str(ctx.message.get("sender_id") or "")):
        logger.info("own red packet skipped", extra={"channel_id": ctx.channel.id, "red_packet_id": red_packet_id})
        return []
    # 先入库(待处理):记下来源和群,同一个红包重复投递时这里返回 None,不再重复安排领取。
    receipt = payment_ledger.record_incoming(
        ctx.db, channel_id=int(ctx.channel.id), kind=payment_ledger.KIND_RED_PACKET, external_id=red_packet_id, chat_type="group",
        chat_external_id=str(ctx.conversation.external_id), chat_title=str(ctx.chatroom.remark or ctx.chatroom.name or ctx.conversation.title or ""),
        sender_id=str(ctx.message.get("sender_id") or ""), sender_name=str(ctx.message.get("sender_name") or ""),
    )
    if receipt is None:
        return []
    try:
        delay_ms = max(0, min(int(ctx.config.get("delay_ms", DEFAULT_DELAY_MS)), MAX_DELAY_MS))
    except (TypeError, ValueError):
        delay_ms = DEFAULT_DELAY_MS
    _schedule(delay_ms / 1000, claim, int(ctx.channel.tenant_id), int(ctx.channel.id), int(ctx.conversation.id), red_packet_id)
    logger.info(
        "red packet claim scheduled",
        extra={"channel_id": ctx.channel.id, "chatroom_id": ctx.chatroom.id, "red_packet_id": red_packet_id, "delay_ms": delay_ms},
    )
    return []


register_handler(FEATURE, handle)
