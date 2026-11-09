"""平台日志页的解析与中文化。

日志文件的物理格式保持不变(便于 grep 和外部采集),这里只在展示层把每条日志
解析成「时间 / 级别 / 来源 / 中文标题 / 关键字段」,把 Traceback 等续行并入上一条。
未登记的英文事件名原样显示,不会丢信息。
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any, Iterator

_LINE_RE = re.compile(
    r"^(?P<date>\d{4}-\d{2}-\d{2}) (?P<time>\d{2}:\d{2}:\d{2})\.\d{3} \| (?P<level>[A-Z]+)\s*\| (?P<where>\S+) - (?P<body>.*)$"
)
_FIELD_RE = re.compile(r"(?:^| )([A-Za-z_][A-Za-z0-9_]*)=")

# 级别排序,用于“只看警告及以上”。
LEVEL_RANK = {"DEBUG": 10, "INFO": 20, "WARNING": 30, "ERROR": 40, "CRITICAL": 50}

# 字段名中文标签;未登记的字段直接显示原名。
FIELD_LABELS = {
    "channel_id": "渠道",
    "conversation_id": "会话",
    "conversation_title": "群/会话",
    "sender_name": "发言人",
    "message_preview": "内容",
    "message_id": "消息",
    "plugin_code": "插件",
    "binding_id": "绑定",
    "job_id": "任务",
    "job_type": "任务类型",
    "request_id": "请求号",
    "duration_ms": "耗时(ms)",
    "status_code": "状态码",
    "status": "状态",
    "method": "方法",
    "path": "路径",
    "error": "错误",
    "error_code": "错误码",
    "error_message": "错误信息",
    "command_name": "命令",
    "command_names": "命令",
    "command_count": "命令数",
    "tenant_id": "租户",
    "raw_event_id": "原始事件",
    "executed_plugins": "已执行插件",
    "message_type": "消息类型",
    "channel_type": "渠道类型",
    "reply_preview": "回复",
    "adapter_type": "适配器",
    "ok": "成功",
}

# 事件名 -> 中文标题模板,模板里的 {字段} 取自该条日志的字段,取不到时显示 ?。
EVENT_TITLES = {
    "request completed": "接口请求 {method} {path} → {status_code}({duration_ms}ms)",
    "request failed": "接口请求异常 {method} {path}",
    "job enqueued": "任务入队 #{job_id} ({job_type})",
    "job claimed": "任务领取 #{job_id}",
    "job completed": "任务完成 #{job_id} ({job_type})",
    "job failed": "任务失败 #{job_id}",
    "job processing failed": "任务处理失败 #{job_id}",
    "job terminal failed without retry": "任务终止失败(不再重试) #{job_id}",
    "recovered running jobs back to pending": "启动恢复:把执行中断的任务放回队列",
    "raw inbound event persisted": "入站原始事件已保存",
    "ingesting inbound event": "开始处理入站事件",
    "inbound message mapped": "收到消息",
    "inbound event ignored": "忽略的入站事件",
    "deduplicated inbound event": "重复事件已去重",
    "normalized event queued for async dispatch": "消息已进入异步插件分发队列",
    "normalized event skipped plugin dispatch by quiet hours": "免打扰时段,跳过插件分发",
    "normalized event skipped plugin dispatch by explicit flag": "事件标记跳过插件分发",
    "plugin dispatch job processed": "插件分发完成,已执行:{executed_plugins}",
    "executing plugin candidate": "开始执行插件 {plugin_code}",
    "directory handler executed": "插件执行完成 {plugin_code} → {status}",
    "directory handler execution timed out": "插件执行超时 {plugin_code}",
    "directory handler execution failed": "插件执行异常 {plugin_code}",
    "executing plugin action": "执行插件动作 {plugin_code}.{action}",
    "executing message plugin action": "执行消息插件动作 {plugin_code}.{action}",
    "executing scheduled plugin action": "执行定时插件动作 {plugin_code}.{action}",
    "dispatching outbound message": "发送消息 → 会话 {conversation_id}",
    "outbound dispatch job processed": "出站任务完成 (成功={ok})",
    "outbound message dispatched with failure": "消息发送失败",
    "skipping expired outbound dispatch job": "出站任务已过期,跳过",
    "auto outbound dispatch completed": "插件回复已发出",
    "webridge commands claimed": "微桥手机领取命令 {command_names}",
    "webridge command result received": "微桥收到命令结果",
    "webridge command completed": "微桥命令完成 #{job_id}",
    "webridge command failed": "微桥命令失败 #{job_id}",
    "webridge directory persisted": "微桥同步通讯录落库",
    "webridge event received": "微桥收到事件",
    "webridge state received": "微桥收到设备状态",
    "webridge register received": "微桥手机注册",
    "webridge register completed": "微桥手机注册完成",
    "webridge stale commands reaped": "微桥清理超时未领取的命令",
    "application module initialized": "应用模块初始化完成",
    "background worker started": "后台任务进程启动",
    "background worker stopped": "后台任务进程停止",
    "background worker loop crashed": "后台任务循环崩溃",
    "plugin discovery completed": "插件扫描完成",
    "database lifecycle ready": "数据库就绪",
    "checking database migration revision": "检查数据库迁移版本",
    "logging configured": "日志系统已启动",
    "ai intent analysis failed": "AI 意图分析失败",
    "ai chat reply failed": "AI 聊天回复失败",
    "runtime executor timed out": "插件运行器超时",
    "runtime executor executed": "插件运行器执行完成",
    "open webhook delivered": "开放接口回调已送达",
    "open webhook delivery failed": "开放接口回调失败",
}


def _clean_value(value: str) -> str:
    value = value.strip()
    # 布尔值显示成中文,避免“成功 True”。
    value = {"True": "是", "False": "否"}.get(value, value)
    return value[:300] + "…" if len(value) > 300 else value


def parse_fields(text: str) -> dict[str, str]:
    """把 `k=v k2=v2` 形式的上下文拆成字典;值里可以含空格。"""

    parts = _FIELD_RE.split(text)
    # split 结果:[前缀, 键1, 值1, 键2, 值2 ...]
    return {parts[i]: _clean_value(parts[i + 1]) for i in range(1, len(parts) - 1, 2)}


class _SafeDict(dict):
    def __missing__(self, key: str) -> str:
        return "?"


def _build_entry(first_line: str, match: re.Match[str]) -> dict[str, Any]:
    body = match.group("body")
    message, _, context = body.partition(" | ")
    fields = parse_fields(context) if context else {}
    title = EVENT_TITLES.get(message.strip())
    title = title.format_map(_SafeDict(fields)) if title else message.strip()
    # 标题里已经用到的字段不再重复出现在字段区。
    used = set(re.findall(r"{(\w+)}", EVENT_TITLES.get(message.strip(), "")))
    module = match.group("where").split(":")[0]
    return {
        "date": match.group("date"),
        "time": match.group("time"),
        "level": match.group("level"),
        "rank": LEVEL_RANK.get(match.group("level"), 20),
        "source": ".".join(module.split(".")[-2:]),
        "title": title,
        "fields": [(FIELD_LABELS.get(key, key), value) for key, value in fields.items() if key not in used],
        "trace": [],
        "raw": first_line,
    }


def parse_entries(lines: list[str]) -> list[dict[str, Any]]:
    """按时间顺序解析日志行;不匹配时间格式的行(Traceback 等)并入上一条。"""

    entries: list[dict[str, Any]] = []
    for line in lines:
        match = _LINE_RE.match(line)
        if match:
            entries.append(_build_entry(line, match))
        elif entries:
            entries[-1]["trace"].append(line)
            entries[-1]["raw"] += "\n" + line
    return entries


_CHUNK_BYTES = 256 * 1024


def _iter_lines_reversed(path: Path) -> Iterator[str]:
    """从文件末尾分块向前读取,逐行产出(最新的行在前),不会整文件载入内存。"""

    with path.open("rb") as handle:
        handle.seek(0, 2)
        position = handle.tell()
        tail = b""
        while position > 0:
            size = min(_CHUNK_BYTES, position)
            position -= size
            handle.seek(position)
            data = handle.read(size) + tail
            parts = data.split(b"\n")
            # 第一段可能被块边界截断,留到下一轮和更早的内容拼接。
            tail = parts[0]
            for raw in reversed(parts[1:]):
                yield raw.decode("utf-8", errors="ignore").rstrip("\r")
        if tail:
            yield tail.decode("utf-8", errors="ignore").rstrip("\r")


def iter_entries_reversed(path: Path) -> Iterator[dict[str, Any]]:
    """由新到旧产出日志条目;往回读到时间行时,把已收集的续行(Traceback)并成一条。"""

    pending: list[str] = []
    for line in _iter_lines_reversed(path):
        match = _LINE_RE.match(line)
        if match:
            entry = _build_entry(line, match)
            for extra in reversed(pending):
                entry["trace"].append(extra)
                entry["raw"] += "\n" + extra
            pending = []
            yield entry
        elif line:
            pending.append(line)


def read_page(
    path: Path,
    *,
    page: int,
    page_size: int,
    min_rank: int = 0,
    keyword: str = "",
) -> tuple[list[dict[str, Any]], bool]:
    """读取指定页的条目,凑够即停。返回 (条目, 是否还有更早的一页)。

    多读一条用来判断是否还有下一页,因此不需要统计总数,耗时只取决于页码而不是文件大小。
    """

    skip = (max(page, 1) - 1) * page_size
    items: list[dict[str, Any]] = []
    try:
        for entry in iter_entries_reversed(path):
            if entry["rank"] < min_rank:
                continue
            if keyword and keyword not in entry["raw"].lower():
                continue
            if skip > 0:
                skip -= 1
                continue
            items.append(entry)
            if len(items) > page_size:
                return items[:page_size], True
    except OSError:
        pass
    return items, False
