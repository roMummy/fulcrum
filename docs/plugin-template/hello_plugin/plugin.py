"""标准示例插件:收到命令后按配置打招呼。

读懂这个文件就掌握了写插件的基本套路:
1. 平台调用模块级函数 execute_action(request),返回 PluginExecutionResult;
2. 配置从 request.config 读取,读取时一律做类型和范围校验,不信任保存的内容一定合法;
3. 用户发的内容在 request.payload 里(command_args 是命令后面跟的文字);
4. 想让机器人回复,就把文字放进 output["reply_text"]。

需要读写数据库(比如订阅)时,另外实现 execute_message_action(db, request);
需要定时推送时实现 execute_scheduled_action(db, request)。详见《插件开发规范》。
"""

from __future__ import annotations

from typing import Any, Mapping

from app.services.plugin_runtime.contracts import PluginExecutionRequest, PluginExecutionResult

STYLES = {"friendly": "~", "formal": "。"}


def _bounded_int(value: Any, default: int, minimum: int, maximum: int) -> int:
    """把配置值转成范围内的整数;不是数字时用默认值。"""

    try:
        number = int(value)
    except (TypeError, ValueError):
        return default
    return max(minimum, min(maximum, number))


def _as_list(value: Any) -> list[str]:
    """标签类配置保存为字符串列表;兜底处理成其他形态的情况。"""

    if isinstance(value, (list, tuple)):
        return [str(item).strip() for item in value if str(item).strip()]
    return []


def _build_greeting(config: Mapping[str, Any], target: str) -> str:
    greeting = str(config.get("greeting") or "你好").strip()[:50]
    times = _bounded_int(config.get("times"), 1, 1, 5)
    ending = "!" if config.get("loud") is True else STYLES.get(str(config.get("style")), "~")
    text = " ".join([f"{greeting}{ending}"] * times)
    if target:
        text = f"{target},{text}"
    signature = _as_list(config.get("signature"))
    return text + (f"\n—— {'、'.join(signature)}" if signature else "")


def execute_action(request: PluginExecutionRequest) -> PluginExecutionResult:
    # reload 是后台的"健康检查"按钮,只需要说明插件正常
    if request.action == "reload":
        return PluginExecutionResult(ok=True, status="running", message="示例插件运行正常。")
    if request.action != "execute":
        return PluginExecutionResult(ok=False, status="unsupported", message=f"不支持的动作: {request.action}")

    # 命令后面跟的文字,例如发送"你好 小明"时 command_args 是"小明"
    target = str(request.payload.get("command_args") or "").strip()[:30]
    reply = _build_greeting(request.config, target)
    return PluginExecutionResult(ok=True, status="success", message="已回复", output={"reply_text": reply})
