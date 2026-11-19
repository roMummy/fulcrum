"""群档案、纳管和邀请关系同步入口。"""

from urllib.parse import quote

from fastapi import APIRouter, Form, HTTPException, Request
from fastapi.responses import JSONResponse, RedirectResponse
from sqlalchemy.orm import Session

from app.models.entities import Channel, Chatroom
from app.services.chatroom_invitations import backfill_chatroom_invitations
from app.services.chatroom_registry import set_chatroom_management_status, upsert_manual_chatroom
from app.services.crud import get_channel
from app.services.ops import record_audit
from app.services.task_queue import WORKWECHAT_CLOUD_CONTROL_JOB, enqueue_job
from app.services.wechat_ipad import sync_wechat_ipad_chatrooms
from app.services.wechat_webridge import (
    invitation_sync as webridge_invitation_sync,
    wakeup as webridge_wakeup,
)
from app.services.wechat_webridge.service import enqueue_command
from app.services.wechat_winauto_chatrooms import sync_wechat_winauto_chatrooms
from app.services.workwechat_chatrooms import (
    sync_workwechat_chatroom_members,
    sync_workwechat_chatroom_profile_from_list,
    sync_workwechat_chatrooms,
)
from app.services.workwechat_cloud_poll import is_cloud_poll_enabled, notify_cloud_job_enqueued
from app.web.auth_common import require_tenant_csrf
from app.web.channels.definitions import GROUP_SCOPE_CHANNEL_TYPES
from app.web.shared.access import enforce_role, get_session
from app.web.shared.urls import preserve_return_to
from app.web.shared.operations import (
    _completed_operation_response,
    _csrf_from_header,
    _operation_json_requested,
    _queued_operation_response,
    _require_operation_csrf,
)


router = APIRouter()


def _invitation_sync_target(db: Session, channel_id: int, chatroom_id: int) -> tuple[Channel, Chatroom]:
    """同步和进度共用同一归属校验，不允许将其他渠道或租户的群资料混入当前图谱。"""
    channel = get_channel(db, channel_id)
    if not channel or channel.channel_type != "wechat_webridge":
        raise HTTPException(status_code=404, detail="Channel not found")
    room = db.get(Chatroom, chatroom_id)
    if not room or room.channel_id != channel.id or room.bot_id != channel.bot_id:
        raise HTTPException(status_code=404, detail="Chatroom not found")
    return channel, room


@router.get("/channels/{channel_id}/chatrooms/{chatroom_id}/invitations/sync")
async def chatroom_invitation_sync_status(request: Request, channel_id: int, chatroom_id: int):
    """只读进度供成员页恢复展示；GET 不修改进度，不排队、不重试。"""
    enforce_role(request, "readonly")
    db = get_session()
    try:
        _, room = _invitation_sync_target(db, channel_id, chatroom_id)
        return JSONResponse({"ok": True, **webridge_invitation_sync.view(room)}, headers={"Cache-Control": "no-store"})
    finally:
        db.close()


@router.post("/channels/{channel_id}/chatrooms/{chatroom_id}/invitations/sync")
async def sync_chatroom_invitations_action(request: Request, channel_id: int, chatroom_id: int):
    """运营人员明确操作后才开始批量读取；CSRF、能力检查和群锁在入队之前完成。"""
    enforce_role(request, "ops")
    db = get_session()
    try:
        await _require_operation_csrf(db, request)
        channel, room = _invitation_sync_target(db, channel_id, chatroom_id)
        return_url = f"/channels/{channel_id}/chatrooms/{chatroom_id}?tab=members"
        try:
            result = webridge_invitation_sync.request_sync(db, channel, room)
        except ValueError as exc:
            if _operation_json_requested(request):
                return JSONResponse({"ok": False, "summary": str(exc)}, status_code=400)
            return _completed_operation_response(request, str(exc), return_url, ok=False)
        db.commit()
        webridge_wakeup.notify(int(channel.id))
        record_audit(db, "channel.wechat_webridge.chatroom.invitations.sync", "chatroom", room.id, result["summary"])
        if _operation_json_requested(request):
            return JSONResponse({"ok": True, **result}, headers={"Cache-Control": "no-store"})
        return RedirectResponse(url=preserve_return_to(request, return_url), status_code=303)
    finally:
        db.close()


@router.post("/channels/{channel_id}/chatrooms/{chatroom_id}/invitations/backfill")
async def backfill_chatroom_invitations_action(request: Request, channel_id: int, chatroom_id: int):
    """按用户操作补查本群已有系统消息，所有写入沿用运营权限、CSRF 和租户会话。"""
    enforce_role(request, "ops")
    db = get_session()
    try:
        await _require_operation_csrf(db, request)
        channel = get_channel(db, channel_id)
        if not channel or channel.channel_type not in ("wechat_webridge", "wechat_ipad", "workwechat", "wechat_gugatool"):
            raise HTTPException(status_code=404, detail="Channel not found")
        room = db.get(Chatroom, chatroom_id)
        if not room or room.channel_id != channel.id:
            raise HTTPException(status_code=404, detail="Chatroom not found")
        if channel.channel_type == "wechat_gugatool":
            # 咕嘎的历史系统消息正文只有占位,原始 XML 在 raw_webhook_events;
            # 回查逻辑放在渠道适配器里,共享服务保持原样。
            from app.services.channel_adapters.wechat_gugatool import backfill_wechat_gugatool_invitations

            report = backfill_wechat_gugatool_invitations(db, channel, room)
        else:
            report = backfill_chatroom_invitations(db, channel, room)
        db.commit()
        source_label = "入群系统事件" if channel.channel_type == "wechat_gugatool" else "系统消息"
        message = f"已检查本群 {report['checked']} 条{source_label}，补充 {report['updated']} 条邀请关系。"
        if report["limited"]:
            message += f"本次检查最近 2000 条{source_label}。"
        record_audit(db, f"channel.{channel.channel_type}.chatroom.invitations.backfill", "chatroom", room.id, message)
        return _completed_operation_response(request, message, f"/channels/{channel_id}/chatrooms/{chatroom_id}?tab=members")
    finally:
        db.close()


@router.post("/channels/{channel_id}/chatrooms/{chatroom_id}/sync")
async def sync_wechat_ipad_chatroom_action(request: Request, channel_id: int, chatroom_id: int, tab: str = Form("features")):
    enforce_role(request, "ops")
    db = get_session()
    try:
        await _require_operation_csrf(db, request)
        channel = get_channel(db, channel_id)
        if not channel:
            raise HTTPException(status_code=404, detail="Channel not found")
        chatroom = db.get(Chatroom, chatroom_id)
        if not chatroom or chatroom.channel_id != channel.id:
            raise HTTPException(status_code=404, detail="Chatroom not found")
        # 只允许既有标签作为同步后的落点，保留资料/成员页且不接受任意重定向地址。
        return_tab = tab if tab in ("features", "plugins", "members", "profile") else "features"
        return_url = f"/channels/{channel_id}/chatrooms/{chatroom_id}?tab={return_tab}"
        if channel.channel_type == "wechat_ipad":
            overview = sync_wechat_ipad_chatrooms(db, channel, chatroom_ids=[chatroom.external_id])
            message = str(overview.get("last_status_summary") or "已触发单群资料同步")
            record_audit(db, "channel.wechat-ipad.sync-chatroom", "chatroom", chatroom.id, message)
        elif channel.channel_type == "wechat_winauto":
            overview = sync_wechat_winauto_chatrooms(db, channel)
            db.commit()
            message = str(overview.get("last_status_summary") or "已刷新当前群样本")
            record_audit(db, f"channel.{channel.channel_type}.sync-chatroom", "chatroom", chatroom.id, message)
            return _completed_operation_response(request, message, return_url)
        elif channel.channel_type == "workwechat":
            if is_cloud_poll_enabled(channel):
                # 云端模式下服务端不能直连客服电脑；先下发群列表任务补中文群名，再下发成员任务补成员快照。
                profile_job = enqueue_job(
                    db,
                    job_type=WORKWECHAT_CLOUD_CONTROL_JOB,
                    payload={
                        "channel_id": channel.id,
                        "action": "sync_chatrooms",
                        "target_chatroom_id": chatroom.id,
                        "target_external_id": chatroom.external_id,
                        "payload": {"page_size": 100, "max_pages": 20, "target_external_id": chatroom.external_id},
                    },
                    priority=96,
                    related_type="chatroom",
                    related_id=chatroom.id,
                    max_attempts=3,
                )
                member_job = enqueue_job(
                    db,
                    job_type=WORKWECHAT_CLOUD_CONTROL_JOB,
                    payload={
                        "channel_id": channel.id,
                        "action": "sync_chatroom_members",
                        "chatroom_id": chatroom.id,
                        "payload": {"conversation_id": chatroom.external_id, "page_size": 100, "max_pages": 30},
                    },
                    priority=95,
                    related_type="chatroom",
                    related_id=chatroom.id,
                    max_attempts=3,
                )
                db.commit()
                notify_cloud_job_enqueued(channel, profile_job)
                notify_cloud_job_enqueued(channel, member_job)
                message = "企微群资料和成员同步已排队，正在等待客户端执行。"
                record_audit(db, "channel.workwechat.sync-chatroom", "chatroom", chatroom.id, message)
                return _queued_operation_response(request, db, channel, [profile_job, member_job], return_url)
            # workwechat 没有单群资料详情接口；这里先用全量群列表只更新当前群，再用 Hook 11040 刷新成员。
            profile_overview = sync_workwechat_chatroom_profile_from_list(db, channel, chatroom)
            member_overview = sync_workwechat_chatroom_members(db, channel, chatroom)
            db.commit()
            profile_summary = str(profile_overview.get("last_status_summary") or "").strip()
            member_summary = str(member_overview.get("last_status_summary") or "").strip()
            message = "；".join(part for part in (profile_summary, member_summary) if part) or "已刷新当前企微群资料和成员"
            record_audit(db, "channel.workwechat.sync-chatroom", "chatroom", chatroom.id, message)
            return _completed_operation_response(request, message, return_url, ok=bool(profile_overview.get("ok") and member_overview.get("ok")))
        elif channel.channel_type == "wechat_webridge":
            # 微桥一次同步读取成员与公告缓存，两个命令分别回报，沿用现有的多任务进度。
            member_job = enqueue_command(db, channel, command_name="room_members", args={"room_id": chatroom.external_id})
            notice_job = enqueue_command(db, channel, command_name="get_group_notice", args={"room_id": chatroom.external_id})
            db.commit()
            message = "群资料和成员同步已排队，正在等待手机执行。"
            record_audit(db, "channel.wechat-webridge.sync-chatroom", "chatroom", chatroom.id, message)
            return _queued_operation_response(request, db, channel, [member_job, notice_job], return_url)
        else:
            raise HTTPException(status_code=400, detail="Channel is not wechat_ipad / wechat_winauto / workwechat / wechat_webridge")
        runtime = overview.get("runtime_info", {})
        return _completed_operation_response(request, message, return_url, ok=not bool(runtime.get("last_error") or runtime.get("last_chatroom_sync_failed_targets")))
    finally:
        db.close()


@router.post("/chatrooms/create")
async def create_channel_chatroom_action(
    request: Request,
    channel_id: int = Form(...),
    external_id: str = Form(...),
    name: str = Form(""),
    remark: str = Form(""),
    operator_note: str = Form(""),
):
    user = enforce_role(request, "ops")
    db = get_session()
    try:
        channel = get_channel(db, channel_id)
        if not channel or channel.channel_type not in GROUP_SCOPE_CHANNEL_TYPES:
            raise HTTPException(status_code=404, detail="Channel not found")
        normalized_external_id = str(external_id or "").strip()
        if not normalized_external_id:
            return RedirectResponse(url=f"/chatrooms?channel_id={channel_id}&notice={quote('群外部 ID 不能为空')}", status_code=303)
        chatroom = upsert_manual_chatroom(
            db,
            channel=channel,
            external_id=normalized_external_id,
            name=name,
            remark=remark,
            operator_note=f"{str(operator_note or '').strip()}".strip() or f"manual created by {user.username}",
        )
        db.commit()
        record_audit(db, f"channel.{channel.channel_type}.chatroom.create", "chatroom", chatroom.id, f"external_id={normalized_external_id}")
        return RedirectResponse(
            url=f"/channels/{channel_id}/chatrooms/{chatroom.id}?notice={quote('已创建群登记，默认待启用')}",
            status_code=303,
        )
    finally:
        db.close()


@router.post("/channels/{channel_id}/chatrooms/{chatroom_id}/management")
async def update_channel_chatroom_management_action(
    request: Request,
    channel_id: int,
    chatroom_id: int,
    management_status: str = Form(...),
    next: str = Form(""),
    csrf_token: str = Form(""),
):
    """切换群的运行模式。群详情页的单选框选中即生效(脚本 fetch 提交,返回 JSON);群列表页的行内开关是整页表单提交。"""

    user = enforce_role(request, "ops")
    db = get_session()
    try:
        require_tenant_csrf(db, request, csrf_token or _csrf_from_header(request))
        channel = get_channel(db, channel_id)
        if not channel or channel.channel_type not in GROUP_SCOPE_CHANNEL_TYPES:
            raise HTTPException(status_code=404, detail="Channel not found")
        chatroom = db.get(Chatroom, chatroom_id)
        if not chatroom or chatroom.channel_id != channel.id:
            raise HTTPException(status_code=404, detail="Chatroom not found")
        set_chatroom_management_status(chatroom, management_status)
        db.commit()
        record_audit(
            db,
            f"channel.{channel.channel_type}.chatroom.management",
            "chatroom",
            chatroom.id,
            f"status={chatroom.management_status}, operator={user.username}",
        )
        message = {
            "enabled": "本群已设为正常运行：采集消息，并响应插件、群功能和 AI",
            "disabled": "本群已设为只采集消息：继续记录消息，但不触发任何功能",
            "archived": "本群已完全关闭：不接收、不记录消息，也不触发任何功能",
        }.get(chatroom.management_status, "已将该群标记为待启用")
        if request.headers.get("X-Requested-With") == "fetch":
            return JSONResponse({"ok": True, "notice": message, "management_status": chatroom.management_status})
        # 列表页行内开关带 next 回到列表;只接受站内路径,防止开放重定向。
        target = next if next.startswith("/") and not next.startswith("//") else f"/channels/{channel_id}/chatrooms/{chatroom_id}"
        separator = "&" if "?" in target else "?"
        return RedirectResponse(url=preserve_return_to(request, f"{target}{separator}notice={quote(message)}"), status_code=303)
    finally:
        db.close()


@router.post("/channels/{channel_id}/wechat-winauto/sync-chatrooms")
async def sync_wechat_winauto_chatrooms_action(request: Request, channel_id: int):
    enforce_role(request, "ops")
    db = get_session()
    try:
        await _require_operation_csrf(db, request)
        channel = get_channel(db, channel_id)
        if not channel:
            raise HTTPException(status_code=404, detail="Channel not found")
        if channel.channel_type != "wechat_winauto":
            raise HTTPException(status_code=400, detail="Channel is not wechat_winauto")
        overview = sync_wechat_winauto_chatrooms(db, channel)
        db.commit()
        message = str(overview.get("last_status_summary") or "已刷新 wxauto 群列表")
        record_audit(db, f"channel.{channel.channel_type}.sync-chatrooms", "channel", channel.id, message)
        return _completed_operation_response(request, message, f"/chatrooms?channel_id={channel_id}")
    finally:
        db.close()


@router.post("/channels/{channel_id}/workwechat/sync-chatrooms")
async def sync_workwechat_chatrooms_action(request: Request, channel_id: int):
    enforce_role(request, "ops")
    db = get_session()
    try:
        await _require_operation_csrf(db, request)
        channel = get_channel(db, channel_id)
        if not channel:
            raise HTTPException(status_code=404, detail="Channel not found")
        if channel.channel_type != "workwechat":
            raise HTTPException(status_code=400, detail="Channel is not workwechat")
        if is_cloud_poll_enabled(channel):
            # 云端模式下服务端无法直连客服电脑的 provider，因此这里改为下发控制任务；
            # 在线 provider 会通过 WS 立即领取，离线时也会在下一次 poll 中补偿执行。
            job = enqueue_job(
                db,
                job_type=WORKWECHAT_CLOUD_CONTROL_JOB,
                payload={"channel_id": channel.id, "action": "sync_chatrooms", "payload": {"page_size": 100, "max_pages": 20}},
                priority=95,
                related_type="channel",
                related_id=channel.id,
                max_attempts=3,
            )
            db.commit()
            notify_cloud_job_enqueued(channel, job)
            message = "企微群列表同步已排队，正在等待客户端执行。"
            record_audit(db, "channel.workwechat.sync-chatrooms", "channel", channel.id, message)
            return _queued_operation_response(request, db, channel, [job], f"/chatrooms?channel_id={channel_id}")
        # workwechat 群列表同步走 provider 的 11038，失败时服务层会保留本地消息样本视图。
        overview = sync_workwechat_chatrooms(db, channel)
        db.commit()
        message = str(overview.get("last_status_summary") or "已同步企微群列表")
        record_audit(db, "channel.workwechat.sync-chatrooms", "channel", channel.id, message)
        return _completed_operation_response(request, message, f"/chatrooms?channel_id={channel_id}", ok=bool((overview.get("provider_response") or {}).get("ok")))
    finally:
        db.close()
