from __future__ import annotations

import asyncio
import base64
import importlib.util
import inspect
import json
import re
import time
import signal
import sys
import threading
from dataclasses import dataclass, field
from html import unescape
from pathlib import Path
from typing import Any, Mapping, Protocol

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.db import TenantSessionLocal
from app.core.logging import get_logger
from app.core.settings import settings
from app.core.tenant_context import tenant_scope
from app.core.tenant_db import tenant_session
from app.core.time import now_shanghai
from app.services.plugin_governance import blocked_plugin_codes
from app.services.config_fields import ConfigField
from app.services.plugin_manifest import ManifestParseError, PluginSchedule, parse_manifest, read_config_fields, read_schedules
from app.services.outbound_files import externalize_media_payload, redact_base64_fields
from app.models.entities import BotPlugin, Channel, Chatroom, Conversation, GroupPluginScope, Plugin, TaskJob
from app.services.plugin_secrets import decrypt_plugin_secret_config
from app.services.plugin_scopes import group_plugin_scope_enabled


ENTRYPOINT_CANDIDATES = ("plugin.py", "main.py", "plugin_main.py")
SKIP_MESSAGE_PLUGIN_TYPES = {"system", "scheduled", "timer", "cron"}
ALLOWED_PLUGIN_TYPES = {"message", "observer", "command", "rule", "ai", "system", "scheduled", "timer", "cron"}
DEFAULT_PRIORITY = 100
DEFAULT_TIMEOUT_MS = 10000
DEFAULT_CONSUME_POLICY = "continue"
ALLOWED_CONSUME_POLICIES = {"continue", "stop", "consume", "exclusive"}
STOP_CONSUME_POLICIES = {"stop", "consume", "exclusive"}
DISPLAY_NAME_OVERRIDES = {
    "ai_auto_response": "AI自动回复",
    "ai_gen_image": "AI绘图",
    "dify": "Dify 对话",
    "douyin_parser": "抖音解析",
    "douyu": "斗鱼",
    "duanju_search": "短剧搜索",
    "game_task": "游戏任务",
    "global_news": "全球新闻",
    "group_auto_invite": "自动拉群",
    "group_virtual": "群虚拟",
    "guess_song": "猜歌",
    "jd_sign_token": "京东签到令牌",
    "lucky_pot": "幸运池",
    "message_push_task": "消息推送任务",
    "message_sign": "消息签到",
    "message_summary": "消息总结",
    "music": "音乐点播",
    "point_trade": "积分交易",
    "weather": "天气",
}
TELEGRAM_TYPING_HEARTBEAT_INTERVAL_SECONDS = 4.0

_SCAN_CACHE: dict[str, tuple[tuple[str, ...], tuple["DiscoveredPlugin", ...]]] = {}
_MODULE_CACHE: dict[str, tuple[int, "LoadedDirectoryPlugin"]] = {}
logger = get_logger(__name__)


@dataclass(frozen=True)
class PluginMatcher:
    commands: tuple[str, ...] = ()
    keywords: tuple[str, ...] = ()
    require_mention: bool = False


@dataclass(frozen=True)
class PluginCapability:
    channel_types: tuple[str, ...] = ()
    chat_types: tuple[str, ...] = ()
    message_types: tuple[str, ...] = ()
    priority: int = DEFAULT_PRIORITY
    consume_policy: str = DEFAULT_CONSUME_POLICY
    timeout_ms: int = DEFAULT_TIMEOUT_MS


@dataclass(frozen=True)
class PluginAiHint:
    """插件声明给 AI 调度的说明；没有 [ai] 段或 routable=false 的插件不参与 AI 调度。

    [ai] 段只接受扁平键和字符串数组。
    """

    routable: bool = False
    # 只有 low 允许 AI 直接调度；medium、high 首期一律不开放。
    risk: str = "high"
    summary: str = ""
    examples: tuple[str, ...] = ()
    # 命令后面需要的参数说明，如“城市名”；为空表示命令不带参数。
    needs_args: str = ""


@dataclass(frozen=True)
class PluginManifest:
    code: str
    display_name: str
    plugin_type: str = "message"
    version: str | None = None
    author: str | None = None
    description: str | None = None
    default_enabled: bool = False


@dataclass(frozen=True)
class DiscoveredPlugin:
    manifest: PluginManifest
    capability: PluginCapability
    matcher: PluginMatcher
    config_path: Path
    entrypoint_path: Path
    directory_path: Path
    ai_hint: "PluginAiHint" = field(default_factory=lambda: PluginAiHint())
    runtime_config: Mapping[str, Any] = field(default_factory=dict)
    secret_fields: tuple[str, ...] = ()
    # 清单 v2 的 [[config.fields]] 声明;为空表示插件没有声明,配置页回退到按默认值推断。
    config_fields: tuple[ConfigField, ...] = ()
    # 清单里声明的 [[schedule]] 定时计划;调度器据此自动入队,新增定时插件不需要改调度代码。
    schedules: tuple[PluginSchedule, ...] = ()
    admin_config: Mapping[str, Any] = field(default_factory=dict)
    validation_errors: tuple[str, ...] = ()
    validation_warnings: tuple[str, ...] = ()

    @property
    def entrypoint_name(self) -> str:
        return self.entrypoint_path.name

    @property
    def is_valid(self) -> bool:
        return not self.validation_errors


@dataclass(frozen=True)
class PluginDispatchContext:
    tenant_id: int
    bot_id: int
    channel_id: int
    channel_type: str
    chat_type: str
    message_type: str
    command_prefix: str = "/"
    sender_id: str | None = None
    conversation_external_id: str | None = None
    message_text: str = ""
    mentioned_self: bool = False
    attachments: tuple[Mapping[str, Any], ...] = ()


@dataclass(frozen=True)
class PluginDispatchCandidate:
    binding_id: int
    plugin_id: int
    plugin_code: str
    plugin_type: str
    priority: int
    consume_policy: str
    timeout_ms: int
    entrypoint_name: str
    commands: tuple[str, ...] = ()
    keywords: tuple[str, ...] = ()
    require_mention: bool = False
    scope_status: str = "inherited"
    scope_enabled: bool | None = None
    scope_config_override_json: str | None = None


@dataclass(frozen=True)
class PluginDispatchPlan:
    context: PluginDispatchContext
    candidates: tuple[PluginDispatchCandidate, ...] = ()
    skipped: tuple[tuple[str, str], ...] = ()

    @property
    def candidate_plugin_codes(self) -> tuple[str, ...]:
        return tuple(item.plugin_code for item in self.candidates)


@dataclass(frozen=True)
class PluginMatchDecision:
    candidate: PluginDispatchCandidate
    matched: bool
    reason: str
    debug: Mapping[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class DirectoryPluginContext:
    plugin_code: str
    binding_id: int
    bot_id: int
    channel_id: int
    channel_type: str
    chat_type: str
    message_type: str
    command_prefix: str
    content: str
    sender_id: str | None
    conversation_external_id: str | None
    mentioned_self: bool
    attachments: tuple[Mapping[str, Any], ...] = ()
    binding_config: Mapping[str, Any] = field(default_factory=dict)
    normalized_event: Mapping[str, Any] = field(default_factory=dict)
    raw_event_id: int | None = None
    conversation_id: int | None = None
    message_id: int | None = None
    command_name: str | None = None
    matched_keyword: str | None = None


@dataclass(frozen=True)
class DirectoryPluginHandleResult:
    ok: bool
    status: str
    message: str
    output: Mapping[str, Any] = field(default_factory=dict)


class DirectoryPluginProtocol(Protocol):
    def matches_event(self, context: DirectoryPluginContext) -> bool | Mapping[str, Any]:
        ...

    def handle_event(self, context: DirectoryPluginContext) -> DirectoryPluginHandleResult | Mapping[str, Any]:
        ...


@dataclass(frozen=True)
class LoadedDirectoryPlugin:
    plugin_code: str
    module_name: str
    entrypoint_path: Path
    instance: Any | None
    supports_matcher: bool
    supports_handler: bool


@dataclass(frozen=True)
class PluginDispatchExecution:
    candidate: PluginDispatchCandidate
    attempted: bool
    ok: bool
    status: str
    message: str
    debug: Mapping[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class PluginDispatchReport:
    static_plan: PluginDispatchPlan
    decisions: tuple[PluginMatchDecision, ...] = ()
    executions: tuple[PluginDispatchExecution, ...] = ()
    stop_reason: str | None = None

    @property
    def matched_plugin_codes(self) -> tuple[str, ...]:
        return tuple(item.candidate.plugin_code for item in self.decisions if item.matched)

    @property
    def executed_plugin_codes(self) -> tuple[str, ...]:
        return tuple(item.candidate.plugin_code for item in self.executions if item.attempted)


class PluginExecutionTimeoutError(TimeoutError):
    """插件执行超时。"""


def discover_plugins(*, force: bool = False) -> tuple[DiscoveredPlugin, ...]:
    base_dir = Path(settings.plugins_dir).resolve()
    signature = _build_scan_signature(base_dir)
    cache_key = str(base_dir)
    if not force and cache_key in _SCAN_CACHE and _SCAN_CACHE[cache_key][0] == signature:
        return _SCAN_CACHE[cache_key][1]

    discovered: list[DiscoveredPlugin] = []
    if base_dir.exists():
        for plugin_dir in sorted(path for path in base_dir.iterdir() if path.is_dir()):
            config_path = plugin_dir / "config.toml"
            if not config_path.exists():
                continue
            entrypoint_path = _resolve_entrypoint(plugin_dir)
            if not entrypoint_path:
                continue
            try:
                parsed, parse_error = parse_manifest(config_path.read_text(encoding="utf-8")), ""
            except ManifestParseError as exc:
                # config.toml 不是合法的 TOML:这个插件标记为无效(页面上能看到原因),不影响其他插件的扫描。
                parsed, parse_error = {}, f"config.toml 解析失败：{exc}"
            discovered.append(_build_discovered_plugin(plugin_dir, config_path, entrypoint_path, parsed, parse_error=parse_error))

    result = tuple(discovered)
    _SCAN_CACHE[cache_key] = (signature, result)
    logger.info("plugin discovery completed", extra={"plugin_count": len(result), "plugins_dir": str(base_dir)})
    return result


def get_discovered_plugin(plugin_code: str | None, *, force: bool = False) -> DiscoveredPlugin | None:
    if not plugin_code:
        return None
    normalized = plugin_code.strip()
    for plugin in discover_plugins(force=force):
        if plugin.manifest.code == normalized:
            return plugin
    return None


def get_plugin_default_config(plugin_code: str | None, *, force: bool = False) -> dict[str, Any]:
    discovered = get_discovered_plugin(plugin_code, force=force)
    if not discovered:
        return {}
    return dict(discovered.runtime_config)


def load_directory_plugin(plugin_code: str | None, *, force: bool = False) -> LoadedDirectoryPlugin | None:
    discovered = get_discovered_plugin(plugin_code, force=force)
    if not discovered:
        logger.warning("plugin load skipped because plugin was not discovered", extra={"plugin_code": plugin_code or ""})
        return None
    if not discovered.is_valid:
        logger.warning("plugin load skipped because manifest is invalid", extra={"plugin_code": discovered.manifest.code})
        return None

    cache_key = str(discovered.entrypoint_path)
    mtime = discovered.entrypoint_path.stat().st_mtime_ns
    if not force and cache_key in _MODULE_CACHE and _MODULE_CACHE[cache_key][0] == mtime:
        return _MODULE_CACHE[cache_key][1]

    module_name = f"fulcrum_plugin_{discovered.manifest.code}_{mtime}"
    spec = importlib.util.spec_from_file_location(module_name, discovered.entrypoint_path)
    if spec is None or spec.loader is None:
        return None
    module = importlib.util.module_from_spec(spec)
    sys.modules[module_name] = module
    try:
        spec.loader.exec_module(module)
    except Exception:
        sys.modules.pop(module_name, None)
        raise

    instance = getattr(module, "plugin", None) or getattr(module, "PLUGIN", None)
    module_supports_matcher = bool(hasattr(module, "matches_event"))
    module_supports_handler = bool(hasattr(module, "handle_event"))
    if instance is None and (
        module_supports_handler
        or hasattr(module, "execute_action")
        or hasattr(module, "execute_message_action")
    ):
        instance = _ModuleFunctionAdapter(module)

    loaded = LoadedDirectoryPlugin(
        plugin_code=discovered.manifest.code,
        module_name=module_name,
        entrypoint_path=discovered.entrypoint_path,
        instance=instance,
        supports_matcher=bool(instance and (module_supports_matcher or hasattr(instance, "matches_event"))),
        supports_handler=bool(instance and (module_supports_handler or not isinstance(instance, _ModuleFunctionAdapter)) and hasattr(instance, "handle_event")),
    )
    _MODULE_CACHE[cache_key] = (mtime, loaded)
    logger.info(
        "plugin module loaded",
        extra={
            "plugin_code": discovered.manifest.code,
            "entrypoint": str(discovered.entrypoint_path),
            "supports_handler": loaded.supports_handler,
            "supports_matcher": loaded.supports_matcher,
            "supports_execute_action": bool(loaded.instance and hasattr(loaded.instance, "execute_action")),
        },
    )
    return loaded


def sync_plugins_from_directory(db: Session, *, force: bool = False) -> list[Plugin]:
    discovered = discover_plugins(force=force)
    discovered_codes = {item.manifest.code for item in discovered}
    rows: list[Plugin] = []
    for item in discovered:
        row = db.scalar(select(Plugin).where(Plugin.code == item.manifest.code))
        if row is None:
            row = Plugin(code=item.manifest.code, display_name=item.manifest.display_name)
            db.add(row)
            db.flush()
        row.display_name = item.manifest.display_name
        row.plugin_type = item.manifest.plugin_type if item.is_valid else "invalid"
        row.version = item.manifest.version
        row.author = item.manifest.author
        row.description = _build_synced_plugin_description(item)
        row.default_enabled = item.manifest.default_enabled if item.is_valid else False
        row.missing_at = None  # 目录里又出现了:清除"缺失"标记
        rows.append(row)
    # 目录里暂时找不到的插件只做标记,绝不删除记录:删除会级联清掉所有租户的绑定、配置、
    # 加密密钥和状态数据(目录被误清空或部署漏拷贝时尤其危险)。恢复目录后标记自动清除。
    now = now_shanghai()
    for stale in db.scalars(select(Plugin).where(Plugin.missing_at.is_(None), ~Plugin.code.in_(discovered_codes))).all():
        stale.missing_at = now
    db.commit()
    return rows


def build_static_dispatch_plan(
    db: Session,
    channel: Channel,
    normalized_event: dict[str, Any],
) -> PluginDispatchPlan:
    context = _build_dispatch_context(channel, normalized_event)
    logger.debug(
        "building static dispatch plan",
        extra={
            "channel_id": channel.id,
            "channel_type": channel.channel_type,
            "message_type": context.message_type,
            "chat_type": context.chat_type,
            "message_text": context.message_text[:200],
        },
    )
    bindings = db.scalars(
        select(BotPlugin).where(BotPlugin.bot_id == channel.bot_id).order_by(BotPlugin.id.asc())
    ).all()
    candidates: list[PluginDispatchCandidate] = []
    skipped: list[tuple[str, str]] = []
    discovered_map = {item.manifest.code: item for item in discover_plugins()}
    group_chatroom = _load_group_chatroom(db, channel=channel, context=context)
    group_scope_map = _load_group_scope_map(db, channel=channel, context=context)
    # 平台停用或对本租户不可见的插件:绑定保留,但不参与调度
    platform_blocked = blocked_plugin_codes(db, int(channel.tenant_id))

    for binding in bindings:
        plugin = binding.plugin
        plugin_code = plugin.code if plugin else ""
        if not plugin_code:
            skipped.append((f"binding:{binding.id}", "missing_plugin"))
            logger.debug("static plan skipped plugin", extra={"binding_id": binding.id, "reason": "missing_plugin"})
            continue
        if binding.status != "enabled":
            skipped.append((plugin_code, "binding_disabled"))
            logger.debug("static plan skipped plugin", extra={"plugin_code": plugin_code, "reason": "binding_disabled"})
            continue
        if plugin_code in platform_blocked:
            skipped.append((plugin_code, "platform_blocked"))
            logger.debug("static plan skipped plugin", extra={"plugin_code": plugin_code, "reason": "platform_blocked"})
            continue

        discovered = discovered_map.get(plugin_code)
        if not discovered:
            skipped.append((plugin_code, "plugin_not_discovered"))
            logger.debug("static plan skipped plugin", extra={"plugin_code": plugin_code, "reason": "plugin_not_discovered"})
            continue
        if not discovered.is_valid:
            skipped.append((plugin_code, "invalid_manifest"))
            logger.debug("static plan skipped plugin", extra={"plugin_code": plugin_code, "reason": "invalid_manifest"})
            continue
        if discovered.manifest.plugin_type in SKIP_MESSAGE_PLUGIN_TYPES:
            skipped.append((plugin_code, "non_message_plugin"))
            logger.debug("static plan skipped plugin", extra={"plugin_code": plugin_code, "reason": "non_message_plugin"})
            continue
        if discovered.capability.channel_types and not _channel_type_matches(
            context.channel_type,
            discovered.capability.channel_types,
        ):
            skipped.append((plugin_code, "channel_type_mismatch"))
            logger.debug(
                "static plan skipped plugin",
                extra={"plugin_code": plugin_code, "reason": "channel_type_mismatch", "channel_type": context.channel_type},
            )
            continue
        if discovered.capability.chat_types and context.chat_type not in discovered.capability.chat_types:
            skipped.append((plugin_code, "chat_type_mismatch"))
            logger.debug(
                "static plan skipped plugin",
                extra={"plugin_code": plugin_code, "reason": "chat_type_mismatch", "chat_type": context.chat_type},
            )
            continue
        if discovered.capability.message_types and context.message_type not in discovered.capability.message_types:
            skipped.append((plugin_code, "message_type_mismatch"))
            logger.debug(
                "static plan skipped plugin",
                extra={"plugin_code": plugin_code, "reason": "message_type_mismatch", "message_type": context.message_type},
            )
            continue
        if _is_denied_by_binding_scope(binding, context):
            skipped.append((plugin_code, "binding_scope_rejected"))
            logger.debug("static plan skipped plugin", extra={"plugin_code": plugin_code, "reason": "binding_scope_rejected"})
            continue
        if group_chatroom is not None and not bool(getattr(group_chatroom, "is_active", False)):
            skipped.append((plugin_code, "group_chatroom_inactive"))
            logger.debug(
                "static plan skipped plugin",
                extra={
                    "plugin_code": plugin_code,
                    "reason": "group_chatroom_inactive",
                    "chatroom_id": getattr(group_chatroom, "id", None),
                    "management_status": getattr(group_chatroom, "management_status", None),
                },
            )
            continue
        scope = group_scope_map.get(binding.id)
        scope_status = str(getattr(scope, "scope_status", "") or "inherited")
        scope_enabled = group_plugin_scope_enabled(binding, scope) if context.chat_type == "group" else None
        if context.chat_type == "group" and not scope_enabled:
            skipped.append((plugin_code, "group_scope_disabled"))
            logger.debug(
                "static plan skipped plugin",
                extra={"plugin_code": plugin_code, "reason": "group_scope_disabled", "scope_status": scope_status},
            )
            continue

        matcher = _resolve_effective_matcher(binding, discovered)
        logger.debug(
            "static plan accepted candidate",
            extra={
                "plugin_code": plugin_code,
                "binding_id": binding.id,
                "plugin_type": discovered.manifest.plugin_type,
                "commands": list(matcher.commands),
                "keywords": list(matcher.keywords),
            },
        )
        candidates.append(
            PluginDispatchCandidate(
                binding_id=binding.id,
                plugin_id=plugin.id if plugin else 0,
                plugin_code=plugin_code,
                plugin_type=discovered.manifest.plugin_type,
                priority=discovered.capability.priority,
                consume_policy=discovered.capability.consume_policy,
                timeout_ms=discovered.capability.timeout_ms,
                entrypoint_name=discovered.entrypoint_name,
                commands=matcher.commands,
                keywords=matcher.keywords,
                require_mention=matcher.require_mention,
                scope_status=scope_status,
                scope_enabled=scope_enabled,
                scope_config_override_json=_coerce_optional_text(getattr(scope, "config_override_json", None)),
            )
        )

    candidates.sort(key=lambda item: (-item.priority, item.binding_id))
    return PluginDispatchPlan(context=context, candidates=tuple(candidates), skipped=tuple(skipped))


def build_dispatch_report(
    db: Session,
    channel: Channel,
    normalized_event: dict[str, Any],
    *,
    raw_event_id: int | None = None,
    conversation_id: int | None = None,
    message_id: int | None = None,
    skip_ai_intent: bool = False,
) -> PluginDispatchReport:
    """skip_ai_intent:命令已被群功能处理时为 True,不再交给 AI 兜底重复响应。"""

    static_plan = build_static_dispatch_plan(db, channel, normalized_event)
    logger.info(
        "building dispatch report",
        extra={
            "channel_id": channel.id,
            "channel_type": channel.channel_type,
            "candidate_plugins": list(static_plan.candidate_plugin_codes),
            "skipped_count": len(static_plan.skipped),
        },
    )
    decisions = evaluate_dynamic_matches(db, static_plan, normalized_event)
    # 规则没有任何插件命中时，把消息交给 AI 核心调度：只入队一个独立的意图任务就返回，
    # 模型调用不在调度主链里同步等待。意图任务完成后会把改写好的命令作为新的
    # plugin_dispatch 任务重新入队，仍走本函数的同一条匹配与执行链。
    if not skip_ai_intent and not any(item.matched for item in decisions):
        _enqueue_ai_intent(db, static_plan, normalized_event, raw_event_id=raw_event_id, conversation_id=conversation_id, message_id=message_id)
    executions = execute_dispatch_plan(
        db,
        static_plan,
        decisions,
        normalized_event,
        raw_event_id=raw_event_id,
        conversation_id=conversation_id,
        message_id=message_id,
    )
    stop_reason = next((item.message for item in executions if item.debug.get("dispatch_stopped")), None)
    logger.info(
        "dispatch report built",
        extra={
            "channel_id": channel.id,
            "matched_plugins": list(item.candidate.plugin_code for item in decisions if item.matched),
            "executed_plugins": list(item.candidate.plugin_code for item in executions if item.attempted),
            "stop_reason": stop_reason or "",
        },
    )
    return PluginDispatchReport(
        static_plan=static_plan,
        decisions=tuple(decisions),
        executions=tuple(executions),
        stop_reason=stop_reason,
    )


def _enqueue_ai_intent(
    db: Session,
    static_plan: PluginDispatchPlan,
    normalized_event: Mapping[str, Any],
    *,
    raw_event_id: int | None,
    conversation_id: int | None,
    message_id: int | None,
) -> bool:
    """满足 AI 触发条件时入队意图任务；这里只做不访问网络的轻量判断。

    目录、额度和模型调用都放到独立任务里，调度主链不会被慢模型拖住。
    """

    from app.services import ai_dispatch
    from app.services.task_queue import AI_INTENT_JOB, enqueue_job

    context = static_plan.context
    try:
        details = normalized_event.get("details") or {}
        if details.get("ai_rewritten"):
            # 正文已经是 AI 改写出的命令，再不命中就直接放弃，不再二次分析。
            return False
        if details.get("from_self"):
            # 本账号自己发的消息(含机器人回复回流)不是用户在向 AI 提问,不进 AI;
            # 否则私聊里 AI 会对着自己的回复继续回复。普通插件命令不受影响,仍可用本账号手动触发。
            return False
        if context.message_type != "text" or not ai_dispatch.should_trigger(
            context.chat_type, context.mentioned_self, context.message_text
        ):
            return False
        # 本会话的 AI 解析:租户总开关、群里独立的模型/聊天开关都在这里合并判断。
        resolved = ai_dispatch.resolve_for_conversation(
            db,
            channel_id=context.channel_id,
            chat_type=context.chat_type,
            conversation_external_id=context.conversation_external_id,
        )
        if resolved is None:
            return False
        # 目录只包含本群当前可用的插件，模型看不到也调不到未启用的能力；
        # 没有可调度功能且本会话没开聊天兜底时不入队，开了聊天兜底则直接进入聊天。
        catalog = ai_dispatch.build_catalog([item.plugin_code for item in static_plan.candidates])
        if not catalog and not resolved.chat_enabled:
            return False
        # 同一条消息最多产生一个意图任务，重试和重复投递不会重复调用模型。
        dedupe_key = f"ai_intent:{context.channel_id}:{raw_event_id or message_id or 0}"
        if db.scalar(select(TaskJob.id).where(TaskJob.dedupe_key == dedupe_key).limit(1)):
            return False
        enqueue_job(
            db,
            job_type=AI_INTENT_JOB,
            payload={
                "channel_id": context.channel_id,
                "raw_event_id": raw_event_id,
                "conversation_id": conversation_id,
                "message_id": message_id,
                "normalized_event": dict(normalized_event),
            },
            priority=100,
            related_type="raw_webhook_event",
            related_id=raw_event_id,
            dedupe_key=dedupe_key,
            # 模型失败不自动重试：重试会重复计费，也可能让用户收到迟到的结果。
            max_attempts=1,
        )
        return True
    except Exception:
        logger.exception("ai intent enqueue failed", extra={"channel_id": context.channel_id})
        return False


def send_ai_reply(
    db: Session,
    channel: Channel,
    normalized_event: Mapping[str, Any],
    text: str,
    *,
    conversation_id: int | None,
) -> None:
    """AI 核心向用户回一句话（缺参数时的追问）；复用插件自动出站链，保持引用等统一行为。"""

    if not text or not conversation_id:
        return
    static_context = _build_dispatch_context(channel, dict(normalized_event))
    directory_context = DirectoryPluginContext(
        plugin_code="ai_dispatch",
        binding_id=0,
        bot_id=static_context.bot_id,
        channel_id=static_context.channel_id,
        channel_type=static_context.channel_type,
        chat_type=static_context.chat_type,
        message_type=static_context.message_type,
        command_prefix=static_context.command_prefix,
        content=static_context.message_text,
        sender_id=static_context.sender_id,
        conversation_external_id=static_context.conversation_external_id,
        mentioned_self=static_context.mentioned_self,
        normalized_event=normalized_event,
        conversation_id=conversation_id,
    )
    _dispatch_auto_outbound_messages(db, static_context, directory_context, {"reply_text": text})


def evaluate_dynamic_matches(
    db: Session,
    static_plan: PluginDispatchPlan,
    normalized_event: Mapping[str, Any],
) -> tuple[PluginMatchDecision, ...]:
    decisions: list[PluginMatchDecision] = []
    for candidate in static_plan.candidates:
        binding = db.get(BotPlugin, candidate.binding_id)
        discovered = get_discovered_plugin(candidate.plugin_code)
        if not binding or binding.status != "enabled" or not discovered:
            decisions.append(
                PluginMatchDecision(candidate=candidate, matched=False, reason="candidate_context_missing")
            )
            continue

        context = _build_directory_context(
            static_plan.context,
            candidate,
            binding,
            normalized_event,
            raw_event_id=None,
            conversation_id=None,
            message_id=None,
        )
        decision = _dynamic_match_candidate(candidate, discovered, context)
        decisions.append(decision)
        logger.debug(
            "dynamic match evaluated",
            extra={
                "plugin_code": candidate.plugin_code,
                "matched": decision.matched,
                "reason": decision.reason,
                "command_name": decision.debug.get("command_name") or "",
                "command_hit": bool(decision.debug.get("command_hit")),
                "matched_keyword": decision.debug.get("matched_keyword") or "",
                "custom_matcher_used": bool(decision.debug.get("custom_matcher_used")),
            },
        )
    return tuple(decisions)


def execute_dispatch_plan(
    db: Session,
    static_plan: PluginDispatchPlan,
    decisions: tuple[PluginMatchDecision, ...],
    normalized_event: Mapping[str, Any],
    *,
    raw_event_id: int | None,
    conversation_id: int | None,
    message_id: int | None,
) -> tuple[PluginDispatchExecution, ...]:
    executions: list[PluginDispatchExecution] = []
    dispatch_stopped = False
    stopped_by_plugin_code: str | None = None
    stopped_by_policy: str | None = None

    for decision in decisions:
        candidate = decision.candidate
        if not decision.matched:
            logger.debug("plugin not matched", extra={"plugin_code": candidate.plugin_code, "reason": decision.reason})
            executions.append(
                PluginDispatchExecution(
                    candidate=candidate,
                    attempted=False,
                    ok=False,
                    status="skipped",
                    message=decision.reason,
                    debug={"stage": "dynamic-match", "decision": dict(decision.debug)},
                )
            )
            continue
        if dispatch_stopped:
            logger.debug(
                "plugin execution blocked by previous consume policy",
                extra={
                    "plugin_code": candidate.plugin_code,
                    "blocked_by_plugin_code": stopped_by_plugin_code or "",
                    "blocked_by_policy": stopped_by_policy or "",
                },
            )
            executions.append(
                PluginDispatchExecution(
                    candidate=candidate,
                    attempted=False,
                    ok=False,
                    status="blocked",
                    message="上一个插件已按 consume_policy 截断后续调度。",
                    debug={
                        "dispatch_stopped": True,
                        "consume_policy": candidate.consume_policy,
                        "blocked_by_plugin_code": stopped_by_plugin_code,
                        "blocked_by_policy": stopped_by_policy,
                    },
                )
            )
            continue

        binding = db.get(BotPlugin, candidate.binding_id)
        if not binding or binding.status != "enabled":
            logger.debug("plugin execution skipped because binding missing", extra={"plugin_code": candidate.plugin_code})
            executions.append(
                PluginDispatchExecution(
                    candidate=candidate,
                    attempted=False,
                    ok=False,
                    status="error",
                    message="找不到 bot 插件绑定。",
                )
            )
            continue

        execution = _execute_candidate(
            db,
            static_plan.context,
            candidate,
            binding,
            normalized_event,
            raw_event_id=raw_event_id,
            conversation_id=conversation_id,
            message_id=message_id,
            command_name=decision.debug.get("command_name"),
            matched_keyword=decision.debug.get("matched_keyword"),
        )
        executions.append(execution)
        logger.debug(
            "plugin execution finished",
            extra={
                "plugin_code": candidate.plugin_code,
                "attempted": execution.attempted,
                "ok": execution.ok,
                "status": execution.status,
                "execution_message": execution.message[:200],
            },
        )
        if _should_stop_after_execution(candidate.consume_policy, execution):
            dispatch_stopped = True
            stopped_by_plugin_code = candidate.plugin_code
            stopped_by_policy = _normalize_consume_policy(candidate.consume_policy)
            executions[-1] = PluginDispatchExecution(
                candidate=execution.candidate,
                attempted=execution.attempted,
                ok=execution.ok,
                status=execution.status,
                message=execution.message,
                debug={
                    **execution.debug,
                    "dispatch_stopped": True,
                    "dispatch_stop_policy": stopped_by_policy,
                },
            )

    return tuple(executions)


def serialize_dispatch_plan(plan: PluginDispatchPlan) -> dict[str, Any]:
    return {
        "context": {
            "bot_id": plan.context.bot_id,
            "channel_id": plan.context.channel_id,
            "channel_type": plan.context.channel_type,
            "chat_type": plan.context.chat_type,
            "message_type": plan.context.message_type,
            "command_prefix": plan.context.command_prefix,
            "sender_id": plan.context.sender_id,
            "conversation_external_id": plan.context.conversation_external_id,
            "mentioned_self": plan.context.mentioned_self,
        },
        "candidate_plugin_codes": list(plan.candidate_plugin_codes),
        "candidates": [
            {
                "binding_id": item.binding_id,
                "plugin_id": item.plugin_id,
                "plugin_code": item.plugin_code,
                "plugin_type": item.plugin_type,
                "priority": item.priority,
                "consume_policy": item.consume_policy,
                "timeout_ms": item.timeout_ms,
                "entrypoint_name": item.entrypoint_name,
                "commands": list(item.commands),
                "keywords": list(item.keywords),
                "require_mention": item.require_mention,
            }
            for item in plan.candidates
        ],
        "skipped": [{"plugin_code": code, "reason": reason} for code, reason in plan.skipped],
    }


def serialize_dispatch_report(report: PluginDispatchReport) -> dict[str, Any]:
    return {
        "static": serialize_dispatch_plan(report.static_plan),
        "matched_plugin_codes": list(report.matched_plugin_codes),
        "executed_plugin_codes": list(report.executed_plugin_codes),
        "decisions": [
            {
                "plugin_code": item.candidate.plugin_code,
                "matched": item.matched,
                "reason": item.reason,
                "debug": dict(item.debug),
            }
            for item in report.decisions
        ],
        "executions": [
            {
                "plugin_code": item.candidate.plugin_code,
                "attempted": item.attempted,
                "ok": item.ok,
                "status": item.status,
                "message": item.message,
                "debug": dict(item.debug),
            }
            for item in report.executions
        ],
        "stop_reason": report.stop_reason,
    }


class _ModuleFunctionAdapter:
    def __init__(self, module: Any):
        self._module = module

    def matches_event(self, context: DirectoryPluginContext) -> bool | Mapping[str, Any]:
        matcher = getattr(self._module, "matches_event", None)
        if not callable(matcher):
            return True
        return matcher(context)

    def handle_event(self, context: DirectoryPluginContext) -> DirectoryPluginHandleResult | Mapping[str, Any]:
        handler = getattr(self._module, "handle_event")
        return handler(context)

    def execute_action(self, request: Any) -> Any:
        handler = getattr(self._module, "execute_action")
        return handler(request)

    def execute_message_action(self, db: Session, request: Any) -> Any:
        """把消息动作交给插件显式入口；普通插件继续走原动作函数。"""

        handler = getattr(self._module, "execute_message_action", None)
        if callable(handler):
            return handler(db, request)
        return self.execute_action(request)

    def execute_scheduled_action(self, db: Session, request: Any) -> Any:
        """把统一调度请求转给插件模块；未声明入口时返回明确不支持。"""

        handler = getattr(self._module, "execute_scheduled_action", None)
        if not callable(handler):
            return {
                "ok": False,
                "status": "unsupported",
                "message": "目录插件未提供 execute_scheduled_action 定时入口。",
            }
        return handler(db, request)


def _build_dispatch_context(channel: Channel, normalized_event: dict[str, Any]) -> PluginDispatchContext:
    conversation = normalized_event.get("conversation") or {}
    message = normalized_event.get("message") or {}
    details = normalized_event.get("details") or {}
    return PluginDispatchContext(
        tenant_id=int(channel.tenant_id),
        bot_id=channel.bot_id,
        channel_id=channel.id,
        channel_type=channel.channel_type,
        chat_type=str(conversation.get("chat_type") or "direct"),
        message_type=str(message.get("message_type") or "text"),
        # SaaS 在目录层统一解析斜杠和井号命令，插件只声明业务命令名称，不能各自
        # 维护一套前缀或把“#命令”伪装成第二个兼容命令。
        command_prefix="/",
        sender_id=_coerce_optional_text(message.get("sender_id")),
        conversation_external_id=_coerce_optional_text(conversation.get("external_id")),
        message_text=str(message.get("content") or ""),
        mentioned_self=bool(details.get("mentioned_self") or details.get("is_at_me")),
        attachments=_extract_event_attachments(normalized_event),
    )


def _resolve_effective_matcher(binding: BotPlugin, discovered: DiscoveredPlugin) -> PluginMatcher:
    config = _parse_config_json(binding.config_json)
    commands = _normalize_text_tuple(config.get("commands")) or discovered.matcher.commands
    keywords = _normalize_text_tuple(config.get("keywords")) or discovered.matcher.keywords
    require_mention = bool(config.get("require_mention", discovered.matcher.require_mention))
    return PluginMatcher(commands=commands, keywords=keywords, require_mention=require_mention)


def _dynamic_match_candidate(
    candidate: PluginDispatchCandidate,
    discovered: DiscoveredPlugin,
    context: DirectoryPluginContext,
) -> PluginMatchDecision:
    message_text = context.content.strip()
    command_name = _extract_command_name(message_text, context.command_prefix)
    command_hit = _command_matches(candidate.commands, command_name, message_text, context.command_prefix)
    matched_keyword = _find_keyword(candidate.keywords, message_text)
    mention_ok = (not candidate.require_mention) or context.mentioned_self

    debug = {
        "command_name": command_name,
        "command_hit": command_hit,
        "matched_keyword": matched_keyword,
        "mentioned_self": context.mentioned_self,
        "require_mention": candidate.require_mention,
        "plugin_type": candidate.plugin_type,
    }

    if candidate.plugin_type in SKIP_MESSAGE_PLUGIN_TYPES:
        return PluginMatchDecision(candidate=candidate, matched=False, reason="non_message_plugin", debug=debug)
    if not mention_ok:
        return PluginMatchDecision(candidate=candidate, matched=False, reason="mention_required", debug=debug)

    if _strict_command_mode_applies(candidate):
        matched = command_hit
        reason = "strict_command_hit" if matched else "strict_command_mismatch"
        loaded = load_directory_plugin(discovered.manifest.code)
        if matched and loaded and loaded.supports_matcher and loaded.instance:
            custom = loaded.instance.matches_event(context)
            custom_matched, custom_reason, custom_debug = _normalize_match_result(custom)
            debug = {**debug, **custom_debug, "custom_matcher_used": True}
            if not custom_matched:
                return PluginMatchDecision(candidate=candidate, matched=False, reason=custom_reason, debug=debug)
            reason = custom_reason or reason
        if matched_keyword:
            debug["matched_keyword"] = matched_keyword
        return PluginMatchDecision(candidate=candidate, matched=matched, reason=reason, debug=debug)

    matched = False
    reason = "default_match"
    if candidate.plugin_type == "observer":
        matched = True
        reason = "observer_always"
    elif candidate.plugin_type == "command":
        matched = command_hit
        reason = "command_hit" if matched else "command_mismatch"
    elif candidate.plugin_type == "rule":
        matched = bool(matched_keyword or command_hit)
        reason = "rule_hit" if matched else "rule_mismatch"
    elif candidate.plugin_type == "ai":
        matched = bool(command_hit or context.mentioned_self or context.chat_type == "direct")
        if matched:
            if command_hit:
                reason = "ai_command_hit"
            elif context.mentioned_self:
                reason = "ai_mention_hit"
            else:
                reason = "ai_direct_chat"
        else:
            reason = "ai_no_trigger"
    else:
        if candidate.commands:
            matched = command_hit
            reason = "command_hit" if matched else "command_mismatch"
        elif candidate.keywords:
            matched = bool(matched_keyword)
            reason = "keyword_hit" if matched else "keyword_mismatch"
        else:
            matched = True
            reason = "message_default"

    loaded = load_directory_plugin(discovered.manifest.code)
    if matched and loaded and loaded.supports_matcher and loaded.instance:
        custom = loaded.instance.matches_event(context)
        custom_matched, custom_reason, custom_debug = _normalize_match_result(custom)
        debug = {**debug, **custom_debug, "custom_matcher_used": True}
        if not custom_matched:
            return PluginMatchDecision(candidate=candidate, matched=False, reason=custom_reason, debug=debug)
        reason = custom_reason or reason

    if matched_keyword:
        debug["matched_keyword"] = matched_keyword
    return PluginMatchDecision(candidate=candidate, matched=matched, reason=reason, debug=debug)


def _execute_candidate(
    db: Session,
    static_context: PluginDispatchContext,
    candidate: PluginDispatchCandidate,
    binding: BotPlugin,
    normalized_event: Mapping[str, Any],
    *,
    raw_event_id: int | None,
    conversation_id: int | None,
    message_id: int | None,
    command_name: str | None,
    matched_keyword: str | None,
) -> PluginDispatchExecution:
    started_at = time.perf_counter()
    context = _build_directory_context(
        static_context,
        candidate,
        binding,
        normalized_event,
        raw_event_id=raw_event_id,
        conversation_id=conversation_id,
        message_id=message_id,
        command_name=command_name,
        matched_keyword=matched_keyword,
    )
    loaded = load_directory_plugin(candidate.plugin_code)
    from app.services.plugin_runtime.registry import get_plugin_runtime_definition
    from app.services.plugin_runtime.service import execute_binding_message_action

    runtime_definition = get_plugin_runtime_definition(candidate.plugin_code)
    timeout_debug = _build_timeout_debug(candidate.timeout_ms)
    logger.info(
        "executing plugin candidate",
        extra={
            "plugin_code": candidate.plugin_code,
            "binding_id": candidate.binding_id,
            "channel_id": static_context.channel_id,
            "conversation_id": conversation_id or 0,
            "message_id": message_id or 0,
            "command_name": command_name or "-",
        },
    )
    typing_heartbeat = _start_channel_typing_indicator(
        static_context.tenant_id,
        static_context.channel_id,
        conversation_id,
    )

    try:
        if loaded and loaded.supports_handler and loaded.instance:
            try:
                result = _run_with_timeout(lambda: loaded.instance.handle_event(context), candidate.timeout_ms)
            except PluginExecutionTimeoutError:
                logger.error("directory handler execution timed out", extra={"plugin_code": candidate.plugin_code})
                return PluginDispatchExecution(
                    candidate=candidate,
                    attempted=True,
                    ok=False,
                    status="timeout",
                    message=f"目录插件执行超时（{candidate.timeout_ms}ms）。",
                    debug={
                        "execution_source": "directory_handler_timeout",
                        "duration_ms": int((time.perf_counter() - started_at) * 1000),
                        **timeout_debug,
                    },
                )
            except Exception as exc:
                logger.exception("directory handler execution failed", extra={"plugin_code": candidate.plugin_code})
                return PluginDispatchExecution(
                    candidate=candidate,
                    attempted=True,
                    ok=False,
                    status="error",
                    message=f"目录插件执行异常: {exc}",
                    debug={
                        "execution_source": "directory_handler_executed",
                        "duration_ms": int((time.perf_counter() - started_at) * 1000),
                        **timeout_debug,
                    },
                )

            normalized = _normalize_handle_result(result)
            auto_outbound = _dispatch_auto_outbound_messages(
                db,
                static_context,
                context,
                normalized.output,
            )
            logger.info(
                "directory handler executed",
                extra={
                    "plugin_code": candidate.plugin_code,
                    "ok": normalized.ok,
                    "status": normalized.status,
                    "duration_ms": int((time.perf_counter() - started_at) * 1000),
                    "command_name": command_name or "-",
                    # 结果说明(中文),失败时能直接看出原因。
                    "message_preview": str(normalized.message or "")[:120].replace("\n", " "),
                },
            )
            if auto_outbound and any(not item.get("ok") for item in auto_outbound):
                return PluginDispatchExecution(
                    candidate=candidate,
                    attempted=True,
                    ok=False,
                    status="outbound_failed",
                    message=f"{normalized.message} (auto outbound failed)",
                    debug={
                        "execution_source": "directory_handler_executed",
                        "duration_ms": int((time.perf_counter() - started_at) * 1000),
                        **timeout_debug,
                        "consume_policy": candidate.consume_policy,
                        "output": redact_base64_fields(dict(normalized.output)),
                        "auto_outbound": auto_outbound,
                    },
                )
            return PluginDispatchExecution(
                candidate=candidate,
                attempted=True,
                ok=normalized.ok,
                status=normalized.status,
                message=normalized.message,
                debug={
                    "execution_source": "directory_handler_executed",
                    "duration_ms": int((time.perf_counter() - started_at) * 1000),
                    **timeout_debug,
                    "consume_policy": candidate.consume_policy,
                    "output": redact_base64_fields(dict(normalized.output)),
                    "auto_outbound": auto_outbound,
                },
            )

        if runtime_definition and runtime_definition.supports_execution:
            runtime_payload = _build_runtime_bridge_payload(context)
            channel = db.get(Channel, static_context.channel_id)
            if channel:
                runtime_payload["channel_api_base"] = str(channel.api_base or "").strip()
                runtime_payload["channel_adapter_config"] = _parse_config_json(channel.adapter_config_json)
            try:
                runtime_result = _run_with_timeout(
                    lambda: execute_binding_message_action(
                        db,
                        binding,
                        "execute",
                        payload=runtime_payload,
                        context=_build_runtime_execution_context(static_context, context),
                    ),
                    candidate.timeout_ms,
                )
            except PluginExecutionTimeoutError:
                logger.error("runtime executor timed out", extra={"plugin_code": candidate.plugin_code})
                return PluginDispatchExecution(
                    candidate=candidate,
                    attempted=True,
                    ok=False,
                    status="timeout",
                    message=f"runtime executor 执行超时（{candidate.timeout_ms}ms）。",
                    debug={
                        "execution_source": "runtime_executor_timeout",
                        "duration_ms": int((time.perf_counter() - started_at) * 1000),
                        **timeout_debug,
                        "runtime_payload": redact_base64_fields(runtime_payload),
                    },
                )
            if runtime_result is not None:
                _apply_runtime_config_patch(binding, runtime_result.output)
                auto_outbound = _dispatch_auto_outbound_messages(
                    db,
                    static_context,
                    context,
                    runtime_result.output,
                )
                logger.info(
                    "runtime executor executed",
                    extra={
                        "plugin_code": candidate.plugin_code,
                        "ok": runtime_result.ok,
                        "status": runtime_result.status,
                    },
                )
                if auto_outbound and any(not item.get("ok") for item in auto_outbound):
                    return PluginDispatchExecution(
                        candidate=candidate,
                        attempted=True,
                        ok=False,
                        status="outbound_failed",
                        message=f"{runtime_result.message} (auto outbound failed)",
                        debug={
                            "execution_source": "runtime_executor_executed",
                            "duration_ms": int((time.perf_counter() - started_at) * 1000),
                            **timeout_debug,
                            "consume_policy": candidate.consume_policy,
                            "output": redact_base64_fields(dict(runtime_result.output)),
                            "runtime_payload": redact_base64_fields(runtime_payload),
                            "auto_outbound": auto_outbound,
                        },
                    )
                return PluginDispatchExecution(
                    candidate=candidate,
                    attempted=True,
                    ok=runtime_result.ok,
                    status=runtime_result.status,
                    message=runtime_result.message,
                    debug={
                        "execution_source": "runtime_executor_executed",
                        "duration_ms": int((time.perf_counter() - started_at) * 1000),
                        **timeout_debug,
                        "consume_policy": candidate.consume_policy,
                        "output": redact_base64_fields(dict(runtime_result.output)),
                        "runtime_payload": redact_base64_fields(runtime_payload),
                        "auto_outbound": auto_outbound,
                    },
                )
        logger.warning("plugin candidate had no executable path", extra={"plugin_code": candidate.plugin_code})

        return PluginDispatchExecution(
            candidate=candidate,
            attempted=False,
            ok=False,
            status="invalid",
            message="插件缺少真实执行入口，已拒绝执行。",
            debug={
                "execution_source": "invalid_plugin",
                "duration_ms": int((time.perf_counter() - started_at) * 1000),
                **timeout_debug,
                "entrypoint_name": candidate.entrypoint_name,
            },
        )
    finally:
        if typing_heartbeat is not None:
            typing_heartbeat.stop()


class _TypingHeartbeat:
    def __init__(self, tenant_id: int, channel_id: int, conversation_id: int) -> None:
        self.tenant_id = tenant_id
        self.channel_id = channel_id
        self.conversation_id = conversation_id
        self._stop_event = threading.Event()
        self._thread = threading.Thread(target=self._run, name=f"typing-heartbeat-{channel_id}-{conversation_id}", daemon=True)

    def start(self) -> None:
        self._thread.start()

    def stop(self) -> None:
        self._stop_event.set()
        self._thread.join(timeout=1)

    def _run(self) -> None:
        while not self._stop_event.is_set():
            _emit_channel_typing_indicator_once(self.tenant_id, self.channel_id, self.conversation_id)
            if self._stop_event.wait(TELEGRAM_TYPING_HEARTBEAT_INTERVAL_SECONDS):
                return


def _start_channel_typing_indicator(
    tenant_id: int,
    channel_id: int,
    conversation_id: int | None,
) -> _TypingHeartbeat | None:
    if not conversation_id:
        return None
    heartbeat = _TypingHeartbeat(int(tenant_id or 0), int(channel_id or 0), int(conversation_id or 0))
    heartbeat.start()
    return heartbeat


def _emit_channel_typing_indicator_once(tenant_id: int, channel_id: int, conversation_id: int | None) -> None:
    if not conversation_id:
        return
    from app.services.channel_adapters.service import dispatch_channel_action

    try:
        # ContextVar 不会自动传播到新线程，因此心跳对象显式携带由父任务确认的
        # tenant_id，并在每次发送前重建租户作用域和独立会话。
        with tenant_scope(tenant_id), tenant_session(TenantSessionLocal) as db:
            channel = db.get(Channel, int(channel_id or 0))
            conversation = db.get(Conversation, int(conversation_id or 0))
            if not channel or not conversation:
                return
            result = dispatch_channel_action(channel, conversation, action="typing")
    except Exception:
        logger.debug(
            "channel typing indicator failed",
            extra={"channel_id": channel_id, "conversation_id": conversation_id},
        )
        return
    if not bool(result.get("ok")):
        logger.debug(
            "channel typing indicator skipped",
            extra={
                "channel_id": channel_id,
                "conversation_id": conversation_id,
                "status": result.get("status"),
                "error_code": result.get("error_code"),
            },
        )


def _dispatch_auto_outbound_messages(
    db: Session,
    static_context: PluginDispatchContext,
    context: DirectoryPluginContext,
    output: Mapping[str, Any],
) -> list[dict[str, Any]]:
    payloads = _build_auto_outbound_payloads(context, output)
    if not payloads:
        logger.debug("no auto outbound payloads generated", extra={"plugin_code": context.plugin_code})
        return []

    details = context.normalized_event.get("details") if isinstance(context.normalized_event, Mapping) else None
    dry_run_outbound = bool(details.get("dry_run_outbound")) if isinstance(details, Mapping) else False
    if dry_run_outbound:
        logger.info(
            "auto outbound skipped because dry run is enabled",
            extra={"plugin_code": context.plugin_code, "payload_count": len(payloads)},
        )
        return [
            {
                "ok": True,
                "dry_run": True,
                "message_id": None,
                "payload": {
                    "conversation_id": context.conversation_id,
                    "external_id": context.conversation_external_id,
                    "receiver_id": context.conversation_external_id,
                    "chat_type": context.chat_type,
                    **item,
                },
                "adapter_result": {
                    "ok": True,
                    "provider": "lab_dry_run",
                    "transport_status": "skipped",
                    "status": "dry_run",
                    "external_status": "dry_run",
                },
            }
            for item in payloads
        ]

    from app.services.channel_adapters import dispatch_outbound_message
    from app.services.task_queue import enqueue_job
    from app.services.workwechat_cloud_poll import notify_cloud_job_enqueued, resolve_outbound_job_type

    channel = db.get(Channel, static_context.channel_id)
    if not channel:
        logger.error("auto outbound failed because channel was not found", extra={"plugin_code": context.plugin_code})
        return [{"ok": False, "error": "channel_not_found"}]

    conversation = db.get(Conversation, context.conversation_id) if context.conversation_id else None
    if not conversation:
        logger.error("auto outbound failed because conversation was not found", extra={"plugin_code": context.plugin_code})
        return [{"ok": False, "error": "conversation_not_found"}]

    results: list[dict[str, Any]] = []
    logger.info(
        "dispatching auto outbound payloads",
        extra={"plugin_code": context.plugin_code, "payload_count": len(payloads), "channel_id": channel.id},
    )
    for item in payloads:
        payload = {
            "conversation_id": conversation.id,
            "external_id": context.conversation_external_id,
            "receiver_id": context.conversation_external_id,
            "chat_type": context.chat_type,
            **item,
        }
        if channel.channel_type == "wechat_webridge":
            # 微桥协议只接受路径或 URL:base64 先落成文件,库、队列和日志只保留文件地址;
            # 再由协议层把统一消息类型映射成微桥原生命令(send_image/send_video 等),手机 poll 直接领取。
            payload = externalize_media_payload(
                payload,
                tenant_id=int(channel.tenant_id),
                channel_id=int(channel.id),
                public_base_url=str(settings.public_base_url or ""),
            )
            if any(str(key).lower().endswith("base64") and value for key, value in payload.items()):
                logger.error("auto outbound media dropped because public base url is missing", extra={"plugin_code": context.plugin_code})
                results.append({"ok": False, "error": "public_base_url_missing", "payload": redact_base64_fields(payload)})
                continue
            # 微桥没有"出站 worker"这一步(手机直接 poll 命令),所以必须在入队的同时经统一出站函数留下消息记录,
            # 否则消息中心和统计里完全看不到机器人发出的消息(之前直接入队命令,message_id 一直是 None)。
            message, adapter_result = dispatch_outbound_message(db, channel, conversation, payload)
            if not adapter_result.get("ok"):
                error = str(adapter_result.get("error_message") or adapter_result.get("error_code") or "webridge_send_failed")
                logger.error("auto outbound webridge send failed", extra={"plugin_code": context.plugin_code, "error": error})
                results.append({"ok": False, "error": error, "message_id": int(message.id), "payload": redact_base64_fields(payload)})
                continue
            results.append(
                {
                    "ok": True,
                    "queued": True,
                    "job_id": adapter_result.get("job_id"),
                    "message_id": int(message.id),
                    "payload": payload,
                    "adapter_result": {
                        "ok": True,
                        "provider": "queued",
                        "transport_status": "queued",
                        "status": "queued",
                        "external_status": "pending",
                    },
                }
            )
            continue
        else:
            job = enqueue_job(
                db,
                job_type=resolve_outbound_job_type(channel),
                payload={
                    "channel_id": channel.id,
                    "conversation_id": conversation.id,
                    "payload": payload,
                    "plugin_code": context.plugin_code,
                },
                priority=90,
                related_type="conversation",
                related_id=conversation.id,
            )
        notify_cloud_job_enqueued(channel, job)
        result_item = {
            "ok": True,
            "queued": True,
            "job_id": job.id,
            "message_id": None,
            "payload": payload,
            "adapter_result": {
                "ok": True,
                "provider": "queued",
                "transport_status": "queued",
                "status": "queued",
                "external_status": "pending",
            },
        }
        results.append(result_item)
    logger.info(
        "auto outbound dispatch completed",
        extra={"plugin_code": context.plugin_code, "result_count": len(results)},
    )
    return results


def _apply_runtime_config_patch(binding: BotPlugin, output: Mapping[str, Any]) -> None:
    if not isinstance(output, Mapping):
        return
    config_patch = output.get("config_patch")
    if not isinstance(config_patch, Mapping):
        return
    current = _parse_config_json(binding.config_json)
    merged = _deep_merge_config(current, dict(config_patch))
    binding.config_json = json.dumps(merged, ensure_ascii=False, sort_keys=True)


def _deep_merge_config(current: dict[str, Any], patch: dict[str, Any]) -> dict[str, Any]:
    merged = dict(current)
    for key, value in patch.items():
        existing = merged.get(key)
        if isinstance(existing, dict) and isinstance(value, dict):
            merged[key] = _deep_merge_config(existing, value)
        else:
            merged[key] = value
    return merged


def _build_auto_outbound_payloads(
    context: DirectoryPluginContext,
    output: Mapping[str, Any],
) -> list[dict[str, Any]]:
    payloads = _collect_outbound_payloads(context, output)
    return _apply_reply_mode(context, output, payloads)


# 回复方式：plain 普通文本；quote 引用触发消息；mention @触发人；both 引用并 @。
REPLY_MODES = ("plain", "quote", "mention", "both")
# 只有微桥协议支持引用与 @，其他渠道保持普通文本，避免把渠道差异散落到插件里。
REPLY_MODE_CHANNEL_TYPES = {"wechat_webridge"}
DEFAULT_REPLY_MODE = "quote"


def _apply_reply_mode(
    context: DirectoryPluginContext,
    output: Mapping[str, Any],
    payloads: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    """按回复方式给第一条文本回复补充引用和 @，插件无需自己拼协议字段。

    优先级：插件输出的 reply_mode > 群级/绑定配置的 reply_mode > 默认值。
    引用只在群聊使用；私聊对话本身一对一，引用和 @ 都是噪音。
    """

    if context.channel_type not in REPLY_MODE_CHANNEL_TYPES or context.chat_type != "group":
        return payloads
    # 成员加入/退出/改昵称等系统事件的 message_id 是适配器合成的,微信里没有对应消息可引用,
    # 引用会被手机端整条拒绝(quote_message_id not found);事件也没有“触发人”可 @,只发普通文本。
    if str(context.message_type or "").startswith("group_member_"):
        return payloads
    mode = str(output.get("reply_mode") or context.binding_config.get("reply_mode") or DEFAULT_REPLY_MODE).strip().lower()
    if mode not in REPLY_MODES or mode == "plain":
        return payloads
    message = context.normalized_event.get("message") if isinstance(context.normalized_event, Mapping) else None
    message = message if isinstance(message, Mapping) else {}
    quote_id = _coerce_optional_text(message.get("external_message_id"))
    sender_id = _coerce_optional_text(context.sender_id)
    for index, item in enumerate(payloads):
        if item.get("message_type") != "text":
            continue
        updated = dict(item)
        if mode in ("quote", "both") and quote_id:
            updated["quote_message_id"] = quote_id
        if mode in ("mention", "both") and sender_id:
            # 正文内联占位符由微桥渲染成 @昵称，并同步写入 @ 名单。
            updated["content"] = "{{mention:%s}} %s" % (sender_id, updated.get("content") or "")
        payloads[index] = updated
        break
    return payloads


def _collect_outbound_payloads(
    context: DirectoryPluginContext,
    output: Mapping[str, Any],
) -> list[dict[str, Any]]:
    if not isinstance(output, Mapping):
        return []

    replies = output.get("replies")
    if isinstance(replies, list):
        normalized = [
            _normalize_reply_item(item, channel_type=context.channel_type)
            for item in replies
            if isinstance(item, Mapping)
        ]
        normalized = [item for item in normalized if item]
        if normalized:
            return normalized

    explicit = output.get("outbound_messages")
    if isinstance(explicit, list):
        payloads = [dict(item) for item in explicit if isinstance(item, Mapping)]
        if payloads:
            return payloads

    payloads: list[dict[str, Any]] = []
    reply_text = _pick_reply_text(output)
    if reply_text:
        payloads.append({"message_type": "text", "content": reply_text})

    for key in ("music_xml", "video_xml", "douyu_xml", "previous_music_xml"):
        xml = str(output.get(key) or "").strip()
        if xml:
            payloads.append(_build_xml_payload(xml, channel_type=context.channel_type))

    return payloads


def _pick_reply_text(output: Mapping[str, Any]) -> str:
    for key in ("reply_text", "answer", "summary", "text", "message"):
        value = output.get(key)
        if value in (None, ""):
            continue
        normalized = str(value).strip()
        if normalized:
            return normalized
    return ""


def _normalize_reply_item(item: Mapping[str, Any], *, channel_type: str = "") -> dict[str, Any] | None:
    reply_type = str(item.get("type") or item.get("message_type") or "").strip().lower()
    if reply_type == "text":
        content = str(item.get("content") or item.get("text") or "").strip()
        return {"message_type": "text", "content": content} if content else None
    if reply_type in {"xml", "card", "app"}:
        xml = str(item.get("xml") or item.get("content") or "").strip()
        if not xml:
            return None
        return _build_xml_payload(xml, channel_type=channel_type)
    if reply_type == "file":
        xml = str(item.get("xml") or "").strip()
        if xml:
            return _build_xml_payload(xml, channel_type=channel_type)
        file_name = str(item.get("file_name") or "").strip()
        file_url = str(item.get("file_url") or item.get("download_url") or item.get("url") or "").strip()
        if file_name and file_url:
            return {"message_type": "file", "file_name": file_name, "file_url": file_url}
        return None
    if reply_type == "image":
        base64_data = str(item.get("base64") or item.get("image_base64") or "").strip()
        if base64_data:
            return {"message_type": "image", "base64": base64_data}
        image_path = str(item.get("image_path") or item.get("local_path") or item.get("path") or "").strip()
        if image_path:
            encoded = _encode_local_file_to_base64(image_path)
            if encoded:
                return {"message_type": "image", "base64": encoded}
        xml = str(item.get("xml") or item.get("cdn_xml") or "").strip()
        if xml:
            return {"message_type": "image", "content": xml}
        return None
    if reply_type == "voice":
        base64_data = str(item.get("base64") or item.get("voice_base64") or item.get("audio_base64") or "").strip()
        if not base64_data:
            return None
        payload: dict[str, Any] = {"message_type": "voice", "base64": base64_data}
        voice_time = item.get("voice_time") or item.get("duration_ms") or item.get("duration")
        if voice_time not in (None, ""):
            payload["voice_time"] = voice_time
        voice_type = item.get("voice_type") or item.get("format_type")
        if voice_type not in (None, ""):
            payload["voice_type"] = voice_type
        return payload
    if reply_type == "video":
        xml = str(item.get("xml") or item.get("cdn_xml") or "").strip()
        if xml:
            return {"message_type": "video", "xml": xml}
        video_base64 = str(item.get("base64") or item.get("video_base64") or "").strip()
        image_base64 = str(item.get("image_base64") or item.get("cover_base64") or item.get("thumb_base64") or "").strip()
        if not video_base64 or not image_base64:
            return None
        payload = {"message_type": "video", "base64": video_base64, "image_base64": image_base64}
        play_length = item.get("play_length") or item.get("duration") or item.get("duration_seconds")
        if play_length not in (None, ""):
            payload["play_length"] = play_length
        return payload
    if reply_type == "link":
        link_url = str(item.get("url") or item.get("link_url") or "").strip()
        if not link_url:
            return None
        title = str(item.get("title") or item.get("link_title") or "").strip()
        description = str(
            item.get("description")
            or item.get("desc")
            or item.get("link_description")
            or ""
        ).strip()
        thumb_url = str(item.get("thumb_url") or item.get("image_url") or "").strip()
        if str(channel_type or "").strip().lower() == "telegram":
            # Telegram 当前适配器只发送文本和图片。链接卡片在目录层转成业务可读文本，
            # 保证插件仍然只输出一种通用链接结构，且不会把渠道能力判断散落到插件中。
            text_parts = [part for part in (title, description, link_url) if part]
            return {"message_type": "text", "content": "\n".join(text_parts)}
        payload = {
            "message_type": "link",
            "content": title or link_url,
            "title": title,
            "desc": description,
            "url": link_url,
            "link_url": link_url,
            "link_title": title,
            "link_description": description,
        }
        if thumb_url:
            payload["thumb_url"] = thumb_url
        return payload
    return None


def _build_xml_payload(xml: str, *, channel_type: str) -> dict[str, Any]:
    normalized_channel_type = str(channel_type or "").strip().lower()
    if normalized_channel_type == "telegram":
        fallback_text = _build_xml_text_fallback(xml)
        if fallback_text:
            return {"message_type": "text", "content": fallback_text}
    return {"message_type": "file", "xml": xml}


def _build_xml_text_fallback(xml: str) -> str:
    title = _extract_xml_tag(xml, "title")
    description = _extract_xml_tag(xml, "des")
    play_url = _extract_xml_tag(xml, "dataurl")
    page_url = _extract_xml_tag(xml, "url")
    lines: list[str] = []
    if title:
        lines.append(f"歌曲：{title}")
    if description:
        lines.append(description)
    if play_url:
        lines.append(f"播放链接：{play_url}")
    elif page_url:
        lines.append(f"链接：{page_url}")
    return "\n".join(lines).strip()


def _extract_xml_tag(xml: str, tag_name: str) -> str:
    pattern = rf"<{re.escape(tag_name)}>(.*?)</{re.escape(tag_name)}>"
    match = re.search(pattern, xml, flags=re.IGNORECASE | re.DOTALL)
    if not match:
        return ""
    return unescape(str(match.group(1) or "").strip())


def _encode_local_file_to_base64(path_value: str) -> str | None:
    try:
        raw = Path(path_value).read_bytes()
    except OSError:
        return None
    if not raw:
        return None
    return base64.b64encode(raw).decode("ascii")


def _build_directory_context(
    static_context: PluginDispatchContext,
    candidate: PluginDispatchCandidate,
    binding: BotPlugin,
    normalized_event: Mapping[str, Any],
    *,
    raw_event_id: int | None,
    conversation_id: int | None,
    message_id: int | None,
    command_name: str | None = None,
    matched_keyword: str | None = None,
) -> DirectoryPluginContext:
    discovered = get_discovered_plugin(candidate.plugin_code)
    default_config = dict(discovered.runtime_config) if discovered else {}
    binding_config = _parse_config_json(binding.config_json)
    scope_config = _parse_config_json(candidate.scope_config_override_json)
    secret_fields = set(discovered.secret_fields) if discovered else set()
    # 群作用域和历史普通 JSON 都不能覆盖绑定密文。敏感字段先从非密文配置中删除，
    # 再把当前租户绑定的解密结果放到最终配置末尾。
    for field_name in secret_fields:
        binding_config.pop(field_name, None)
        scope_config.pop(field_name, None)
    secret_config = decrypt_plugin_secret_config(
        tenant_id=int(binding.tenant_id),
        binding_id=int(binding.id),
        encrypted=binding.secret_config_encrypted,
    )
    effective_config = _deep_merge_config(_deep_merge_config(default_config, binding_config), scope_config)
    effective_config.update(secret_config)
    return DirectoryPluginContext(
        plugin_code=candidate.plugin_code,
        binding_id=candidate.binding_id,
        bot_id=static_context.bot_id,
        channel_id=static_context.channel_id,
        channel_type=static_context.channel_type,
        chat_type=static_context.chat_type,
        message_type=static_context.message_type,
        command_prefix=static_context.command_prefix,
        content=static_context.message_text,
        sender_id=static_context.sender_id,
        conversation_external_id=static_context.conversation_external_id,
        mentioned_self=static_context.mentioned_self,
        attachments=static_context.attachments,
        binding_config=effective_config,
        normalized_event=normalized_event,
        raw_event_id=raw_event_id,
        conversation_id=conversation_id,
        message_id=message_id,
        command_name=_coerce_optional_text(command_name),
        matched_keyword=_coerce_optional_text(matched_keyword),
    )


def _build_runtime_bridge_payload(context: DirectoryPluginContext) -> dict[str, Any]:
    command_args = _extract_command_args(context.content, context.command_prefix, context.command_name)
    payload = {
        "content": context.content,
        "query": context.content,
        "message_text": context.content,
        "stripped_content": command_args or context.content,
        "stripped_query": command_args or context.content,
        "stripped_message_text": command_args or context.content,
        "command_name": context.command_name,
        "command_args": command_args,
        "matched_keyword": context.matched_keyword,
        "command_prefix": context.command_prefix,
        "conversation_id": context.conversation_id,
        "conversation_external_id": context.conversation_external_id,
        "message_id": context.message_id,
        "raw_event_id": context.raw_event_id,
        "bot_id": context.bot_id,
        "channel_id": context.channel_id,
        "channel_type": context.channel_type,
        "chat_type": context.chat_type,
        "message_type": context.message_type,
        "mentioned_self": context.mentioned_self,
        "sender_id": context.sender_id,
        "user": context.sender_id or f"bot-{context.bot_id}",
        "normalized_event": dict(context.normalized_event),
        "attachments": [dict(item) for item in context.attachments],
    }
    messages = _extract_runtime_messages(context)
    if messages:
        payload["messages"] = messages
    return payload


def _build_runtime_execution_context(
    static_context: PluginDispatchContext,
    directory_context: DirectoryPluginContext,
) -> "PluginExecutionContext":
    """把已落库的入站消息定位信息转换为插件可信运行上下文。

    插件 payload 仍保留现有业务字段，供插件读取命令和消息内容；权限判断和后续
    通用能力只能依赖这里显式构造的上下文，不能信任 payload 中的同名字段。
    """

    # 运行时包的注册表会反向读取目录插件。这里延迟导入契约类，避免模块初始化
    # 阶段形成 plugin_directory -> plugin_runtime -> registry -> plugin_directory 环路。
    from app.services.plugin_runtime.contracts import PluginExecutionContext

    return PluginExecutionContext(
        tenant_id=static_context.tenant_id,
        binding_id=directory_context.binding_id,
        bot_id=directory_context.bot_id,
        channel_id=directory_context.channel_id,
        conversation_id=directory_context.conversation_id,
        message_id=directory_context.message_id,
        raw_event_id=directory_context.raw_event_id,
        sender_id=directory_context.sender_id,
        channel_type=directory_context.channel_type,
        chat_type=directory_context.chat_type,
        message_type=directory_context.message_type,
    )


def _extract_event_attachments(normalized_event: Mapping[str, Any]) -> tuple[Mapping[str, Any], ...]:
    # 附件信息在入站时已经固化为普通 dict；这里仍做一次形态过滤，避免插件收到不可序列化对象。
    raw_attachments = normalized_event.get("attachments")
    if not isinstance(raw_attachments, list):
        details = normalized_event.get("details")
        if isinstance(details, Mapping):
            raw_attachments = details.get("attachments")
    if not isinstance(raw_attachments, list):
        return ()
    return tuple(dict(item) for item in raw_attachments if isinstance(item, Mapping))


def _extract_runtime_messages(context: DirectoryPluginContext) -> list[str]:
    normalized_event = context.normalized_event
    candidate_keys = ("messages", "recent_messages", "history_messages")
    for key in candidate_keys:
        value = normalized_event.get(key)
        normalized = _normalize_runtime_messages(value)
        if normalized:
            return normalized
    details = normalized_event.get("details")
    if isinstance(details, Mapping):
        for key in candidate_keys:
            normalized = _normalize_runtime_messages(details.get(key))
            if normalized:
                return normalized
    content = context.content.strip()
    return [content] if content else []


def _normalize_runtime_messages(value: Any) -> list[str]:
    if not isinstance(value, (list, tuple)):
        return []
    messages: list[str] = []
    for item in value:
        if isinstance(item, Mapping):
            text = str(item.get("content") or item.get("text") or "").strip()
        else:
            text = str(item).strip()
        if text:
            messages.append(text)
    return messages


def _normalize_consume_policy(value: str | None) -> str:
    normalized = str(value or DEFAULT_CONSUME_POLICY).strip().lower()
    return normalized or DEFAULT_CONSUME_POLICY


def _should_stop_after_execution(policy: str | None, execution: PluginDispatchExecution) -> bool:
    if not execution.attempted or not execution.ok:
        return False
    normalized = _normalize_consume_policy(policy)
    return normalized in STOP_CONSUME_POLICIES


def _normalize_match_result(value: Any) -> tuple[bool, str, dict[str, Any]]:
    if isinstance(value, dict):
        matched = bool(value.get("matched", True))
        reason = str(value.get("reason") or ("custom_match_hit" if matched else "custom_match_rejected"))
        debug = value.get("debug")
        return matched, reason, debug if isinstance(debug, dict) else {}
    return bool(value), "custom_match_hit" if bool(value) else "custom_match_rejected", {}


def _normalize_handle_result(value: Any) -> DirectoryPluginHandleResult:
    if isinstance(value, DirectoryPluginHandleResult):
        return value
    if isinstance(value, dict):
        return DirectoryPluginHandleResult(
            ok=bool(value.get("ok", True)),
            status=str(value.get("status") or ("completed" if value.get("ok", True) else "error")),
            message=str(value.get("message") or "目录插件已处理消息。"),
            output=value.get("output") if isinstance(value.get("output"), dict) else {},
        )
    return DirectoryPluginHandleResult(ok=True, status="completed", message=str(value or "目录插件已处理消息。"))


def _build_timeout_debug(timeout_ms: int) -> dict[str, Any]:
    return {
        "timeout_ms": timeout_ms,
        "timeout_enforced": timeout_ms > 0,
    }


def _run_with_timeout(func: Any, timeout_ms: int) -> Any:
    if timeout_ms <= 0:
        return _await_if_needed(func(), None)

    timeout_seconds = timeout_ms / 1000
    if _supports_signal_timeout():
        return _run_with_signal_timeout(func, timeout_seconds)

    return _await_if_needed(func(), timeout_seconds)


def _supports_signal_timeout() -> bool:
    return hasattr(signal, "setitimer") and threading.current_thread() is threading.main_thread()


def _run_with_signal_timeout(func: Any, timeout_seconds: float) -> Any:
    previous_handler = signal.getsignal(signal.SIGALRM)

    def _handle_timeout(signum: int, frame: Any) -> None:
        raise PluginExecutionTimeoutError("plugin execution timed out")

    signal.signal(signal.SIGALRM, _handle_timeout)
    signal.setitimer(signal.ITIMER_REAL, timeout_seconds)
    try:
        result = func()
        signal.setitimer(signal.ITIMER_REAL, 0)
    except PluginExecutionTimeoutError:
        signal.setitimer(signal.ITIMER_REAL, 0)
        raise
    except Exception:
        signal.setitimer(signal.ITIMER_REAL, 0)
        raise
    finally:
        signal.signal(signal.SIGALRM, previous_handler)

    return _await_if_needed(result, timeout_seconds)


def _await_if_needed(value: Any, timeout_seconds: float | None) -> Any:
    if not inspect.isawaitable(value):
        return value

    async def _wait() -> Any:
        if timeout_seconds is None:
            return await value
        return await asyncio.wait_for(value, timeout_seconds)

    try:
        return asyncio.run(_wait())
    except asyncio.TimeoutError as exc:
        raise PluginExecutionTimeoutError("plugin async execution timed out") from exc


def _extract_command_name(message_text: str, prefix: str) -> str | None:
    normalized = _strip_supported_command_prefix(message_text, prefix)
    if not normalized:
        return None
    first = normalized.split()[0].strip()
    return first or None


def _extract_command_args(message_text: str, prefix: str, command_name: str | None) -> str:
    normalized = _strip_supported_command_prefix(message_text, prefix)
    if not normalized:
        return ""
    if command_name:
        lowered = normalized.lower()
        command_lower = command_name.strip().lower()
        if command_lower and lowered.startswith(command_lower):
            return normalized[len(command_name) :].lstrip()
    parts = normalized.split(maxsplit=1)
    if len(parts) == 2:
        return parts[1].strip()
    return ""


def _command_matches(commands: tuple[str, ...], command_name: str | None, message_text: str, prefix: str) -> bool:
    if not commands:
        return False
    normalized_commands = {item.strip().lower() for item in commands if item.strip()}
    if command_name and command_name.lower() in normalized_commands:
        return True
    normalized_text = message_text.strip().lower()
    for command in normalized_commands:
        if any(normalized_text.startswith(f"{item}{command}") for item in _supported_command_prefixes(prefix)):
            return True
        if normalized_text == command:
            return True
    return False


def _strip_supported_command_prefix(message_text: str, configured_prefix: str) -> str:
    """统一移除平台允许的命令前缀，并保留无前缀命令的现有业务语义。"""

    normalized = str(message_text or "").strip()
    for prefix in _supported_command_prefixes(configured_prefix):
        if normalized.startswith(prefix):
            return normalized[len(prefix) :].lstrip()
    return normalized


def _supported_command_prefixes(configured_prefix: str) -> tuple[str, ...]:
    """返回去重后的平台命令前缀；当前产品只提供斜杠和井号两种入口。"""

    ordered: list[str] = []
    for item in (str(configured_prefix or "").strip(), "/", "#"):
        if item and item not in ordered:
            ordered.append(item)
    return tuple(ordered)


def _find_keyword(keywords: tuple[str, ...], message_text: str) -> str | None:
    lowered = message_text.strip().lower()
    if not lowered:
        return None
    for keyword in keywords:
        normalized = keyword.strip().lower()
        if normalized and normalized in lowered:
            return keyword
    return None


def _is_denied_by_binding_scope(binding: BotPlugin, context: PluginDispatchContext) -> bool:
    config = _parse_config_json(binding.config_json)
    allow_from = _normalize_scope_values(config.get("allow_from"))
    deny_from = _normalize_scope_values(config.get("deny_from"))
    subjects = {item for item in (context.sender_id, context.conversation_external_id) if item}
    if deny_from and subjects & deny_from:
        return True
    if allow_from and not subjects.intersection(allow_from):
        return True
    return False


def _strict_command_mode_applies(candidate: PluginDispatchCandidate) -> bool:
    if candidate.plugin_type == "observer":
        return False
    return bool(candidate.commands)


def _channel_type_matches(channel_type: str, capability_channel_types: tuple[str, ...]) -> bool:
    normalized = (channel_type or "").strip()
    supported = {item.strip() for item in capability_channel_types if item and item.strip()}
    if not supported:
        return True
    if normalized in supported:
        return True
    if normalized == "telegram" and "simulator" in supported:
        return True
    return False


def _normalize_scope_values(raw_value: Any) -> set[str]:
    if isinstance(raw_value, list):
        return {str(item).strip() for item in raw_value if str(item).strip()}
    if isinstance(raw_value, str):
        return {item.strip() for item in raw_value.replace("，", ",").split(",") if item.strip()}
    return set()


def _resolve_entrypoint(plugin_dir: Path) -> Path | None:
    for candidate in ENTRYPOINT_CANDIDATES:
        entrypoint = plugin_dir / candidate
        if entrypoint.exists():
            return entrypoint
    return None


def _build_manifest(default_code: str, parsed: dict[str, Any]) -> PluginManifest:
    plugin_block = parsed.get("plugin", {})
    declared_code = _coerce_optional_text(plugin_block.get("code"))
    code = default_code if declared_code != default_code else declared_code or default_code
    display_name = _resolve_display_name(
        code,
        str(plugin_block.get("display_name") or plugin_block.get("name") or code).strip() or code,
    )
    return PluginManifest(
        code=code,
        display_name=display_name,
        plugin_type=str(plugin_block.get("plugin_type") or "message").strip() or "message",
        version=_coerce_optional_text(plugin_block.get("version")),
        author=_coerce_optional_text(plugin_block.get("author")),
        description=_coerce_optional_text(plugin_block.get("description")),
        default_enabled=bool(plugin_block.get("default_enabled", False)),
    )


# 支持“群启用总开关”和“群内单独启停插件”的渠道。必须与群详情页提供这些开关的渠道一致
# (main.GROUP_SCOPE_CHANNEL_TYPES),否则页面上能设置、调度却不认。
GROUP_SCOPED_CHANNEL_TYPES = frozenset({"wechat_ipad", "wechat_winauto", "workwechat", "wechat_webridge", "wechat_gugatool"})


def _load_group_scope_map(
    db: Session,
    *,
    channel: Channel,
    context: PluginDispatchContext,
) -> dict[int, GroupPluginScope]:
    channel_type = str(channel.channel_type or "").strip()
    if channel_type not in GROUP_SCOPED_CHANNEL_TYPES:
        return {}
    if str(context.chat_type or "").strip() != "group":
        return {}
    external_id = str(context.conversation_external_id or "").strip()
    if not external_id:
        return {}
    if channel_type == "wechat_ipad" and not external_id.endswith("@chatroom"):
        return {}
    chatroom = _load_group_chatroom(db, channel=channel, context=context)
    if chatroom is None:
        return {}
    scope_rows = db.scalars(
        select(GroupPluginScope).where(
            GroupPluginScope.channel_id == channel.id,
            GroupPluginScope.chatroom_id == chatroom.id,
        )
    ).all()
    return {int(item.bot_plugin_id): item for item in scope_rows}


def _load_group_chatroom(
    db: Session,
    *,
    channel: Channel,
    context: PluginDispatchContext,
) -> Chatroom | None:
    channel_type = str(channel.channel_type or "").strip()
    if channel_type not in GROUP_SCOPED_CHANNEL_TYPES:
        return None
    if str(context.chat_type or "").strip() != "group":
        return None
    external_id = str(context.conversation_external_id or "").strip()
    if not external_id:
        return None
    if channel_type == "wechat_ipad" and not external_id.endswith("@chatroom"):
        return None
    return db.scalar(
        select(Chatroom).where(
            Chatroom.channel_id == channel.id,
            Chatroom.external_id == external_id,
        )
    )


def _build_discovered_plugin(
    plugin_dir: Path,
    config_path: Path,
    entrypoint_path: Path,
    parsed: dict[str, Any],
    *,
    parse_error: str = "",
) -> DiscoveredPlugin:
    errors, warnings = _validate_manifest(plugin_dir.name, parsed)
    if parse_error:
        errors.insert(0, parse_error)
    config_fields, field_errors = read_config_fields(parsed)
    errors += field_errors
    schedules, schedule_errors = read_schedules(parsed)
    errors += schedule_errors
    # 声明为 secret 的字段自动按密钥处理(加密保存、不进普通配置),不必在 [secrets] 里再写一遍。
    secret_fields = tuple(dict.fromkeys((*_extract_secret_fields(parsed), *(item.key for item in config_fields if item.kind == "secret"))))
    runtime_config = _extract_runtime_config(parsed)
    # 即使旧目录里仍残留可用密钥，扫描阶段也必须把显式敏感字段从默认运行配置中删除。
    # 后续物理清理配置文件时不依赖这层防线，但当前提交开始就不能再执行共享凭证。
    for field_name in secret_fields:
        runtime_config.pop(field_name, None)
    return DiscoveredPlugin(
        manifest=_build_manifest(plugin_dir.name, parsed),
        capability=_build_capability(parsed),
        matcher=_build_matcher(parsed),
        ai_hint=_build_ai_hint(parsed),
        runtime_config=runtime_config,
        secret_fields=secret_fields,
        config_fields=config_fields,
        schedules=schedules,
        admin_config=_extract_admin_config(parsed),
        config_path=config_path,
        entrypoint_path=entrypoint_path,
        directory_path=plugin_dir,
        validation_errors=tuple(errors),
        validation_warnings=tuple(warnings),
    )


def _build_capability(parsed: dict[str, Any]) -> PluginCapability:
    capability_block = parsed.get("capability", {})
    return PluginCapability(
        channel_types=_normalize_text_tuple(capability_block.get("channel_types")),
        chat_types=_normalize_text_tuple(capability_block.get("chat_types")),
        message_types=_normalize_text_tuple(capability_block.get("message_types")),
        priority=_coerce_int(capability_block.get("priority"), DEFAULT_PRIORITY),
        consume_policy=str(capability_block.get("consume_policy") or DEFAULT_CONSUME_POLICY).strip() or DEFAULT_CONSUME_POLICY,
        timeout_ms=_coerce_int(capability_block.get("timeout_ms"), DEFAULT_TIMEOUT_MS),
    )


def _build_matcher(parsed: dict[str, Any]) -> PluginMatcher:
    matcher_block = parsed.get("matcher", {})
    return PluginMatcher(
        commands=_normalize_text_tuple(matcher_block.get("commands")),
        keywords=_normalize_text_tuple(matcher_block.get("keywords")),
        require_mention=bool(matcher_block.get("require_mention", False)),
    )


def _build_ai_hint(parsed: dict[str, Any]) -> PluginAiHint:
    """读取可选的 [ai] 段；缺省视为不参与 AI 调度，不猜测插件用途。"""

    block = parsed.get("ai")
    if not isinstance(block, dict):
        return PluginAiHint()
    return PluginAiHint(
        routable=bool(block.get("routable", False)),
        risk=str(block.get("risk") or "high").strip().lower(),
        summary=str(block.get("summary") or "").strip(),
        examples=_normalize_text_tuple(block.get("examples")),
        needs_args=str(block.get("needs_args") or "").strip(),
    )


def _normalize_text_tuple(raw_value: Any) -> tuple[str, ...]:
    if isinstance(raw_value, list):
        return tuple(str(item).strip() for item in raw_value if str(item).strip())
    if isinstance(raw_value, str):
        normalized = raw_value.strip()
        if not normalized:
            return ()
        return tuple(item.strip() for item in normalized.replace("，", ",").split(",") if item.strip())
    return ()


def _coerce_optional_text(value: Any) -> str | None:
    if value is None:
        return None
    text = str(value).strip()
    return text or None


def _resolve_display_name(code: str, raw_display_name: str) -> str:
    override = DISPLAY_NAME_OVERRIDES.get(code)
    if override:
        return override
    return raw_display_name or code


def _coerce_int(value: Any, default: int) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def _validate_manifest(default_code: str, parsed: dict[str, Any]) -> tuple[list[str], list[str]]:
    errors: list[str] = []
    warnings: list[str] = []

    plugin_block = parsed.get("plugin")
    if not isinstance(plugin_block, dict):
        errors.append("缺少 [plugin] 配置块。")
        plugin_block = {}

    declared_code = _coerce_optional_text(plugin_block.get("code"))
    if not declared_code:
        errors.append("缺少 plugin.code。")
    elif declared_code != default_code:
        errors.append(f"plugin.code 与目录名不一致：声明为 {declared_code}，目录为 {default_code}。")
        warnings.append(f"当前按目录名 {default_code} 作为扫描镜像编码，声明值 {declared_code} 不生效。")

    plugin_type = _coerce_optional_text(plugin_block.get("plugin_type")) or "message"
    if plugin_type not in ALLOWED_PLUGIN_TYPES:
        errors.append(f"plugin_type 非法：{plugin_type}。")

    capability_block = parsed.get("capability")
    if capability_block is not None and not isinstance(capability_block, dict):
        errors.append("[capability] 配置块类型非法。")
        capability_block = {}
    capability_block = capability_block or {}

    consume_policy = _coerce_optional_text(capability_block.get("consume_policy")) or DEFAULT_CONSUME_POLICY
    if consume_policy not in ALLOWED_CONSUME_POLICIES:
        errors.append(f"consume_policy 非法：{consume_policy}。")

    priority_value = capability_block.get("priority")
    if priority_value is not None and not _is_int_like(priority_value):
        errors.append("priority 必须是整数。")

    timeout_value = capability_block.get("timeout_ms")
    if timeout_value is not None:
        if not _is_int_like(timeout_value):
            errors.append("timeout_ms 必须是整数。")
        elif int(timeout_value) < 0:
            errors.append("timeout_ms 不能小于 0。")

    matcher_block = parsed.get("matcher")
    if matcher_block is not None and not isinstance(matcher_block, dict):
        errors.append("[matcher] 配置块类型非法。")
        matcher_block = {}
    matcher_block = matcher_block or {}

    _validate_text_sequence(capability_block.get("channel_types"), "channel_types", errors)
    _validate_text_sequence(capability_block.get("chat_types"), "chat_types", errors)
    _validate_text_sequence(capability_block.get("message_types"), "message_types", errors)
    _validate_text_sequence(matcher_block.get("commands"), "commands", errors)
    _validate_text_sequence(matcher_block.get("keywords"), "keywords", errors)
    secrets_block = parsed.get("secrets")
    if secrets_block is not None and not isinstance(secrets_block, dict):
        errors.append("[secrets] 配置块类型非法。")
    elif isinstance(secrets_block, dict):
        _validate_text_sequence(secrets_block.get("fields"), "secrets.fields", errors)
    return errors, warnings


def _validate_text_sequence(value: Any, field_name: str, errors: list[str]) -> None:
    if value is None or isinstance(value, str):
        return
    if not isinstance(value, list):
        errors.append(f"{field_name} 必须是字符串或字符串数组。")
        return
    if any(not isinstance(item, str) for item in value):
        errors.append(f"{field_name} 数组成员必须是字符串。")


def _is_int_like(value: Any) -> bool:
    try:
        int(value)
    except (TypeError, ValueError):
        return False
    return True


def _build_synced_plugin_description(item: DiscoveredPlugin) -> str | None:
    parts: list[str] = []
    if item.manifest.description:
        parts.append(item.manifest.description)
    if item.validation_errors:
        parts.append("manifest 校验失败：" + "；".join(item.validation_errors))
    elif item.validation_warnings:
        parts.append("manifest 提示：" + "；".join(item.validation_warnings))
    return "\n".join(parts) if parts else None


def _extract_runtime_config(parsed: dict[str, Any]) -> dict[str, Any]:
    runtime_block = parsed.get("runtime")
    if isinstance(runtime_block, dict):
        return dict(runtime_block)

    defaults: dict[str, Any] = {}
    for key, value in parsed.items():
        if key in {"plugin", "capability", "matcher", "secrets", "admin"}:
            continue
        if isinstance(value, dict):
            defaults.update(value)
    return defaults


def _extract_secret_fields(parsed: dict[str, Any]) -> tuple[str, ...]:
    """读取插件显式敏感字段声明，字段名重复时保持首次出现顺序。"""

    secrets_block = parsed.get("secrets")
    if not isinstance(secrets_block, dict):
        return ()
    return tuple(dict.fromkeys(_normalize_text_tuple(secrets_block.get("fields"))))


def _extract_admin_config(parsed: dict[str, Any]) -> dict[str, Any]:
    """读取插件后台声明。

    这里只做受控元数据解析，不允许插件通过配置动态注册路由；真正的业务路由仍由主项目显式实现。
    """
    admin_block = parsed.get("admin")
    return dict(admin_block) if isinstance(admin_block, dict) else {}


def _parse_config_json(raw_config: str | None) -> dict[str, Any]:
    normalized = (raw_config or "").strip()
    if not normalized:
        return {}
    try:
        parsed = json.loads(normalized)
    except json.JSONDecodeError:
        return {}
    return parsed if isinstance(parsed, dict) else {}


def _build_scan_signature(base_dir: Path) -> tuple[str, ...]:
    if not base_dir.exists():
        return ()
    signature: list[str] = []
    for plugin_dir in sorted(path for path in base_dir.iterdir() if path.is_dir()):
        config_path = plugin_dir / "config.toml"
        if config_path.exists():
            signature.append(f"config:{config_path}:{config_path.stat().st_mtime_ns}")
        for candidate in ENTRYPOINT_CANDIDATES:
            entrypoint = plugin_dir / candidate
            if entrypoint.exists():
                signature.append(f"entry:{entrypoint}:{entrypoint.stat().st_mtime_ns}")
                break
    return tuple(signature)


