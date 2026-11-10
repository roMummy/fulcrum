"""自动收款:机器人账号收到转账时,自动确认收款(开关在账号详情页,账号级,私聊和群里的转账都适用)。

和自动抢红包不同,转账多发生在私聊,不属于某个群,所以开关放在账号上而不是群功能里;
设置存在渠道的 adapter_config_json 里(键 auto_receive_transfer),不需要改库表。

协议(微桥 8080 文档第 4.3.5、12 节):
- 转账事件 transfer_received 带 transfer_id、message_id、付款方/收款方等;事件的 status="received" 只表示收到转账消息,不代表已收款。
- 确认收款命令 receive_transfer 的参数是 transfer_id 与 message_id(至少一个非空,文档建议都传)。
- 命令会等约 5 秒的原生回调:status="callback_ok" 且 confirmed=true 才算确认收到;
  "confirm_invoked"(等待超时)与 "callback_unknown" 都是"结果未知",文档明确要求不要直接重试,应改用 get_transfer_status 查询。
  所以这里只发一次命令,不做自动重试;结果未确认时记警告,请人工核对。
- 范围:收款方是当前账号的个人转账(含普通群);群收款/AA、企业互通群等不支持。

处理在后台调度任务里进行(和群功能同一个位置),发现转账就入队命令并立刻唤醒手机轮询。
"""

from __future__ import annotations

import json
from typing import Any, Mapping

from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.core.logging import get_logger
from app.models.entities import Channel
from app.services.wechat_webridge import service, wakeup

logger = get_logger(__name__)

SETTING_KEY = "auto_receive_transfer"
COMMAND_NAME = "receive_transfer"
# 和领取红包一样比普通出站(90)优先。
PRIORITY = 150


def _load_config(channel: Channel) -> dict[str, Any]:
    try:
        value = json.loads(channel.adapter_config_json or "{}")
    except (TypeError, ValueError):
        return {}
    return value if isinstance(value, dict) else {}


def is_enabled(channel: Channel) -> bool:
    return bool(_load_config(channel).get(SETTING_KEY))


def set_enabled(channel: Channel, enabled: bool) -> None:
    """保存开关;保留适配器配置里的其他键(如 upload_url)。调用方负责提交。"""

    config = _load_config(channel)
    config[SETTING_KEY] = bool(enabled)
    channel.adapter_config_json = json.dumps(config, ensure_ascii=False, sort_keys=True)


def _text(value: Any) -> str:
    return str(value).strip() if value not in (None, "") else ""


def handle_event(db: Session, channel: Channel, normalized_event: Mapping[str, Any], *, conversation_id: int | None = None) -> bool:
    """处理一条入站事件:是发给本账号的转账、且开关已打开,就把确认收款命令入队。返回是否入队。

    不抛异常的约定由调用方保证(见 async_workers):这里出错不能影响后面的插件调度。
    """

    if channel.channel_type != "wechat_webridge" or not is_enabled(channel):
        return False
    message = normalized_event.get("message") if isinstance(normalized_event.get("message"), Mapping) else {}
    if str(message.get("message_type") or "") != "transfer":
        return False
    details = normalized_event.get("details") if isinstance(normalized_event.get("details"), Mapping) else {}
    payment = details.get("payment") if isinstance(details.get("payment"), Mapping) else {}
    transfer_id = _text(payment.get("transfer_id"))
    message_id = _text(payment.get("message_id"))
    if not transfer_id and not message_id:
        return False

    # 自己付出的转账也可能产生事件("本机发出的消息也可能入队"),收款方不是自己,领取只会得到 NOT_RECEIVER,直接跳过。
    runtime = service._runtime(channel).get("webridge")
    own = _text((runtime or {}).get("account_id")) if isinstance(runtime, dict) else ""
    if own and own in {_text(message.get("sender_id")), _text(payment.get("from_user")), _text(payment.get("payer_username"))}:
        logger.info("own transfer skipped", extra={"channel_id": channel.id, "transfer_id": transfer_id})
        return False

    args = {key: value for key, value in (("transfer_id", transfer_id), ("message_id", message_id)) if value}
    try:
        # 同一笔转账只收一次:request_id 由转账 ID 决定,任务表对 (租户, 去重键) 有唯一约束。
        with db.begin_nested():
            job = service.enqueue_command(
                db, channel, command_name=COMMAND_NAME, args=args,
                request_id=f"transfer_{transfer_id or message_id}", priority=PRIORITY,
            )
    except IntegrityError:
        logger.info("transfer receive already queued", extra={"channel_id": channel.id, "transfer_id": transfer_id})
        return False
    # 必须先提交再叫醒手机轮询,否则被叫醒的轮询查不到这条命令。
    db.commit()
    wakeup.notify(int(channel.id))
    logger.info(
        "transfer receive queued",
        extra={
            "channel_id": channel.id, "conversation_id": conversation_id or 0, "job_id": int(job.id), "transfer_id": transfer_id,
            "amount_text": _text(payment.get("amount_text")),
        },
    )
    return True
