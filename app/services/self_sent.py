"""判断一条入站事件是不是"本账号自己发出的"。

全局约定:自己发出的消息(手机上亲自发的、机器人回复后又被协议回流的)只存档,**不进入任何内容检测**——
插件命令/关键词、群功能、触发回复规则、AI 都不应该把它当作别人说的话。否则机器人的回复会再次命中规则,
形成自问自答的循环;在机器人账号上手动发一句话也会触发自动回复。

各渠道标记自发消息的方式不一致,这里统一识别:
- 微桥、咕嘎助手:适配器按注册的账号 ID 比对,在 details 里写 from_self=True;
- iPad、企业微信:适配器把方向写成 direction="out"。
新增渠道时,只要用上面任意一种方式标记,就自动被全局拦住,不需要再改分发代码。
"""

from __future__ import annotations

from typing import Any, Mapping


def is_self_sent(normalized: Mapping[str, Any] | None) -> bool:
    if not isinstance(normalized, Mapping):
        return False
    details = normalized.get("details") if isinstance(normalized.get("details"), Mapping) else {}
    message = normalized.get("message") if isinstance(normalized.get("message"), Mapping) else {}
    if details.get("from_self") is True:
        return True
    direction = str(message.get("direction") or details.get("direction") or "").strip().lower()
    return direction == "out"
