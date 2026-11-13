from __future__ import annotations

import base64
import io
import json
import random
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from datetime import timedelta
from threading import Barrier

import pytest
from PIL import Image
from sqlalchemy import func, select

from test_existing_stateful_plugins import _message_sign_request, message_sign_fixture  # noqa: F401
from app.core.tenant_context import tenant_scope
from app.core.tenant_db import tenant_session
from app.core.time import now_shanghai
from app.models.entities import Chatroom, ChatroomMember, PluginStateRecord, PointsTransaction, SignInHistory, SignInRecord, WorkWechatContact
from app.services.plugin_directory import get_discovered_plugin, load_directory_plugin
from app.services.plugin_runtime.registry import get_plugin_schema
from app.services.points import PointsResult


@pytest.fixture()
def sign_module():
    return load_directory_plugin("message_sign", force=True).instance._module


def request_for(db, identity, *, sender="fortune-user", content="签到", **config):
    request = _message_sign_request(
        db, identity, content=content, sender_id=sender, sender_name="运势用户",
    )
    return replace(request, config={**request.config, "fortune_image_enabled": False, **config})


def test_manifest_keeps_commands_and_exposes_fortune_configuration():
    plugin = get_discovered_plugin("message_sign", force=True)
    assert plugin.is_valid
    assert plugin.matcher.commands == ("签到", "补签")
    fields = {field.name: field for field in get_plugin_schema("message_sign").fields}
    assert len(fields) == 10
    assert fields["background_url"].default == ""  # 默认使用随包背景,不在每次签到时访问外网
    assert fields["fortune_enabled"].default is True
    assert fields["fortune_image_enabled"].default is True
    assert fields["sign_exp_max"].default == 100


def test_daily_fortune_is_stable_and_does_not_change_global_rng(sign_module):
    current = now_shanghai()
    before = random.getstate()
    first = sign_module.daily_fortune(1, "room", "wxid:/../user", current)
    second = sign_module.daily_fortune(1, "room", "wxid:/../user", current.replace(hour=23))
    assert first == second
    assert random.getstate() == before
    assert 0 <= first["luck"] <= 99
    assert len({e["name"] for e in first["good_events"] + first["bad_events"]}) == 4
    assert first != sign_module.daily_fortune(1, "room", "wxid:/../user", current + timedelta(days=1))
    assert first != sign_module.daily_fortune(2, "room", "wxid:/../user", current)
    assert first != sign_module.daily_fortune(1, "other-room", "wxid:/../user", current)


def test_level_progress_uses_current_level_interval_and_handles_maximum(sign_module):
    level = sign_module.level_info(750)
    assert level["level"] == 1 and level["progress"] == 50
    assert level["next_exp"] == 1000
    maximum = sign_module.level_info(2_000_000)
    assert maximum["level"] == 20 and maximum["next_exp"] is None
    assert maximum["progress"] == 100


def test_sign_rewards_once_across_new_messages_and_plugin_reload(message_sign_fixture, sign_module):
    factory, identity = message_sign_fixture
    with tenant_scope(identity["tenant_id"]), tenant_session(factory) as db:
        first = sign_module.execute_message_action(db, request_for(db, identity, sign_exp_min=25, sign_exp_max=25))
        module = load_directory_plugin("message_sign", force=True).instance._module
        repeated = module.execute_message_action(db, request_for(db, identity))
        assert first.ok and repeated.ok
        assert first.output["points_awarded"] == 4
        assert first.output["exp_awarded"] == first.output["total_exp"] == 25
        assert repeated.output["exp_awarded"] == repeated.output["points_awarded"] == 0
        assert repeated.output["total_exp"] == 25
        assert repeated.output["fortune"] == first.output["fortune"]
        assert repeated.output["todays_rank"] == 1
        assert db.scalar(select(func.count(SignInHistory.id))) == 1
        assert db.scalar(select(func.count(PointsTransaction.id))) == 1
        assert db.scalar(select(func.count(PluginStateRecord.id))) == 1
        assert first.output["replies"][0]["type"] == "text"
        assert "宜：" in repeated.message and "忌：" in repeated.message


def test_next_day_adds_experience_and_keeps_one_profile(message_sign_fixture, sign_module, monkeypatch):
    factory, identity = message_sign_fixture
    current = now_shanghai()
    monkeypatch.setattr(sign_module, "now_shanghai", lambda: current)
    with tenant_scope(identity["tenant_id"]), tenant_session(factory) as db:
        first = sign_module.execute_message_action(db, request_for(db, identity, sign_exp_min=10, sign_exp_max=10))
        current += timedelta(days=1)
        second = sign_module.execute_message_action(db, request_for(db, identity, sign_exp_min=10, sign_exp_max=10))
        assert second.output["total_exp"] == 20 and second.output["exp_awarded"] == 10
        assert second.output["signin_streak"] == second.output["total_sign_count"] == 2
        assert second.output["points_awarded"] == 5
        assert second.output["fortune"]["date"] != first.output["fortune"]["date"]
        states = db.scalars(select(PluginStateRecord)).all()
        assert len(states) == 1
        assert json.loads(states[0].payload_json)["daily"]["date"] == current.date().isoformat()


def test_existing_today_record_does_not_gain_retroactive_rewards(message_sign_fixture, sign_module):
    factory, identity = message_sign_fixture
    with tenant_scope(identity["tenant_id"]), tenant_session(factory) as db:
        db.add(SignInRecord(user_id="fortune-user", group_id="sign-room", user_name="老用户",
                            sign_stat=now_shanghai(), points=85, total_sign_count=12, signin_streak=5))
        db.commit()
        result = sign_module.execute_message_action(db, request_for(db, identity))
        record = db.scalar(select(SignInRecord))
        assert result.output["provider_status"] == "already_signed"
        assert result.output["total_exp"] == result.output["exp_awarded"] == 0
        assert record.points == 85 and record.total_sign_count == 12 and record.signin_streak == 5
        assert db.scalar(select(func.count(PointsTransaction.id))) == 0
        assert db.scalar(select(func.count(SignInHistory.id))) == 0


def test_fortune_can_be_disabled_without_creating_state(message_sign_fixture, sign_module):
    factory, identity = message_sign_fixture
    with tenant_scope(identity["tenant_id"]), tenant_session(factory) as db:
        result = sign_module.execute_message_action(db, request_for(db, identity, fortune_enabled="false"))
        assert result.ok and result.output["points_awarded"] == 4
        assert "fortune" not in result.output and "今日运势" not in result.message
        assert db.scalar(select(func.count(PluginStateRecord.id))) == 0


def test_turning_fortune_on_after_sign_does_not_reward_twice(message_sign_fixture, sign_module):
    factory, identity = message_sign_fixture
    with tenant_scope(identity["tenant_id"]), tenant_session(factory) as db:
        sign_module.execute_message_action(db, request_for(db, identity, fortune_enabled=False))
        result = sign_module.execute_message_action(db, request_for(db, identity, fortune_enabled=True))
        assert result.output["provider_status"] == "already_signed"
        assert result.output["total_exp"] == result.output["exp_awarded"] == 0
        assert db.scalar(select(func.count(PointsTransaction.id))) == 1


def test_failed_render_falls_back_after_reward_commit(message_sign_fixture, sign_module, monkeypatch):
    factory, identity = message_sign_fixture
    def fail(view):
        with tenant_scope(identity["tenant_id"]), tenant_session(factory) as other:
            assert other.scalar(select(func.count(SignInHistory.id))) == 1
            assert other.scalar(select(func.count(PluginStateRecord.id))) == 1
        raise RuntimeError("browser missing")
    monkeypatch.setattr(sign_module, "_render_fortune_card", fail)
    with tenant_scope(identity["tenant_id"]), tenant_session(factory) as db:
        result = sign_module.execute_message_action(db, request_for(db, identity, fortune_image_enabled=True))
        repeated = sign_module.execute_message_action(db, request_for(db, identity, fortune_image_enabled=True))
        assert result.ok and result.output["card_status"] == "failed"
        assert result.output["replies"][0]["type"] == "text"
        assert "今日运势" in result.message and "宜：" in result.message and "忌：" in result.message
        assert repeated.output["total_exp"] == result.output["total_exp"]
        assert repeated.output["exp_awarded"] == 0
        assert db.scalar(select(func.count(PointsTransaction.id))) == 1


def test_rendered_card_uses_standard_image_reply(message_sign_fixture, sign_module, monkeypatch):
    factory, identity = message_sign_fixture
    encoded = base64.b64encode(b"test-image").decode("ascii")
    captured = []
    def render(view):
        captured.append(view)
        return encoded
    monkeypatch.setattr(sign_module, "_render_fortune_card", render)
    with tenant_scope(identity["tenant_id"]), tenant_session(factory) as db:
        result = sign_module.execute_message_action(db, request_for(db, identity, fortune_image_enabled=True, background_url="https://example.com/background.jpg"))
        assert result.output["card_status"] == "rendered"
        assert captured[0]["background_url"] == "https://example.com/background.jpg"
        assert result.output["replies"] == [{"type": "image", "base64": encoded}]
        assert captured[0]["username"] == "运势用户"
        assert captured[0]["points"] == captured[0]["points_awarded"] == 4


def test_points_failure_rolls_back_experience_and_sign(message_sign_fixture, sign_module, monkeypatch):
    factory, identity = message_sign_fixture
    monkeypatch.setattr(sign_module.PointsService, "earn", lambda *a, **k: PointsResult(ok=False, message="failed"))
    with tenant_scope(identity["tenant_id"]), tenant_session(factory) as db:
        request = request_for(db, identity)
        db.commit()
        result = sign_module.execute_message_action(db, request)
        assert not result.ok and result.output["provider_status"] == "points_failed"
        assert db.scalar(select(func.count(PluginStateRecord.id))) == 0
        assert db.scalar(select(func.count(SignInRecord.id))) == 0
        assert db.scalar(select(func.count(SignInHistory.id))) == 0


def test_makeup_does_not_change_experience_or_fortune(message_sign_fixture, sign_module, monkeypatch):
    factory, identity = message_sign_fixture
    current = now_shanghai() - timedelta(days=2)
    monkeypatch.setattr(sign_module, "now_shanghai", lambda: current)
    with tenant_scope(identity["tenant_id"]), tenant_session(factory) as db:
        sign_module.execute_message_action(db, request_for(db, identity))
        current += timedelta(days=2)
        signed = sign_module.execute_message_action(db, request_for(db, identity))
        before = db.scalar(select(PluginStateRecord)).payload_json
        makeup = sign_module.execute_message_action(db, request_for(db, identity, content="补签", makeup_cost=1))
        assert makeup.ok and makeup.output["signin_streak"] == 3
        assert db.scalar(select(PluginStateRecord)).payload_json == before
        repeated = sign_module.execute_message_action(db, request_for(db, identity))
        assert repeated.output["fortune"] == signed.output["fortune"]
        assert repeated.output["total_exp"] == signed.output["total_exp"]


def test_group_profiles_do_not_share_experience(message_sign_fixture, sign_module):
    factory, identity = message_sign_fixture
    with tenant_scope(identity["tenant_id"]), tenant_session(factory) as db:
        first = sign_module.execute_message_action(db, request_for(db, identity, sign_exp_min=10, sign_exp_max=10))
        other = {**identity, "conversation_id": identity["other_conversation_id"]}
        second = sign_module.execute_message_action(db, request_for(db, other, sign_exp_min=20, sign_exp_max=20))
        assert first.output["total_exp"] == 10 and second.output["total_exp"] == 20
        assert first.output["points"] == second.output["points"] == 4
        assert db.scalar(select(func.count(PluginStateRecord.id))) == 2


def test_concurrent_signs_from_different_messages_reward_once(message_sign_fixture, sign_module, monkeypatch):
    factory, identity = message_sign_fixture
    with tenant_scope(identity["tenant_id"]), tenant_session(factory) as db:
        requests = [request_for(db, identity, sign_exp_min=10, sign_exp_max=10) for _ in range(2)]
        db.commit()
    original = sign_module._require_trusted_group_message
    barrier = Barrier(2)
    def resolve(db, context):
        result = original(db, context)
        barrier.wait(timeout=10)
        return result
    monkeypatch.setattr(sign_module, "_require_trusted_group_message", resolve)
    def execute(request):
        with tenant_scope(identity["tenant_id"]), tenant_session(factory) as db:
            return sign_module.execute_message_action(db, request)
    with ThreadPoolExecutor(max_workers=2) as executor:
        results = list(executor.map(execute, requests))
    assert sorted(r.output["provider_status"] for r in results) == ["already_signed", "signed"]
    assert sum(r.output["exp_awarded"] for r in results) == 10
    with tenant_scope(identity["tenant_id"]), tenant_session(factory) as db:
        assert db.scalar(select(func.count(SignInHistory.id))) == 1
        assert db.scalar(select(func.count(PointsTransaction.id))) == 1
        assert json.loads(db.scalar(select(PluginStateRecord)).payload_json)["total_exp"] == 10


def test_experience_config_is_bounded_and_swaps_reversed_limits(sign_module):
    config = sign_module._load_config({"sign_exp_min": 90, "sign_exp_max": 10, "fortune_image_enabled": "off"})
    assert (config.sign_exp_min, config.sign_exp_max) == (10, 90)
    assert not config.fortune_image_enabled
    config = sign_module._load_config({"sign_exp_min": -10, "sign_exp_max": 10**9})
    assert (config.sign_exp_min, config.sign_exp_max) == (1, 100000)


def test_card_embeds_local_assets_and_escapes_nickname(sign_module):
    current = now_shanghai()
    view = {
        "fortune": sign_module.daily_fortune(1, "room", "user", current),
        "level": sign_module.level_info(750), "username": '<script>alert(1)</script>',
        "provider_status": "signed", "points_awarded": 4, "exp_awarded": 20,
        "total_exp": 750, "todays_rank": 2, "signin_streak": 2,
        "total_sign_count": 10, "points": 100,
    }
    markup = sign_module._fortune_module.build_card_html(view)
    assert '<script>' not in markup and '&lt;script&gt;' in markup
    assert 'data:font/woff2;base64,' in markup
    assert 'data:image/jpeg;base64,' in markup and 'data:image/png;base64,' in markup
    assert 'http:' not in markup and 'https:' not in markup and 'file:' not in markup


def test_card_reuses_original_upstream_layout_and_repeat_status(sign_module, monkeypatch):
    module = sign_module._fortune_module
    current = now_shanghai().replace(hour=8)
    monkeypatch.setattr(module, "now_shanghai", lambda: current)
    fortune = sign_module.daily_fortune(1, "room", "user", current)
    view = {
        "fortune": fortune, "level": sign_module.level_info(750),
        "username": "本群昵称", "provider_status": "signed",
        "points_awarded": 4, "exp_awarded": 20, "total_exp": 750,
        "points": 100,
    }
    markup = module.build_card_html(view)
    assert '<div class="container">' in markup
    assert 'height: 389px' in markup and 'margin-top: -48px' in markup
    assert 'class="dateInfo"' in markup and 'backdrop-filter: blur(6px)' in markup
    assert 'class="levelInfo"' in markup and 'height: 29px' in markup
    assert 'class="toDoBg"' in markup and 'background-color:#D94A3F' in markup
    assert '本群昵称<br>' in markup and '早上好' in markup
    assert current.strftime('%m/%d') in markup and '750/1000' in markup
    assert '签到成功！' in markup and '+20' in markup and '+4' in markup
    assert markup.count('data:image/svg+xml;base64,') == 4
    assert markup.index('class="hitokoto"') < markup.index('class="content"')
    assert '随机生成 请勿迷信 | AstrBot' in markup
    repeated = module.build_card_html({**view, "provider_status": "already_signed"})
    assert '今天已经签到过了哦' in repeated and '累计 积分 100' in repeated
    repeated_signin = repeated.split('<div class="signin">', 1)[1].split('</div>', 1)[0]
    assert '+20' not in repeated_signin and '+4' not in repeated_signin
    assert repeated.count('data:image/svg+xml;base64,') == 2


def synced_member(db, identity, *, group="sign-room", sender="fortune-user", display="本群群昵称", nickname="成员昵称", avatar="https://example.com/member.png"):
    room = Chatroom(tenant_id=identity["tenant_id"], bot_id=identity["bot_id"],
                    channel_id=identity["channel_id"], external_id=group, name="测试群")
    db.add(room)
    db.flush()
    member = ChatroomMember(
        tenant_id=identity["tenant_id"], bot_id=identity["bot_id"],
        channel_id=identity["channel_id"], chatroom_id=room.id, member_external_id=sender,
        display_name=display, nickname=nickname, extra_json=json.dumps({"avatar_url": avatar}),
    )
    db.add(member)
    db.flush()
    return member


def test_card_uses_synced_group_profile_and_refreshes_on_repeat(message_sign_fixture, sign_module, monkeypatch):
    factory, identity = message_sign_fixture
    captured = []
    def render(view):
        captured.append(view)
        return base64.b64encode(b"image").decode("ascii")
    monkeypatch.setattr(sign_module, "_render_fortune_card", render)
    with tenant_scope(identity["tenant_id"]), tenant_session(factory) as db:
        member = synced_member(db, identity)
        db.add(WorkWechatContact(tenant_id=identity["tenant_id"], channel_id=identity["channel_id"],
                                 contact_external_id="fortune-user", display_name="好友备注",
                                 avatar_url="https://example.com/friend.png"))
        request = request_for(db, identity, fortune_image_enabled=True)
        first = sign_module.execute_message_action(db, request)
        assert captured[0]["username"] == "本群群昵称"
        assert captured[0]["avatar_url"] == "https://example.com/member.png"
        assert db.scalar(select(SignInRecord)).user_name == "本群群昵称"
        assert db.get(sign_module.Message, request.context.message_id).sender_name == "运势用户"
        member.display_name = "新的群昵称"
        member.extra_json = json.dumps({"avatar_url": "https://example.com/new.png"})
        db.commit()
        repeated = sign_module.execute_message_action(db, request_for(db, identity, fortune_image_enabled=True))
        assert captured[1]["username"] == "新的群昵称"
        assert captured[1]["avatar_url"] == "https://example.com/new.png"
        assert repeated.output["fortune"] == first.output["fortune"]
        assert repeated.output["exp_awarded"] == 0


def test_group_profile_skips_raw_id_and_keeps_avatar(message_sign_fixture, sign_module):
    factory, identity = message_sign_fixture
    with tenant_scope(identity["tenant_id"]), tenant_session(factory) as db:
        synced_member(db, identity, sender="wxid_a1", display="wxid_a1", nickname="真正的昵称")
        request = request_for(db, identity, sender="wxid_a1")
        trusted, error = sign_module._require_trusted_group_message(db, request.context)
        assert error is None and trusted.sender_name == "真正的昵称"
        assert trusted.avatar_url == "https://example.com/member.png"


def test_sign_profile_is_scoped_to_current_group_and_channel(message_sign_fixture, sign_module):
    factory, identity = message_sign_fixture
    with tenant_scope(identity["tenant_id"]), tenant_session(factory) as db:
        synced_member(db, identity, group="other-sign-room", display="别群昵称", avatar="https://example.com/other.png")
        request = request_for(db, identity)
        trusted, error = sign_module._require_trusted_group_message(db, request.context)
        assert error is None and trusted.sender_name == "运势用户" and trusted.avatar_url == ""
        member = synced_member(db, identity, display="当前群昵称")
        trusted, error = sign_module._require_trusted_group_message(db, request.context)
        assert error is None and trusted.sender_name == "当前群昵称"
        assert trusted.avatar_url == "https://example.com/member.png"
        # A foreign-channel record cannot supply the current channel's profile.
        member.channel_id = 99999
        db.flush()
        trusted, error = sign_module._require_trusted_group_message(db, request.context)
        assert error is None and trusted.sender_name == "运势用户" and trusted.avatar_url == ""


class AvatarResponse(io.BytesIO):
    def __init__(self, body, *, status=200):
        super().__init__(body)
        self.status = status
        self.headers = {"content-type": "image/png", "content-length": str(len(body))}


def avatar_png(size=(320, 200)):
    buffer = io.BytesIO()
    Image.new("RGB", size, (24, 155, 122)).save(buffer, format="PNG")
    return buffer.getvalue()


def test_synced_avatar_is_downloaded_bounded_and_embedded_as_png(sign_module, monkeypatch):
    module = sign_module._fortune_module
    calls = []
    def fetch(url, *, timeout):
        calls.append((url, timeout))
        return AvatarResponse(avatar_png())
    monkeypatch.setattr(module, "open_public_url", fetch)
    encoded = module._avatar_data_uri("https://example.com/member.png")
    # 超时会被扣掉已经用掉的时间（如 1.99998），所以只断言地址正确且不超过 2 秒上限
    assert [url for url, _ in calls] == ["https://example.com/member.png"] and 0 < calls[0][1] <= 2
    assert encoded != module._asset_uri("default_avatar.png")
    with Image.open(io.BytesIO(base64.b64decode(encoded.split(",", 1)[1]))) as picture:
        assert picture.format == "PNG" and picture.size == (168, 105)
        assert picture.getpixel((0, 0)) == (24, 155, 122, 255)


@pytest.mark.parametrize("body,status", [
    (b"not an image", 200), (b"error", 404),
    (b"x" * (2 * 1024 * 1024 + 1), 200),
])
def test_broken_avatar_falls_back_without_failing_card(sign_module, monkeypatch, body, status):
    module = sign_module._fortune_module
    monkeypatch.setattr(module, "open_public_url", lambda *a, **k: AvatarResponse(body, status=status))
    assert module._avatar_data_uri("https://example.com/member.png") == module._asset_uri("default_avatar.png")


def test_oversized_avatar_pixels_are_rejected_before_decode(sign_module, monkeypatch):
    module = sign_module._fortune_module
    monkeypatch.setattr(module, "open_public_url", lambda *a, **k: AvatarResponse(avatar_png((2001, 2001))))
    assert module._avatar_data_uri("https://example.com/member.png") == module._asset_uri("default_avatar.png")


def test_private_avatar_url_falls_back_without_network_request(sign_module):
    module = sign_module._fortune_module
    assert module._avatar_data_uri("http://127.0.0.1/avatar.png") == module._asset_uri("default_avatar.png")


def test_avatar_timeout_keeps_default_and_does_not_escape(sign_module, monkeypatch):
    module = sign_module._fortune_module
    def fail(*a, **k):
        raise TimeoutError("avatar timeout")
    monkeypatch.setattr(module, "open_public_url", fail)
    assert module._avatar_data_uri("https://example.com/member.png") == module._asset_uri("default_avatar.png")


def test_background_is_normalized_and_embedded(sign_module, monkeypatch):
    module = sign_module._fortune_module
    calls = []
    def fetch(url, *, timeout):
        calls.append((url, timeout))
        return AvatarResponse(avatar_png((1600, 900)))
    monkeypatch.setattr(module, "open_public_url", fetch)
    uri = module._background_data_uri("https://example.com/background.png")
    assert [url for url, _ in calls] == ["https://example.com/background.png"] and 0 < calls[0][1] <= 3  # 同上：不超过 3 秒上限
    assert uri != module._asset_uri("default_background.jpg")
    with Image.open(io.BytesIO(base64.b64decode(uri.split(",", 1)[1]))) as picture:
        assert picture.format == "JPEG" and picture.size == (1200, 675)


@pytest.mark.parametrize("body,status", [
    (b"invalid", 200), (b"error", 404),
    (b"x" * (5 * 1024 * 1024 + 1), 200),
    (avatar_png((4001, 3000)), 200),
])
def test_background_failure_uses_bundled_image(sign_module, monkeypatch, body, status):
    module = sign_module._fortune_module
    monkeypatch.setattr(module, "open_public_url", lambda *a, **k: AvatarResponse(body, status=status))
    assert module._background_data_uri("https://example.com/background.png") == module._asset_uri("default_background.jpg")


def test_private_background_and_empty_url_use_bundled_image(sign_module):
    module = sign_module._fortune_module
    for url in ("", "http://127.0.0.1/background.jpg", "file:///etc/passwd"):
        assert module._background_data_uri(url) == module._asset_uri("default_background.jpg")


def test_render_card_embeds_both_downloaded_images(sign_module, monkeypatch):
    module = sign_module._fortune_module
    captured = []
    monkeypatch.setattr(module, "_avatar_data_uri", lambda url: "avatar:" + url)
    monkeypatch.setattr(module, "_background_data_uri", lambda url: "background:" + url)
    monkeypatch.setattr(module, "build_card_html", lambda view: captured.append(view) or "markup")
    async def render(markup):
        assert markup == "markup"
        return b"png"
    monkeypatch.setattr(module, "_render_png", render)
    assert module.render_card({"avatar_url": "a", "background_url": "b"}) == base64.b64encode(b"png").decode()
    assert captured[0]["avatar_data_uri"] == "avatar:a"
    assert captured[0]["background_data_uri"] == "background:b"


def test_render_slots_are_capped_and_a_busy_renderer_falls_back_to_text(sign_module, monkeypatch):
    """同时渲染的 Chromium 有上限;等不到名额时抛错,调用方会改回复文字。"""

    module = sign_module._fortune_module
    monkeypatch.setattr(module, "_RENDER_WAIT_SECONDS", 0.05)
    held = [module._RENDER_SLOTS.acquire(timeout=1) for _ in range(2)]  # 把两个名额都占住
    try:
        assert all(held)
        with pytest.raises(RuntimeError, match="fortune_render_busy"):
            module.render_card({"avatar_url": "", "background_url": ""})
    finally:
        for _ in held:
            module._RENDER_SLOTS.release()
    assert module._RENDER_SLOTS.acquire(timeout=1)  # 名额释放后恢复可用
    module._RENDER_SLOTS.release()
