from __future__ import annotations

import re
from typing import Any, Mapping


COMMAND_PREFIXES = ("发单", "补发单", "查询批次", "批次进度", "供应商帮助")
EXCEL_SUFFIXES = (".xls", ".xlsx")
TRACKING_TOKEN_RE = re.compile(r"^[A-Z0-9]{8,32}$")


def matches_event(context: Any) -> dict[str, Any]:
    """轻量识别收发单相关企微群消息。

    这个插件已经不处理供应商、批次、Excel 解析和发货导入；这些业务都在独立
    supplier-order-admin 系统里完成。这里保留 matcher 的目的，是让 Fulcrum 仍能在插件列表
    里展示“供应商订单收发”，并允许运营按群关闭该入口，避免误触发或误提示。
    """
    if _conversation_disabled(context):
        return {"matched": False, "reason": "supplier_order_flow_group_disabled"}
    if getattr(context, "channel_type", "") != "workwechat":
        return {"matched": False, "reason": "supplier_order_flow_only_handles_workwechat"}
    if getattr(context, "chat_type", "") != "group":
        return {"matched": False, "reason": "supplier_order_flow_only_handles_group_messages"}

    content = str(getattr(context, "content", "") or "").strip()
    if _is_command(content):
        return {"matched": True, "reason": "supplier_order_command"}

    attachments = tuple(getattr(context, "attachments", ()) or ())
    if any(_is_excel_attachment(item) for item in attachments):
        return {"matched": True, "reason": "supplier_order_excel_candidate"}

    if _has_text_return_tracking_number(content):
        return {"matched": True, "reason": "supplier_order_text_return_candidate"}

    return {"matched": False, "reason": "supplier_order_flow_no_match"}


def handle_event(context: Any) -> dict[str, Any]:
    """执行桥接插件。

    普通回传候选只记录“已识别”结果，不自动回复群消息；入站事件会由 Fulcrum 的 Open
    Webhook 推给 supplier-order-admin，避免本插件和独立系统重复通知供应商群。
    只有“供应商帮助”命令会返回一条说明，方便现场排查配置边界。
    """
    content = str(getattr(context, "content", "") or "").strip()
    if _conversation_disabled(context):
        return _completed("当前群已停用收发单插件桥接。", reply=False)
    if content.startswith(("供应商帮助", "/供应商帮助")):
        return _completed(_help_text(context), reply=True)
    if _is_command(content):
        return _completed("已识别收发单命令；请在独立收发单系统后台处理发单、批次和回传。", reply=False)
    if any(_is_excel_attachment(item) for item in tuple(getattr(context, "attachments", ()) or ())):
        return _completed("已识别收发单 Excel 候选；实际回传处理由 Open Webhook 交给独立收发单系统。", reply=False)
    if _has_text_return_tracking_number(content):
        return _completed("已识别收发单文本候选；实际回传处理由 Open Webhook 交给独立收发单系统。", reply=False)
    return _completed("收发单桥接插件未处理该消息。", reply=False)


def execute_action(request: Any) -> dict[str, Any]:
    """支持插件运行页的重载/执行按钮。

    目录插件适配器会把模块级 `execute_action` 暴露到运行状态页；这里返回桥接状态，
    不做发单、解析或导入，防止手动执行按钮绕过独立收发单系统。
    """
    action = str(getattr(request, "action", "") or "").strip()
    config = dict(getattr(request, "config", {}) or {})
    if action == "reload":
        return {
            "ok": True,
            "status": "running",
            "message": "供应商订单收发桥接插件已就绪；业务处理由独立 supplier-order-admin 系统承载。",
            "output": {
                "bridge_only": True,
                "supplier_order_admin_url": str(config.get("supplier_order_admin_url") or "").strip(),
                "disabled_conversation_external_ids": sorted(_as_text_set(config.get("disabled_conversation_external_ids"))),
            },
        }
    return {
        "ok": True,
        "status": "bridge_only",
        "message": "该插件只负责识别和提示；请在独立收发单系统后台执行发单、回传解析和导入。",
        "output": {"bridge_only": True},
    }


def _conversation_disabled(context: Any) -> bool:
    """支持在插件绑定配置里按群 external_id 兜底停用。

    Fulcrum 已有群详情页的“群内强制停用”能力，会在插件进入 matches_event 前生效；
    这里再支持 `disabled_conversation_external_ids`，用于群还没有进入群管理表、
    或运营希望直接按企微会话 ID 精确排除的场景。
    """
    external_id = str(getattr(context, "conversation_external_id", "") or "").strip()
    if not external_id:
        return False
    config = _binding_config(context)
    disabled_ids = _as_text_set(config.get("disabled_conversation_external_ids"))
    disabled_ids.update(_as_text_set(config.get("disabled_group_external_ids")))
    return external_id in disabled_ids


def _binding_config(context: Any) -> dict[str, Any]:
    raw = getattr(context, "binding_config", {}) or {}
    return dict(raw) if isinstance(raw, Mapping) else {}


def _is_command(content: str) -> bool:
    return any(content.startswith(command) or content.startswith(f"/{command}") for command in COMMAND_PREFIXES)


def _is_excel_attachment(attachment: Any) -> bool:
    if not isinstance(attachment, Mapping):
        return False
    file_name = str(
        attachment.get("file_name")
        or attachment.get("filename")
        or attachment.get("name")
        or attachment.get("source_path")
        or attachment.get("download_path")
        or ""
    ).strip().lower()
    return file_name.endswith(EXCEL_SUFFIXES)


def _has_text_return_tracking_number(content: str) -> bool:
    """保守识别文本回传，避免系统提示文本反向触发插件。"""
    text = str(content or "").strip()
    if not ("分单ID" in text and "快递单号" in text):
        return False

    labeled = re.search(r"快递单号\s*[:：]\s*([A-Za-z0-9][A-Za-z0-9\s-]{5,40})", text)
    if labeled and _looks_like_tracking_number(labeled.group(1)):
        return True

    for line in text.splitlines():
        normalized = line.strip()
        if not normalized or "快递单号" in normalized or "分单ID" in normalized:
            continue
        parts = [part for part in re.split(r"[\s,，|;；:：]+", normalized) if part]
        if len(parts) >= 3 and _looks_like_tracking_number(parts[2]):
            return True
    return False


def _looks_like_tracking_number(value: str) -> bool:
    token = _normalize_tracking_number(value)
    return bool(TRACKING_TOKEN_RE.match(token) and any(char.isdigit() for char in token))


def _normalize_tracking_number(value: str) -> str:
    return re.sub(r"[^A-Za-z0-9]", "", str(value or "")).upper()


def _help_text(context: Any) -> str:
    config = _binding_config(context)
    text = str(config.get("help_text") or "").strip()
    admin_url = str(config.get("supplier_order_admin_url") or "").strip()
    if text and admin_url:
        return f"{text}\n后台地址：{admin_url}"
    if text:
        return text
    if admin_url:
        return f"收发单功能已迁移到独立系统，请到后台处理：{admin_url}"
    return "收发单功能已迁移到独立 supplier-order-admin 系统；Fulcrum 只负责消息网关、Open API 和 Webhook。"


def _completed(message: str, *, reply: bool) -> dict[str, Any]:
    output: dict[str, Any] = {"bridge_only": True}
    if reply:
        output["reply_text"] = message
        output["replies"] = [{"type": "text", "content": message}]
    return {
        "ok": True,
        "status": "completed",
        "message": message,
        "output": output,
    }


def _as_bool(value: object, *, default: bool) -> bool:
    if value is None:
        return default
    if isinstance(value, bool):
        return value
    text = str(value).strip().lower()
    if text in {"1", "true", "yes", "on", "enabled"}:
        return True
    if text in {"0", "false", "no", "off", "disabled"}:
        return False
    return default


def _as_text_set(value: object) -> set[str]:
    if isinstance(value, list):
        return {str(item).strip() for item in value if str(item).strip()}
    if isinstance(value, str):
        return {item.strip() for item in value.replace("，", ",").split(",") if item.strip()}
    return set()
