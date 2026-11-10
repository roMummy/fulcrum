"""展开/收起统一用右侧的小箭头图标:模板里的展开块都带 disclosure 类,不再出现"展开""收起"这类文字式的展开提示。"""

from __future__ import annotations

import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1] / "app"
TEMPLATES = sorted((ROOT / "templates").glob("*.html"))
# 下拉菜单(更多、账号菜单、"…"操作、管理)是弹出菜单,不是展开内容,不使用箭头图标。
MENU_CLASSES = ("nav-more", "account-menu", "record-action-menu", "record-actions-menu")
DETAILS = re.compile(r"<details\b([^>]*)>")
SUMMARY = re.compile(r"<summary\b[^>]*>(.*?)</summary>", re.S)


def test_every_content_details_uses_the_shared_arrow() -> None:
    missing = []
    for path in TEMPLATES:
        for attrs in DETAILS.findall(path.read_text(encoding="utf-8")):
            classes = re.search(r'class="([^"]*)"', attrs)
            names = (classes.group(1) if classes else "").split()
            if "disclosure" not in names and not any(menu in names for menu in MENU_CLASSES):
                missing.append(f"{path.name}: <details{attrs}>")
    assert missing == [], "这些展开块没有 disclosure 类(右侧箭头图标):\n" + "\n".join(missing)


def test_no_text_style_expand_hints_remain() -> None:
    offenders = []
    for path in TEMPLATES:
        text = path.read_text(encoding="utf-8")
        for summary in SUMMARY.findall(text):
            label = re.sub(r"<[^>]+>", "", summary).strip()
            if re.search(r"展开|收起|查看", label):
                offenders.append(f"{path.name}: <summary>{label}</summary>")
        # 摘要里不再放"展开配置…"这类提示文字
        if "plugin-toggle-summary-hint" in text:
            offenders.append(f"{path.name}: plugin-toggle-summary-hint")
    css = (ROOT / "static" / "css" / "style.css").read_text(encoding="utf-8")
    offenders += [f"style.css: {m}" for m in re.findall(r'content:\s*"(?:展开|收起|设置)"', css)]
    assert offenders == [], "\n".join(offenders)


def test_group_feature_toggle_is_an_icon_only_button() -> None:
    template = (ROOT / "templates" / "chatroom_detail.html").read_text(encoding="utf-8")
    buttons = re.findall(r'<button class="gf-toggle"[^>]*>(.*?)</button>', template, re.S)
    assert len(buttons) == 2 and all(label.strip() == "" for label in buttons)  # 没有"设置"之类的文字,图标由样式绘制
