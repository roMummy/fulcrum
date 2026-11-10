"""消息内容解析:把原始消息(文字,或"发送人: <XML>")整理成消息中心卡片需要的结构。

微信的非文字消息内容是 XML:
- 图片 <img length=… cdnthumbwidth=… />、视频 <videomsg length=… playlength=… />、语音 <voicemsg voicelength=…(毫秒) />、表情 <emoji len=… />;
- 其他都是 <appmsg><type>N</type>…:5 链接、6 文件、19 聊天记录、33/36 小程序、57 引用(<refermsg> 里是被引用的人和内容)、
  2000 转账、2001 红包等。
群聊里这类内容前面还带一个"发送人: "前缀(文字消息没有)。

这里只做"解析成结构",不读文件、不访问网络;哪些媒体已经下载由调用方按附件状态补充(attach_media)。
"""

from __future__ import annotations

import html
import re
from typing import Any, Iterable
from urllib.parse import urlsplit
from xml.etree import ElementTree

# 超过这个长度的内容不解析 XML(防止异常大的内容拖慢页面)。
MAX_XML_CHARS = 60_000
_SENDER_PREFIX = re.compile(r"^[^\s<:]{1,100}:\s*(?=<)")
_XML_DECLARATION = re.compile(r"^\s*<\?xml[^>]*\?>\s*", re.IGNORECASE)
_TAG = re.compile(r"<[^>]+>")

MEDIA_LABELS = {"image": "图片", "video": "视频", "voice": "语音", "emoji": "表情", "file": "文件"}
# 被引用消息的类型 -> 显示文字(文字消息直接显示内容)
REFER_LABELS = {3: "[图片]", 34: "[语音]", 42: "[名片]", 43: "[视频]", 47: "[表情]", 48: "[位置]", 10000: "[系统消息]"}
# appmsg 类型 -> 卡片种类
APPMSG_KINDS = {5: "link", 4: "link", 3: "link", 6: "file", 19: "merged", 33: "mini_program", 36: "mini_program", 57: "quote", 2000: "notice", 2001: "notice"}
KIND_TAGS = {"mini_program": "小程序", "merged": "聊天记录", "notice": "通知", "link": "链接"}


def _text(value: Any) -> str:
    return re.sub(r"\s+", " ", str(value or "")).strip()


def _xml(content: str) -> ElementTree.Element | None:
    body = _XML_DECLARATION.sub("", content.strip())
    if not body.startswith("<") or len(body) > MAX_XML_CHARS:
        return None
    try:
        return ElementTree.fromstring(body)
    except (ElementTree.ParseError, ValueError):
        return None


def _find(root: ElementTree.Element | None, path: str) -> str:
    if root is None:
        return ""
    node = root.find(path)
    return _text(node.text) if node is not None and node.text else ""


def _int(value: Any, default: int = 0) -> int:
    try:
        return int(float(str(value)))
    except (TypeError, ValueError):
        return default


def size_label(size: int) -> str:
    """字节数 -> 1.2 MB 这样的短文字;0 或未知返回空串。"""

    if size <= 0:
        return ""
    value = float(size)
    for unit in ("B", "KB", "MB", "GB"):
        if value < 1024 or unit == "GB":
            return f"{value:.0f} {unit}" if unit == "B" else f"{value:.1f} {unit}"
        value /= 1024
    return ""


def duration_label(seconds: float) -> str:
    total = int(round(seconds))
    return f"{total // 60}:{total % 60:02d}" if total > 0 else ""


def safe_url(value: str) -> str:
    """只接受 http/https 地址(卡片里的链接来自消息内容,不能是 javascript: 之类)。"""

    url = html.unescape(str(value or "")).strip()
    return url if urlsplit(url).scheme in {"http", "https"} and len(url) < 2000 else ""


def strip_sender(content: str) -> str:
    """去掉"发送人: "前缀(只在后面跟着 XML 时去掉,文字消息里的冒号不动)。"""

    return _SENDER_PREFIX.sub("", content.strip(), count=1)


def _quote(root: ElementTree.Element) -> dict[str, Any]:
    refer_type = _int(_find(root, ".//refermsg/type"))
    refer_text = _find(root, ".//refermsg/content")
    if refer_type == 1:
        shown = refer_text
    elif refer_type == 49 and refer_text.startswith("<"):
        # 引用的是卡片:里面是转义过的 appmsg,取它的标题
        nested = _xml(html.unescape(refer_text))
        shown = f"[卡片] {_find(nested, './/title')}".strip() if nested is not None else "[卡片]"
    else:
        shown = REFER_LABELS.get(refer_type, refer_text or "[消息]")
    return {"kind": "quote", "title": _find(root, ".//appmsg/title"), "refer_name": _find(root, ".//refermsg/displayname"), "refer_text": shown[:200]}


def _media_view(media_type: str, root: ElementTree.Element | None) -> dict[str, Any]:
    meta = ""
    if root is not None:
        if media_type == "image":
            node = root.find(".//img")  # 注意:没有子元素的 XML 节点在布尔判断里是"假",必须显式比较 is not None
            if node is not None:
                meta = size_label(_int(node.get("length")))
        elif media_type == "video":
            node = root.find(".//videomsg")
            if node is not None:
                parts = [duration_label(_int(node.get("playlength"))), size_label(_int(node.get("length")))]
                meta = " · ".join(part for part in parts if part)
        elif media_type == "voice":
            node = root.find(".//voicemsg")
            if node is not None:
                meta = duration_label(_int(node.get("voicelength")) / 1000)
        elif media_type == "emoji":
            node = root.find(".//emoji")
            if node is not None:
                meta = size_label(_int(node.get("len")))
    return {"kind": "media", "media_type": media_type, "label": MEDIA_LABELS[media_type], "meta": meta, "title": ""}


def describe(content: object, message_type: object = "text") -> dict[str, Any]:
    """解析一条消息,返回 {"kind": ..., ...}。kind: text / quote / link / mini_program / merged / notice / media / system / unknown。

    解析失败或内容异常时退回纯文字/未知,保证页面永远能显示。
    """

    raw = str(content or "")
    kind_hint = str(message_type or "text").strip().lower()
    body = strip_sender(raw)
    root = _xml(body)

    if kind_hint in MEDIA_LABELS and kind_hint != "file":
        return _media_view(kind_hint, root)
    if root is None:
        if kind_hint == "system" or _TAG.search(raw):
            # 系统消息常带标签(红包图标、自定义链接):去掉标签只留文字
            plain = _text(html.unescape(_TAG.sub(" ", raw)))
            return {"kind": "system" if kind_hint == "system" else "unknown", "text": plain or "[消息]", "raw": raw[:2000]}
        return {"kind": "text", "text": raw.strip()}

    appmsg = root.find(".//appmsg")
    if appmsg is None:
        if root.tag == "sysmsg":
            return {"kind": "system", "text": "[系统消息]", "raw": raw[:2000]}
        return {"kind": "unknown", "text": "[暂不支持显示的消息]", "raw": raw[:2000]}

    app_type = _int(_find(appmsg, "type"))
    kind = APPMSG_KINDS.get(app_type) or {"quote": "quote", "link": "link", "mini_program": "mini_program", "merged_records": "merged", "file": "file"}.get(kind_hint, "link")
    if kind == "quote":
        return _quote(root)
    title = _find(appmsg, "title")
    desc = _find(appmsg, "des")
    if kind == "file":
        view = _media_view("file", None)
        view.update(title=title or "文件", meta=" · ".join(p for p in (_find(appmsg, "appattach/fileext").upper(), size_label(_int(_find(appmsg, "appattach/totallen")))) if p))
        return view
    url = safe_url(_find(appmsg, "url"))
    source = _find(appmsg, "sourcedisplayname") or _find(appmsg, "appinfo/appname") or (urlsplit(url).hostname or "")
    if kind == "link" and not url and not title:
        return {"kind": "unknown", "text": "[暂不支持显示的消息]", "raw": raw[:2000]}
    if "红包" in desc or "wxhb" in url:
        kind = "notice"
    return {"kind": kind, "title": title or desc or "卡片消息", "desc": desc if title else "", "url": url, "source": source, "tag": KIND_TAGS.get(kind, "")}


def attach_media(view: dict[str, Any], attachments: Iterable[Any]) -> dict[str, Any]:
    """给媒体类视图补上附件状态:ready(已下载,可直接显示)/ pending(有记录但没下载)/ failed / none(还没有附件记录)。"""

    if view.get("kind") != "media":
        return view
    wanted = view["media_type"]
    found = [item for item in attachments if str(getattr(item, "attachment_type", "")) == wanted]
    latest = found[-1] if found else None
    state, url = "none", ""
    if latest is not None:
        if latest.status == "downloaded" and latest.storage_path:
            state, url = "ready", f"/media/attachments/{latest.id}"
        elif latest.status == "failed":
            state = "failed"
        else:
            state = "pending"
    view["state"] = state
    view["url"] = url
    return view


def preview(content: object, message_type: object = "text", limit: int = 60) -> str:
    """会话列表、最近消息里用的一行摘要:文字取开头,其他类型用 [图片] [引用] 标题 这样的短标签。"""

    view = describe(content, message_type)
    kind = view["kind"]
    if kind == "text":
        text = _text(view["text"])
    elif kind == "media":
        text = f"[{view['label']}]" + (f" {view['title']}" if view.get("title") else "")
    elif kind == "quote":
        text = f"[引用] {view['title']}".strip()
    elif kind in {"link", "mini_program", "merged", "notice"}:
        text = f"[{KIND_TAGS.get(kind, '卡片')}] {view['title']}"
    else:
        text = view.get("text") or "[消息]"
    return text if len(text) <= limit else text[: limit - 1] + "…"
