"""群功能标签页渲染的基准对比(golden)。

群功能页的渲染逻辑要抽取成通用的"声明式配置"渲染器,供插件配置共用。抽取必须不改变群功能页的任何输出,
所以先把当前渲染结果固化成基准文件,之后每次改动都要和它逐字一致。

基准覆盖:每个已注册的群功能各取一个状态(开/关交替)、输入项取非默认值、密钥项已保存(页面只显示"已配置")。
比较前先做规整:去掉 CSRF 令牌、把空白折叠,所以缩进和换行的变化不算差异,标签、属性、文字的任何变化都算。

需要有意更新基准时:UPDATE_GOLDEN=1 python -m pytest tests/test_group_feature_render_golden.py
"""

from __future__ import annotations

import html as html_module
import os
import re
import tempfile
from pathlib import Path

os.environ.setdefault("LOG_DIR", tempfile.mkdtemp(prefix="gf-golden-logs-"))

import pytest

from app.models.entities import Channel, Chatroom
from app.services.group_features.registry import all_features, get_feature
from app.services.group_features.store import get_state, save_state
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
        elif field.kind == "select":
            # 下拉配置必须使用真实选项，才能覆盖选中态而不是无效值的规整结果。
            config[field.key] = field.options[-1][0]
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
    """基准覆盖开关、文本、数字、密钥、下拉及开启/关闭状态，避免空壳对比。"""

    text = GOLDEN.read_text(encoding="utf-8") if GOLDEN.exists() else ""
    if not text:
        pytest.skip("基准文件尚未生成")
    assert 'role="switch"' in text and 'type="number"' in text and 'type="password"' in text and 'type="text"' in text
    assert '<textarea name="cfg_join_template" rows="4" maxlength="2000">' in text
    assert "已配置，留空则保持不变" in text and " is-on" in text and "data-gf-depends" in text
    assert '<select name="cfg_card_thumb_source">' in text
    assert '<option value="member_avatar" selected>新成员头像</option>' in text


@pytest.mark.parametrize("first_source", ["member_avatar", "url"])
def test_welcome_card_image_source_and_custom_url_persist_through_the_page(feature_page, first_source) -> None:
    """真实表单可双向切换来源；重新打开正确选中、隐藏 URL，并始终保留已填地址。"""

    client, channel_id, room_id = feature_page
    csrf = _login(client, "admin")
    base = f"/channels/{channel_id}/chatrooms/{room_id}"
    for source in (first_source, "url" if first_source == "member_avatar" else "member_avatar"):
        response = client.post(base + "/features/member_change", data={
            "enabled": "true", "cfg_notify_join": "true", "cfg_card_enabled": "true",
            "cfg_card_title": "欢迎 {nickname}", "cfg_card_desc": "{now}",
            "cfg_card_url": "https://example.com/rules", "cfg_card_thumb_source": source,
            "cfg_card_thumb_url": "https://example.com/custom.jpg",
        }, headers={"X-CSRF-Token": csrf, "X-Requested-With": "fetch"})
        assert response.status_code == 200 and response.json()["ok"] is True
        html = client.get(base + "?tab=features").text
        assert f'<option value="{source}" selected>' in html
        assert 'name="cfg_card_thumb_url" value="https://example.com/custom.jpg"' in html
        label = re.search(r'<label[^>]*data-gf-show-when="cfg_card_thumb_source"[^>]*>', html)
        assert label is not None and 'data-gf-show-value="url"' in label.group()
        assert (' hidden' in label.group()) == (source == "member_avatar")
        assert "未获取头像时不带缩略图" in html


@pytest.mark.parametrize("title", ["{group_name} · <{nickname}>", ""])
def test_member_notification_templates_persist_only_in_the_current_group(feature_page, provider_page_context, title) -> None:
    """真实表单保存并刷新多行模板、花括号与空标题，另一群配置和头像来源独立保留。"""

    client, channel_id, room_id = feature_page
    ids = provider_page_context[1]
    with ids["factory"]() as db:
        room = db.get(Chatroom, room_id)
        other_room = Chatroom(tenant_id=room.tenant_id, bot_id=room.bot_id, channel_id=channel_id,
                             external_id="other@chatroom", name="另一个群", is_active=True)
        db.add(other_room)
        db.flush()
        save_state(other_room, "member_change", enabled=True, config={"notify_title": "另一群标题", "join_template": "另一群欢迎 {nickname}"})
        other_room_id = int(other_room.id)
        other_state = get_state(other_room, get_feature("member_change"))
        db.commit()

    csrf = _login(client, "admin")
    base = f"/channels/{channel_id}/chatrooms/{room_id}"
    templates = {
        "notify_title": title,
        "join_template": "欢迎 {nickname} 加入 {group_name}\n请先阅读 <群公告> & 群规\n时间：{now}",
        "leave_template": "{nickname} 离开了 {group_name}\n期待再见",
        "removed_template": "已将 {nickname} 移出 {group_name}\n{now}",
        "rename_template": "{nickname} 修改群昵称\n{old_nickname} → {new_nickname}\n{unknown}",
    }
    response = client.post(base + "/features/member_change", data={
        "enabled": "true", "cfg_notify_join": "true", "cfg_notify_leave": "true", "cfg_notify_rename": "true",
        **{"cfg_" + key: value for key, value in templates.items()},
        "cfg_card_enabled": "true", "cfg_card_title": "欢迎 {nickname}", "cfg_card_url": "https://example.com/rules",
        "cfg_card_thumb_source": "member_avatar", "cfg_card_thumb_url": "https://example.com/saved-cover.jpg",
    }, headers={"X-CSRF-Token": csrf, "X-Requested-With": "fetch"})
    assert response.status_code == 200 and response.json()["ok"] is True
    refreshed = client.get(base + "?tab=features")
    assert refreshed.status_code == 200
    rendered = refreshed.text
    assert f'name="cfg_notify_title" value="{html_module.escape(title, quote=True)}"' in rendered
    for key, value in templates.items():
        if key != "notify_title":
            field = re.search(r'<textarea name="cfg_' + key + r'"[^>]*>(.*?)</textarea>', rendered, re.S)
            assert field is not None and html_module.unescape(field.group(1)) == value
    assert '<option value="member_avatar" selected>新成员头像</option>' in rendered
    assert 'name="cfg_card_thumb_url" value="https://example.com/saved-cover.jpg"' in rendered
    with ids["factory"]() as db:
        enabled, config = get_state(db.get(Chatroom, room_id), get_feature("member_change"))
        assert enabled and {key: config[key] for key in templates} == templates
        assert get_state(db.get(Chatroom, other_room_id), get_feature("member_change")) == other_state
    other_page = client.get(f"/channels/{channel_id}/chatrooms/{other_room_id}?tab=features")
    assert other_page.status_code == 200
    assert "另一群欢迎 {nickname}" in other_page.text and "请先阅读" not in other_page.text
