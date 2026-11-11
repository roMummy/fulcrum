"""群功能标签页渲染的基准对比(golden)。

群功能页的渲染逻辑要抽取成通用的"声明式配置"渲染器,供插件配置共用。抽取必须不改变群功能页的任何输出,
所以先把当前渲染结果固化成基准文件,之后每次改动都要和它逐字一致。

基准覆盖:每个已注册的群功能各取一个状态(开/关交替)、输入项取非默认值、密钥项已保存(页面只显示"已配置")。
比较前先做规整:去掉 CSRF 令牌、把空白折叠,所以缩进和换行的变化不算差异,标签、属性、文字的任何变化都算。

需要有意更新基准时:UPDATE_GOLDEN=1 python -m pytest tests/test_group_feature_render_golden.py
"""

from __future__ import annotations

import os
import re
import tempfile
from pathlib import Path

os.environ.setdefault("LOG_DIR", tempfile.mkdtemp(prefix="gf-golden-logs-"))

import pytest

from app.models.entities import Channel, Chatroom
from app.services.group_features.registry import all_features
from app.services.group_features.store import save_state
from test_provider_node_pages import _login, provider_page_context  # noqa: F401  (provider_page_context 是 fixture)

GOLDEN = Path(__file__).parent / "golden" / "group_features_tab.html"


def _normalize(html: str) -> str:
    """去掉每次渲染都不同的内容(CSRF 令牌),并折叠空白,只保留结构、属性和文字。"""

    html = re.sub(r'(name="csrf_token" value=")[^"]*(")', r"\1\2", html)
    html = re.sub(r'(<meta name="csrf-token" content=")[^"]*(")', r"\1\2", html)
    html = re.sub(r"\s+", " ", html)
    html = re.sub(r">\s+<", "><", html)
    return html.strip()


def _sample_config(feature, index: int) -> dict:
    """给每个输入项一个非默认值:文本给示例文字,数字取范围内的值,开关取反,密钥填一个(页面不回显)。"""

    config = {}
    for field in feature.fields:
        if field.kind == "bool":
            config[field.key] = not bool(field.default)
        elif field.kind == "number":
            low = field.minimum if field.minimum is not None else 0
            high = field.maximum if field.maximum is not None else low + 50
            config[field.key] = min(max(low + 3, low), high)
        elif field.kind == "secret":
            config[field.key] = f"secret-{feature.key}"
        else:
            config[field.key] = f"示例值-{field.key}"
    return config


@pytest.fixture()
def feature_page(provider_page_context):  # noqa: F811
    client, ids = provider_page_context
    with ids["factory"]() as db:
        channel = Channel(tenant_id=ids["alpha"], bot_id=ids["bot"], name="微桥账号", channel_type="wechat_webridge", is_enabled=True)
        db.add(channel)
        db.flush()
        room = Chatroom(tenant_id=ids["alpha"], bot_id=ids["bot"], channel_id=channel.id, external_id="10000000001@chatroom", name="示例群", status="synced", management_status="enabled", is_active=True)
        db.add(room)
        db.flush()
        for index, feature in enumerate(all_features()):
            save_state(room, feature.key, enabled=index % 2 == 0, config=_sample_config(feature, index))
        db.commit()
        return client, int(channel.id), int(room.id)


def _render(client, channel_id: int, room_id: int) -> str:
    _login(client, "admin")
    html = client.get(f"/channels/{channel_id}/chatrooms/{room_id}?tab=features").text
    start = html.index('<div class="gf-list">')
    end = html.rindex("</section>")  # 群详情工作区(work-surface)的结束处;不能取第一个,小节(gf-section)也是 section
    return _normalize(html[start:end])


def test_group_feature_tab_matches_the_golden_render(feature_page) -> None:
    client, channel_id, room_id = feature_page
    rendered = _render(client, channel_id, room_id)
    assert rendered.count('data-gf-card') >= 4  # 至少渲染出运行模式和若干群功能卡片,避免基准是个空壳
    if os.environ.get("UPDATE_GOLDEN") == "1" or not GOLDEN.exists():
        GOLDEN.parent.mkdir(parents=True, exist_ok=True)
        # 每个标签换行写出,基准文件的 diff 可读
        GOLDEN.write_text(rendered.replace("><", ">\n<") + "\n", encoding="utf-8")
        pytest.skip("已(重新)生成基准文件")
    expected = _normalize(GOLDEN.read_text(encoding="utf-8"))
    if rendered != expected:
        # 找出第一处差异,给出可读的上下文
        position = next((i for i, (a, b) in enumerate(zip(rendered, expected)) if a != b), min(len(rendered), len(expected)))
        raise AssertionError(
            "群功能页渲染和基准不一致(抽取渲染器不能改变任何输出)。\n"
            f"首个差异位置 {position}:\n  现在: …{rendered[max(0, position - 80): position + 120]}…\n  基准: …{expected[max(0, position - 80): position + 120]}…"
        )


def test_the_golden_covers_every_kind_of_field_and_state() -> None:
    """基准必须覆盖开关/输入框/数字/密钥/文本域和开启/关闭两种状态,否则对比没有意义。"""

    text = GOLDEN.read_text(encoding="utf-8") if GOLDEN.exists() else ""
    if not text:
        pytest.skip("基准文件尚未生成")
    assert 'role="switch"' in text and 'type="number"' in text and 'type="password"' in text and 'type="text"' in text
    assert "已配置，留空则保持不变" in text and " is-on" in text and "data-gf-depends" in text
