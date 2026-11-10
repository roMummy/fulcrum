"""AI 长期记忆:对接 Hindsight 记忆服务(召回 recall / 写入 retain)。

设计要点:
- 记忆库按"会话"划分:每个群一个库,每个渠道的私聊共用一个库;库内用标签 user:<发言人> 区分是谁说的,
  召回时带上发言人标签,既能拿到这个人的记忆,也能拿到没有标签的群共同背景。库名带租户前缀,租户之间互不可见。
- 记忆服务地址来自平台配置 HINDSIGHT_URL(环境变量,不是租户可填项,所以不存在被租户指向内部服务的风险)。
  没配置时整个模块不工作,AI 对话照常聊天,只是没有长期记忆。
- 记忆服务出任何问题都不能影响聊天:召回失败返回空、写入失败只记日志;连续失败后短时间内直接跳过(断路),
  避免每条消息都白等一次超时。
- 写入使用异步模式(记忆服务在后台用 LLM 抽取事实,可能要几秒到几十秒),不会阻塞聊天回复。
"""

from __future__ import annotations

import json
import re
import threading
import time
import urllib.error
import urllib.request
from dataclasses import replace
from datetime import datetime
from typing import Any
from urllib.parse import quote

from app.core.logging import get_logger
from app.core.settings import settings
from app.core.time import now_shanghai

logger = get_logger(__name__)

API_PREFIX = "/v1/default/banks"
# 召回要尽快返回,聊天在等它;写入是异步入队,也不用等太久。
RECALL_TIMEOUT_SECONDS = 4
RETAIN_TIMEOUT_SECONDS = 6
ADMIN_TIMEOUT_SECONDS = 15
# 注入提示词的记忆条数与长度上限。
RECALL_MAX_ITEMS = 8
RECALL_MAX_TOKENS = 800
MEMORY_ITEM_CHARS = 200
# 用户消息太短(如"哈哈""好的")不值得记。
MIN_REMEMBER_CHARS = 6
# 记忆服务连续不可用时,这么多秒内不再尝试。
CIRCUIT_OPEN_SECONDS = 60
# 列出记忆库时每页条数。
PAGE_SIZE = 100

_down_until = 0.0
_lock = threading.Lock()


def is_configured() -> bool:
    return bool(str(settings.hindsight_url or "").strip())


def _base_url() -> str:
    return str(settings.hindsight_url or "").strip().rstrip("/")


def bank_prefix(tenant_id: int) -> str:
    """租户的记忆库名前缀,清空记忆时按它筛选,不会碰到其他租户。"""

    return f"t{int(tenant_id)}-"


def bank_id(*, tenant_id: int, channel_id: int, conversation_id: int | None, chat_type: str) -> str:
    """记忆库名:群聊每个群一个库;私聊同一个渠道共用一个库(库内按发言人标签区分)。"""

    if chat_type == "group" and conversation_id:
        return f"{bank_prefix(tenant_id)}ch{int(channel_id)}-g{int(conversation_id)}"
    return f"{bank_prefix(tenant_id)}ch{int(channel_id)}-direct"


def user_tag(sender_id: str) -> str:
    return "user:" + re.sub(r"\s+", "", str(sender_id or ""))[:100]


def _clean(value: object, limit: int) -> str:
    return re.sub(r"\s+", " ", str(value or "")).strip()[:limit]


def _circuit_open() -> bool:
    return time.monotonic() < _down_until


def _trip() -> None:
    global _down_until
    with _lock:
        _down_until = time.monotonic() + CIRCUIT_OPEN_SECONDS


def _request(method: str, path: str, body: dict[str, Any] | None, *, timeout: int) -> Any:
    """向记忆服务发一个 JSON 请求;失败抛 OSError/ValueError,由调用方决定怎么降级。"""

    url = _base_url() + path
    data = json.dumps(body, ensure_ascii=False).encode("utf-8") if body is not None else None
    request = urllib.request.Request(url, data=data, method=method, headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            raw = response.read()
    except urllib.error.HTTPError as exc:
        raise ValueError(f"记忆服务返回 {exc.code}") from exc
    return json.loads(raw) if raw else None


def recall(bank: str, query: str, sender_id: str) -> tuple[str, ...]:
    """按当前话题召回相关记忆(该发言人的 + 群共同背景),失败或没配置返回空。"""

    if not is_configured() or _circuit_open() or not _clean(query, 10):
        return ()
    body = {
        # 查询超过 500 token 会被拒绝,截短即可,话题已经足够。
        "query": _clean(query, 300),
        "budget": "low",
        "max_tokens": RECALL_MAX_TOKENS,
        "tags": [user_tag(sender_id)],
        # any:带这个人标签的,加上没有标签的(群共同背景)。
        "tags_match": "any",
    }
    try:
        result = _request("POST", f"{API_PREFIX}/{quote(bank, safe='')}/memories/recall", body, timeout=RECALL_TIMEOUT_SECONDS)
    except ValueError as exc:
        # 库还不存在(第一次聊天前)是正常情况,不算故障。
        if "404" not in str(exc):
            logger.warning("memory recall failed", extra={"bank": bank, "error": str(exc)})
        return ()
    except (OSError, json.JSONDecodeError) as exc:
        logger.warning("memory service unavailable on recall", extra={"bank": bank, "error": str(exc)[:120]})
        _trip()
        return ()
    items: list[str] = []
    for entry in (result or {}).get("results") or []:
        text = _clean(entry.get("text") if isinstance(entry, dict) else "", MEMORY_ITEM_CHARS)
        if text and text not in items:
            items.append(text)
        if len(items) >= RECALL_MAX_ITEMS:
            break
    return tuple(items)


def should_remember(text: str) -> bool:
    return len(_clean(text, 10_000)) >= MIN_REMEMBER_CHARS


def retain(bank: str, *, sender_id: str, sender_name: str, text: str, chat_type: str, when: datetime | None = None) -> bool:
    """把一条用户发言异步写入记忆;返回是否已成功提交(失败只记日志,不影响聊天)。

    内容里写明说话人,并在 context 里说明场景,这样记忆服务会把"我在北京"记成关于这个人的事实,
    而不是机器人自己的经历。时间戳让"下个月""昨天"这类说法能换算成具体日期。
    """

    if not is_configured() or _circuit_open() or not should_remember(text):
        return False
    name = _clean(sender_name, 40) or "群友"
    scene = "微信群聊天" if chat_type == "group" else "微信私聊"
    moment = (when or now_shanghai()).isoformat()
    body = {
        "items": [
            {
                "content": f"{name}: {_clean(text, 1500)}",
                "context": f"{scene}。说话人是{name}(用户),对方是群里的 AI 助手。只记关于{name}本人的稳定信息和计划。",
                "timestamp": moment,
                "tags": [user_tag(sender_id)],
            }
        ],
        "async": True,
    }
    try:
        _request("POST", f"{API_PREFIX}/{quote(bank, safe='')}/memories", body, timeout=RETAIN_TIMEOUT_SECONDS)
    except ValueError as exc:
        logger.warning("memory retain rejected", extra={"bank": bank, "error": str(exc)})
        return False
    except (OSError, json.JSONDecodeError) as exc:
        logger.warning("memory service unavailable on retain", extra={"bank": bank, "error": str(exc)[:120]})
        _trip()
        return False
    return True


def retain_batch(bank: str, *, content: str, when: datetime | None = None) -> bool:
    """把一段多人群聊记录异步写入记忆(不打个人标签:这是群里的共同背景,召回时任何人提问都能用到)。

    每行是"昵称: 内容",由记忆服务分别抽取各人的信息;失败只返回 False,由调用方决定是否重试。
    """

    from app.services.ai_memory_digest import _BATCH_CONTEXT

    if not is_configured() or _circuit_open() or not _clean(content, 10):
        return False
    body = {
        "items": [{"content": content, "context": _BATCH_CONTEXT, "timestamp": (when or now_shanghai()).isoformat()}],
        "async": True,
    }
    try:
        _request("POST", f"{API_PREFIX}/{quote(bank, safe='')}/memories", body, timeout=RETAIN_TIMEOUT_SECONDS)
    except ValueError as exc:
        logger.warning("memory batch retain rejected", extra={"bank": bank, "error": str(exc)})
        return False
    except (OSError, json.JSONDecodeError) as exc:
        logger.warning("memory service unavailable on batch retain", extra={"bank": bank, "error": str(exc)[:120]})
        _trip()
        return False
    return True


def list_banks(tenant_id: int) -> list[str] | None:
    """本租户的记忆库名;没配置或服务不可用返回 None。记忆服务的列表是分页的,这里翻完所有页。"""

    if not is_configured():
        return None
    prefix = bank_prefix(tenant_id)
    names: list[str] = []
    offset = 0
    while True:
        try:
            result = _request("GET", f"{API_PREFIX}?limit={PAGE_SIZE}&offset={offset}", None, timeout=ADMIN_TIMEOUT_SECONDS)
        except (OSError, ValueError, json.JSONDecodeError):
            return None
        banks = (result or {}).get("banks") or []
        for entry in banks:
            name = entry.get("bank_id") if isinstance(entry, dict) else None
            if isinstance(name, str) and name.startswith(prefix):
                names.append(name)
        if len(banks) < PAGE_SIZE:
            return names
        offset += PAGE_SIZE


def clear_tenant_memories(tenant_id: int) -> int | None:
    """删除本租户的全部记忆库,返回删除个数;没配置或服务不可用返回 None。"""

    names = list_banks(tenant_id)
    if names is None:
        return None
    removed = 0
    for name in names:
        try:
            _request("DELETE", f"{API_PREFIX}/{quote(name, safe='')}", None, timeout=ADMIN_TIMEOUT_SECONDS)
            removed += 1
        except (OSError, ValueError, json.JSONDecodeError):
            logger.warning("memory bank delete failed", extra={"bank": name})
    return removed


def with_recalled_memories(chat):
    """给聊天上下文补上召回的记忆(会访问网络,必须在不持有数据库会话时调用)。"""

    if not getattr(chat, "memory_enabled", False) or not chat.bank_id:
        return chat
    return replace(chat, memories=recall(chat.bank_id, chat.text, chat.sender_id))
