#!/usr/bin/env python3
"""使用临时数据库执行双租户 Provider 主流程冒烟，不接触部署环境业务数据。"""

from __future__ import annotations

import asyncio
import io
import json
import logging
import sys
from dataclasses import dataclass
from datetime import timedelta
from pathlib import Path
from tempfile import TemporaryDirectory
from typing import Any, Awaitable

# 允许直接执行 ``python scripts/saas_business_smoke.py``，同时保持当前工作目录
# 继续作为日志和环境文件的解析基准，与其他部署脚本的行为一致。
PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from fastapi import HTTPException, Request
from sqlalchemy import create_engine, func, select
from sqlalchemy.orm import sessionmaker

import app.api.workwechat_provider_cloud as provider_api
from app.core.db import Base
from app.core.tenant_context import tenant_scope
from app.core.tenant_db import platform_session, tenant_session
from app.core.time import now_shanghai
from app.models.entities import Bot, Channel, Chatroom, Conversation, Message, TaskJob, WorkWechatContact
from app.models.saas import (
    PlatformAuditLog,
    ProviderActivationCode,
    ProviderCredential,
    ProviderNodeAccount,
    SaasPlan,
    TenantSubscription,
)
from app.services.platform_admin import create_tenant_with_subscription, save_plan
from app.services.provider_nodes import ProviderNodeLimitReached, create_provider_node, issue_activation_code
from app.services.task_queue import enqueue_job
from app.services.workwechat_cloud_poll import resolve_outbound_job_type


@dataclass(frozen=True)
class TenantRuntime:
    """保存跨进程重建仍需使用的非敏感主键和仅在内存存在的设备密钥。"""

    tenant_id: int
    node_id: int
    account_id: int
    bot_id: int
    channel_id: int
    conversation_id: int
    chatroom_id: int
    contact_id: int
    message_id: int
    first_job_id: int
    device_token: str


def _build_request(
    path: str,
    *,
    body: dict[str, Any] | None = None,
    device_token: str = "",
) -> Request:
    """构造最小 ASGI Request，直接调用真实 Provider 路由函数且不依赖测试库。"""

    payload = json.dumps(body or {}, ensure_ascii=False).encode("utf-8")
    headers = [(b"content-type", b"application/json")]
    if device_token:
        headers.append((b"x-provider-device-token", device_token.encode("ascii")))
    delivered = False

    async def receive() -> dict[str, Any]:
        nonlocal delivered
        if delivered:
            return {"type": "http.request", "body": b"", "more_body": False}
        delivered = True
        return {"type": "http.request", "body": payload, "more_body": False}

    return Request(
        {
            "type": "http",
            "http_version": "1.1",
            "method": "POST",
            "scheme": "http",
            "path": path,
            "raw_path": path.encode("ascii"),
            "query_string": b"",
            "root_path": "",
            "headers": headers,
            "client": ("127.0.0.1", 10000),
            "server": ("127.0.0.1", 8280),
        },
        receive,
    )


def _run(awaitable: Awaitable[Any]) -> Any:
    """冒烟脚本本身是同步命令，通过独立事件循环调用异步 FastAPI 路由。"""

    return asyncio.run(awaitable)


def _expect_http_error(awaitable: Awaitable[Any], status_code: int, detail: str) -> None:
    """断言安全边界返回预期 HTTP 错误，避免宽泛捕获掩盖其他异常。"""

    try:
        _run(awaitable)
    except HTTPException as exc:
        assert exc.status_code == status_code, (exc.status_code, exc.detail)
        assert str(exc.detail) == detail, exc.detail
    else:
        raise AssertionError(f"expected HTTP {status_code}: {detail}")


def _activate_node(activation_code: str, label: str) -> str:
    """通过真实激活路由取得设备密钥，并验证同一激活码不能再次消费。"""

    body = {
        "activation_code": activation_code,
        "fingerprint": f"smoke-device-{label}",
        "client_version": "smoke-1.0.0",
    }
    activated = _run(provider_api.activate_provider_device(_build_request("/api/provider/activate", body=body)))
    token = str(activated["device_token"])
    assert token.startswith("pdev_")
    _expect_http_error(
        provider_api.activate_provider_device(_build_request("/api/provider/activate", body=body)),
        401,
        "activation_code_invalid",
    )
    return token


def _heartbeat(device_token: str, label: str) -> dict[str, Any]:
    """上报一个节点内稳定账号，后续渠道绑定只使用云端发现的账号主键。"""

    return _run(
        provider_api.provider_node_heartbeat(
            _build_request(
                "/api/provider/heartbeat",
                device_token=device_token,
                body={
                    "client_version": "smoke-1.0.0",
                    "hook_status": "running",
                    "workwechat_status": "running",
                    "accounts": [
                        {
                            "account_key": "shared-account",
                            "display_name": "同名企微账号",
                            "external_user_id": f"workwechat-{label}",
                            "status": "online",
                        }
                    ],
                },
            )
        )
    )


def _poll(device_token: str, *, batch_size: int = 5) -> dict[str, Any]:
    """调用真实轮询策略，租户到期判断和节点任务领取必须位于同一路径。"""

    return _run(
        provider_api.poll_provider_node_jobs(
            _build_request(
                "/api/provider/poll",
                device_token=device_token,
                body={"batch_size": batch_size, "lease_seconds": 900},
            )
        )
    )


def _complete(device_token: str, job: dict[str, Any]) -> dict[str, Any]:
    """使用路由签发的租约令牌回报任务，调用方可再次调用验证幂等。"""

    return _run(
        provider_api.complete_provider_job(
            int(job["job_id"]),
            _build_request(
                f"/api/provider/jobs/{job['job_id']}/complete",
                device_token=device_token,
                body={
                    "lease_token": job["lease_token"],
                    "adapter_result": {"ok": True, "status": "sent"},
                },
            ),
        )
    )


def _create_tenant_runtime(factory, tenant_id: int, label: str) -> tuple[TenantRuntime, list[str]]:
    """创建节点、激活设备、发现账号并建立一套同名企微业务数据。"""

    with tenant_scope(tenant_id), tenant_session(factory) as db:
        node = create_provider_node(db, tenant_id, name="同名客服电脑")
        activation_code = issue_activation_code(db, node.id).raw_code
        node_id = int(node.id)

    device_token = _activate_node(activation_code, label)
    heartbeat = _heartbeat(device_token, label)
    assert heartbeat["poll_allowed"] is True
    assert int(heartbeat["node_id"]) == node_id

    with tenant_scope(tenant_id), tenant_session(factory) as db:
        account = db.scalar(
            select(ProviderNodeAccount).where(
                ProviderNodeAccount.provider_node_id == node_id,
                ProviderNodeAccount.account_key == "shared-account",
            )
        )
        assert account is not None
        bot = Bot(name="同名机器人")
        db.add(bot)
        db.flush()
        channel = Channel(
            bot_id=bot.id,
            name="同名企业微信渠道",
            channel_type="workwechat",
            adapter_config_json=json.dumps({"workwechat_delivery_mode": "cloud_poll"}),
            provider_node_id=node_id,
            provider_account_id=account.id,
        )
        db.add(channel)
        db.flush()
        conversation = Conversation(
            bot_id=bot.id,
            channel_id=channel.id,
            channel_type="workwechat",
            external_id="shared-room",
            title="同名会话",
            chat_type="group",
        )
        chatroom = Chatroom(
            bot_id=bot.id,
            channel_id=channel.id,
            external_id="shared-room",
            name="同名群聊",
        )
        contact = WorkWechatContact(
            channel_id=channel.id,
            contact_kind="external",
            contact_external_id="shared-contact",
            external_userid="shared-external-user",
            name="同名联系人",
        )
        db.add_all([conversation, chatroom, contact])
        db.flush()
        message = Message(
            conversation_id=conversation.id,
            direction="out",
            sender_id="smoke-user",
            sender_name="同名员工",
            content="双租户隔离测试消息",
            status="pending",
        )
        db.add(message)
        db.flush()
        job = enqueue_job(
            db,
            job_type=resolve_outbound_job_type(channel),
            payload={
                "channel_id": channel.id,
                "conversation_id": conversation.id,
                "payload": {"message_type": "text", "content": "双租户隔离测试消息"},
            },
        )
        db.commit()
        runtime = TenantRuntime(
            tenant_id=tenant_id,
            node_id=node_id,
            account_id=int(account.id),
            bot_id=int(bot.id),
            channel_id=int(channel.id),
            conversation_id=int(conversation.id),
            chatroom_id=int(chatroom.id),
            contact_id=int(contact.id),
            message_id=int(message.id),
            first_job_id=int(job.id),
            device_token=device_token,
        )
    return runtime, [activation_code, device_token]


def _enqueue_followup(factory, runtime: TenantRuntime, label: str) -> int:
    """为既有绑定追加任务，用于到期策略和进程重建恢复验证。"""

    with tenant_scope(runtime.tenant_id), tenant_session(factory) as db:
        channel = db.get(Channel, runtime.channel_id)
        conversation = db.get(Conversation, runtime.conversation_id)
        assert channel is not None and conversation is not None
        job = enqueue_job(
            db,
            job_type=resolve_outbound_job_type(channel),
            payload={
                "channel_id": channel.id,
                "conversation_id": conversation.id,
                "payload": {"message_type": "text", "content": f"进程恢复消息-{label}"},
            },
        )
        db.commit()
        return int(job.id)


def _assert_tenant_visibility(factory, own: TenantRuntime, other: TenantRuntime) -> None:
    """同名资源、外部 ID、统计查询和直接主键读取都只能看到当前租户。"""

    with tenant_scope(own.tenant_id), tenant_session(factory) as db:
        assert db.get(Bot, own.bot_id) is not None
        assert db.get(Bot, other.bot_id) is None
        assert db.get(Channel, other.channel_id) is None
        assert db.get(Conversation, other.conversation_id) is None
        assert db.get(Chatroom, other.chatroom_id) is None
        assert db.get(WorkWechatContact, other.contact_id) is None
        assert db.get(Message, other.message_id) is None
        assert db.scalar(select(func.count(Bot.id))) == 1
        assert db.scalar(select(func.count(Channel.id))) == 1
        assert db.scalar(select(func.count(Conversation.id))) == 1
        assert db.scalar(select(func.count(Chatroom.id))) == 1
        assert db.scalar(select(func.count(WorkWechatContact.id))) == 1
        visible_messages = db.scalars(select(Message).order_by(Message.id.asc())).all()
        # Provider 完成回报会按真实业务逻辑补写结果消息，因此数量可能大于初始
        # 的一条；隔离验收只要求本租户原始消息仍可见且结果集合没有串租户。
        assert any(message.id == own.message_id for message in visible_messages)
        assert all(int(message.tenant_id) == own.tenant_id for message in visible_messages)


def _assert_secrets_not_persisted(factory, secrets: list[str], captured_logs: str) -> None:
    """一次性激活码、设备密钥、租约和初始密码不能进入审计、任务或日志原文。"""

    with platform_session(factory) as db:
        records: list[Any] = []
        for model in (ProviderActivationCode, ProviderCredential, TaskJob, PlatformAuditLog):
            records.extend(db.scalars(select(model)).all())
        persisted = "\n".join(repr(record.__dict__) for record in records)
    for secret in secrets:
        assert secret
        assert secret not in persisted
        assert secret not in captured_logs


def run_business_smoke() -> dict[str, Any]:
    """完成双租户节点主流程、到期只读和进程重建后的租约恢复。"""

    original_session_local = provider_api.SessionLocal
    original_tenant_session_local = provider_api.TenantSessionLocal
    captured_log = io.StringIO()
    log_handler = logging.StreamHandler(captured_log)
    logging.getLogger().addHandler(log_handler)

    with TemporaryDirectory(prefix="fulcrum-smoke-") as temp_dir:
        database_path = Path(temp_dir) / "business-smoke.db"
        engine = create_engine(f"sqlite:///{database_path}")
        factory = sessionmaker(bind=engine, autoflush=False, expire_on_commit=False)
        provider_api.SessionLocal = factory
        provider_api.TenantSessionLocal = factory
        Base.metadata.create_all(bind=engine)
        try:
            with platform_session(factory) as db:
                plan = save_plan(
                    db,
                    code="smoke-plan",
                    name="双租户冒烟套餐",
                    provider_node_limit=1,
                )
                alpha_created = create_tenant_with_subscription(
                    db,
                    code="smoke-alpha",
                    name="冒烟甲租户",
                    plan_id=plan.id,
                    expires_at=now_shanghai() + timedelta(days=30),
                    admin_username="admin",
                    admin_mobile="13100131001",
                    admin_display_name="同名员工",
                )
                beta_created = create_tenant_with_subscription(
                    db,
                    code="smoke-beta",
                    name="冒烟乙租户",
                    plan_id=plan.id,
                    expires_at=now_shanghai() + timedelta(days=30),
                    admin_username="admin",
                    admin_mobile="13100131002",
                    admin_display_name="同名员工",
                )

            alpha, alpha_secrets = _create_tenant_runtime(factory, alpha_created.tenant_id, "alpha")
            beta, beta_secrets = _create_tenant_runtime(factory, beta_created.tenant_id, "beta")

            # 待激活节点立即占用套餐额度，不能通过快速创建第二个节点超额占用。
            with tenant_scope(alpha.tenant_id), tenant_session(factory) as db:
                try:
                    create_provider_node(db, alpha.tenant_id, name="超额节点")
                except ProviderNodeLimitReached:
                    pass
                else:
                    raise AssertionError("provider node quota was not enforced")

            alpha_first = _poll(alpha.device_token)["jobs"]
            beta_first = _poll(beta.device_token)["jobs"]
            assert [int(job["job_id"]) for job in alpha_first] == [alpha.first_job_id]
            assert [int(job["job_id"]) for job in beta_first] == [beta.first_job_id]

            # 即使掌握乙租户任务主键和租约，甲租户设备身份仍只能得到资源不存在。
            _expect_http_error(
                provider_api.complete_provider_job(
                    beta.first_job_id,
                    _build_request(
                        f"/api/provider/jobs/{beta.first_job_id}/complete",
                        device_token=alpha.device_token,
                        body={"lease_token": beta_first[0]["lease_token"], "adapter_result": {"ok": True}},
                    ),
                ),
                404,
                "job_not_found",
            )
            alpha_completed = _complete(alpha.device_token, alpha_first[0])
            alpha_repeated = _complete(alpha.device_token, alpha_first[0])
            beta_completed = _complete(beta.device_token, beta_first[0])
            assert alpha_completed["status"] == "completed"
            assert alpha_repeated["idempotent"] is True
            assert beta_completed["status"] == "completed"

            alpha_followup = _enqueue_followup(factory, alpha, "alpha")
            beta_followup = _enqueue_followup(factory, beta, "beta")
            alpha_running = _poll(alpha.device_token)["jobs"]
            assert [int(job["job_id"]) for job in alpha_running] == [alpha_followup]

            with platform_session(factory) as db:
                beta_subscription = db.scalar(
                    select(TenantSubscription)
                    .where(TenantSubscription.tenant_id == beta.tenant_id)
                    .order_by(TenantSubscription.id.desc())
                )
                assert beta_subscription is not None
                beta_subscription.expires_at = now_shanghai() - timedelta(seconds=1)
                db.commit()
            beta_heartbeat = _heartbeat(beta.device_token, "beta-expired")
            beta_expired_poll = _poll(beta.device_token)
            assert beta_heartbeat["poll_allowed"] is False
            assert beta_expired_poll["read_only"] is True
            assert beta_expired_poll["jobs"] == []

            _assert_tenant_visibility(factory, alpha, beta)
            _assert_tenant_visibility(factory, beta, alpha)

            # 关闭并重新创建 Engine/Session 工厂，模拟云端进程重建；运行中任务不能
            # 再次被领取，原租约仍可完成，已完成任务也不能回到待领取状态。
            engine.dispose()
            engine = create_engine(f"sqlite:///{database_path}")
            factory = sessionmaker(bind=engine, autoflush=False, expire_on_commit=False)
            provider_api.SessionLocal = factory
            provider_api.TenantSessionLocal = factory
            assert _poll(alpha.device_token)["jobs"] == []
            restarted_completion = _complete(alpha.device_token, alpha_running[0])
            assert restarted_completion["status"] == "completed"
            assert _poll(beta.device_token)["jobs"] == []

            with platform_session(factory) as db:
                assert db.get(TaskJob, alpha_followup).status == "completed"
                assert db.get(TaskJob, beta_followup).status == "pending"
                completed_count = int(
                    db.scalar(select(func.count(TaskJob.id)).where(TaskJob.status == "completed")) or 0
                )
                pending_count = int(
                    db.scalar(select(func.count(TaskJob.id)).where(TaskJob.status == "pending")) or 0
                )

            all_secrets = [
                alpha_created.initial_password,
                beta_created.initial_password,
                *alpha_secrets,
                *beta_secrets,
                alpha_first[0]["lease_token"],
                beta_first[0]["lease_token"],
                alpha_running[0]["lease_token"],
            ]
            _assert_secrets_not_persisted(factory, all_secrets, captured_log.getvalue())
            return {
                "tenants": 2,
                "provider_nodes": 2,
                "completed_jobs": completed_count,
                "expired_pending_jobs": pending_count,
                "restart_verified": True,
            }
        finally:
            provider_api.SessionLocal = original_session_local
            provider_api.TenantSessionLocal = original_tenant_session_local
            try:
                Base.metadata.drop_all(bind=engine)
            finally:
                engine.dispose()
                logging.getLogger().removeHandler(log_handler)


def main() -> int:
    """命令行只输出脱敏计数，任何凭证原文都保留在进程内存。"""

    result = run_business_smoke()
    print("saas business smoke passed: " + json.dumps(result, ensure_ascii=False, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
