"""写入 TEXT 列的 JSON 序列化收口。

MariaDB 的 TEXT 列上限是 65535 字节。插件回复可能带整张图片的 base64,调度报告和执行日志
原样入库会触发 "Data too long",并连带回滚同一事务,已入队的出站消息也跟着丢失。
所有会携带插件回复内容的 JSON 列,都必须经过这里再写入。
"""

from __future__ import annotations

import json
from typing import Any

# 单个字符串超过该长度就只保留摘要;整体超过字节上限就逐级丢弃体积大的部分。
STRING_LIMIT = 500
BYTE_LIMIT = 60000


def shrink_strings(value: Any) -> Any:
    """递归把过长的字符串(图片 base64、整段正文)截成摘要,保留排查需要的小字段。"""

    if isinstance(value, str):
        if len(value) > STRING_LIMIT:
            return f"{value[:STRING_LIMIT]}...(共 {len(value)} 字符,已截断)"
        return value
    if isinstance(value, dict):
        return {key: shrink_strings(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [shrink_strings(item) for item in value]
    return value


def _encode(value: Any, sort_keys: bool) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=sort_keys, default=str)


def dump_json_for_text_column(value: Any, *, sort_keys: bool = False, drop_keys: tuple[str, ...] = ()) -> str:
    """序列化为不超过列容量的 JSON。

    先截断超长字符串;仍然超限时依次丢弃 drop_keys 指定的顶层或二级大字段;
    最后兜底只写一个截断标记,保证绝不因为日志写入而回滚业务事务。
    """

    shrunk = shrink_strings(value)
    text = _encode(shrunk, sort_keys)
    if len(text.encode("utf-8")) <= BYTE_LIMIT:
        return text
    if isinstance(shrunk, dict):
        for key in drop_keys:
            shrunk.pop(key, None)
            for child in shrunk.values():
                if isinstance(child, dict):
                    child.pop(key, None)
        text = _encode(shrunk, sort_keys)
        if len(text.encode("utf-8")) <= BYTE_LIMIT:
            return text
    return _encode({"truncated": True, "reason": "内容超过存储上限"}, False)
