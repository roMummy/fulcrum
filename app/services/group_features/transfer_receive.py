"""自动收款:群里有人转账给机器人账号时,自动确认收款(开关在群详情「群功能」里,按群设置)。

协议(微桥 8080 文档第 4.3.5、12 节):
- 转账事件 transfer_received 带 transfer_id、message_id、付款方/收款方等;事件的 status="received" 只表示收到转账消息,不代表已收款。
- 确认收款命令 receive_transfer 的参数是 transfer_id 与 message_id(至少一个非空,文档建议都传)。
- 命令会等约 5 秒的原生回调:status="callback_ok" 且 confirmed=true 才算确认收到;
  "confirm_invoked"(等待超时)与 "callback_unknown" 都是"结果未知",文档明确要求不要直接重试,应改用 get_transfer_status 查询。
  所以这里只发一次命令,不做自动重试;结果未确认时记警告,请人工核对。
- 范围:收款方是当前账号的个人转账(含普通群);群收款/AA、企业互通群等不支持。

只处理群里的转账(群功能只响应群消息);私聊里收到的转账不会自动收款。
结果由 wechat_webridge.service 记日志并回填收款记录(payment_ledger)。
"""

from __future__ import annotations

from typing import Any

from sqlalchemy.exc import IntegrityError

from app.core.logging import get_logger
from app.services import payment_ledger
from app.services.group_features.dispatch import FeatureContext, register_handler
from app.services.group_features.registry import GroupFeature, register
from app.services.wechat_webridge import service as webridge_service
from app.services.wechat_webridge import wakeup

logger = get_logger(__name__)

CHANNEL_TYPE = "wechat_webridge"
COMMAND_NAME = "receive_transfer"
# 和领取红包一样比普通出站(90)优先。
PRIORITY = 150

FEATURE = register(
    GroupFeature(
        key="transfer_receive",
        name="自动收款",
        description="本群有人转账给机器人账号时自动确认收款，钱进入这个微信的零钱。每笔只确认一次；超时未确认时不会自动重试，请到日志里核对。只处理群里的转账，私聊里的转账不会自动收。",
        event_types=("transfer",),
        channel_types=(CHANNEL_TYPE,),
    )
)


def _text(value: Any) -> str:
    return str(value).strip() if value not in (None, "") else ""


def handle(ctx: FeatureContext, args: str) -> list[dict[str, Any]] | None:
    """收到转账事件:把确认收款命令入队。不是转账事件(或没有转账 ID)返回 None(与本能力无关)。"""

    if ctx.channel.channel_type != CHANNEL_TYPE or _text(ctx.message.get("message_type")) != "transfer":
        return None
    payment = ctx.details.get("payment") if isinstance(ctx.details.get("payment"), dict) else {}
    transfer_id = _text(payment.get("transfer_id"))
    message_id = _text(payment.get("message_id"))
    if not transfer_id and not message_id:
        return None

    # 自己付出的转账也可能产生事件("本机发出的消息也可能入队"),收款方不是自己,领取只会得到 NOT_RECEIVER,直接跳过。
    runtime = webridge_service._runtime(ctx.channel).get("webridge")
    own = _text((runtime or {}).get("account_id")) if isinstance(runtime, dict) else ""
    if own and own in {_text(ctx.message.get("sender_id")), _text(payment.get("from_user")), _text(payment.get("payer_username"))}:
        logger.info("own transfer skipped", extra={"channel_id": ctx.channel.id, "transfer_id": transfer_id})
        return []

    # 先入库(待处理):记下付款人、群和金额(转账事件自带金额)。同一笔转账重复投递时返回 None,不再重复处理。
    receipt = payment_ledger.record_incoming(
        ctx.db, channel_id=int(ctx.channel.id), kind=payment_ledger.KIND_TRANSFER, external_id=transfer_id or message_id, chat_type="group",
        chat_external_id=str(ctx.conversation.external_id), chat_title=str(ctx.chatroom.remark or ctx.chatroom.name or ctx.conversation.title or ""),
        sender_id=_text(ctx.message.get("sender_id")), sender_name=_text(ctx.message.get("sender_name")),
        amount_fen=payment_ledger.extract_amount_fen(payment),
    )
    if receipt is None:
        logger.info("transfer already recorded", extra={"channel_id": ctx.channel.id, "transfer_id": transfer_id})
        return []

    args_map = {key: value for key, value in (("transfer_id", transfer_id), ("message_id", message_id)) if value}
    try:
        # 同一笔转账只收一次:request_id 由转账 ID 决定,任务表对 (租户, 去重键) 有唯一约束。
        with ctx.db.begin_nested():
            job = webridge_service.enqueue_command(
                ctx.db, ctx.channel, command_name=COMMAND_NAME, args=args_map,
                request_id=f"transfer_{transfer_id or message_id}", priority=PRIORITY,
            )
    except IntegrityError:
        logger.info("transfer receive already queued", extra={"channel_id": ctx.channel.id, "transfer_id": transfer_id})
        return []
    # 必须先提交再叫醒手机轮询,否则被叫醒的轮询查不到这条命令。
    ctx.db.commit()
    wakeup.notify(int(ctx.channel.id))
    logger.info(
        "transfer receive queued",
        extra={
            "channel_id": ctx.channel.id, "chatroom_id": ctx.chatroom.id, "job_id": int(job.id), "transfer_id": transfer_id,
            "amount_text": _text(payment.get("amount_text")),
        },
    )
    return []


register_handler(FEATURE, handle)
