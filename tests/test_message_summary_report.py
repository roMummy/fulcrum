from __future__ import annotations

from datetime import datetime, timedelta, timezone
from unittest.mock import patch

from app.services.plugin_runtime.messages import PluginMessageBatch, PluginMessageRecord
from app.services.web_screenshots import ScreenshotResult
from plugins.message_summary.renderer import capture_summary_image, render_summary_html
from plugins.message_summary.report import (
    DailyActivity,
    MemberChangeStats,
    MessageSummaryReport,
    SpeakerContribution,
    SummaryAnalytics,
    SummaryTopic,
    build_model_prompt,
    build_structured_report,
    build_summary_analytics,
    parse_model_response,
    report_from_dict,
)
from app.services.plugin_runtime.text_generation import PluginTextGenerationResult
import plugins.message_summary.renderer as summary_renderer


TZ = timezone(timedelta(hours=8))


def _batch() -> PluginMessageBatch:
    messages = (
        PluginMessageRecord(1, datetime(2026, 8, 30, 8, 0, tzinfo=TZ), "in", "alice", "Alice", "方案 <A>", "Alice: 方案 <A>"),
        PluginMessageRecord(2, datetime(2026, 8, 30, 9, 0, tzinfo=TZ), "in", "bob", "Bob", "确认上线", "Bob: 确认上线"),
        PluginMessageRecord(3, datetime(2026, 8, 30, 10, 0, tzinfo=TZ), "out", None, "机器人", "收到", "机器人: 收到"),
    )
    return PluginMessageBatch(50, "产品研发群", "telegram", datetime(2026, 8, 30, tzinfo=TZ), datetime(2026, 8, 31, tzinfo=TZ), messages)


def test_local_statistics_keep_direction_and_daily_boundaries() -> None:
    analytics = build_summary_analytics(None, _batch())
    assert analytics.message_count == 3
    assert analytics.active_member_count == 2
    assert analytics.group_member_count is None
    assert analytics.active_ratio_percent is None
    assert [item.message_count for item in analytics.contributions] == [1, 1]
    assert analytics.daily_activity[0].message_count == 2
    assert analytics.daily_activity[0].active_member_count == 2
    assert analytics.daily_activity[1].message_count == 0


def test_model_contract_recounts_indexes_and_discards_untrusted_quotes() -> None:
    batch = _batch()
    analytics = build_summary_analytics(None, batch)
    prompt = build_model_prompt(batch, analytics)
    assert "speaker_key=" in prompt
    assert "消息 3 条" in prompt
    parsed = parse_model_response(
        "```json\n"
        '{"headline":"上线准备","intro":"已确认","topics":[{"title":"方案","summary":"归纳","message_indexes":[1,1,99],"participant_ids":[],"quotes":["方案 <A>","模型编造"],"market_tags":[{"label":"指数","change_pct":1.2}]}],"top_topics":[]}'
        "\n```",
        batch.messages,
    )
    topic = parsed["topics"][0]
    assert topic.message_indexes == (1,)
    assert topic.message_count == 1
    assert topic.quotes == ("方案 <A>",)
    assert topic.market_tags[0].change_pct == 1.2


def test_summary_html_escapes_model_text_and_has_four_demo_sections() -> None:
    analytics = SummaryAnalytics(
        3,
        2,
        None,
        None,
        (SpeakerContribution("a", "<成员>", "", 2, 66.7), SpeakerContribution("b", "成员 B", "", 1, 33.3)),
        (DailyActivity("2026-08-30", 3, 2, 50.0),),
        MemberChangeStats(1, 0),
    )
    topic = SummaryTopic("热点 &", "归纳 <script>", (1,), 1, ("<成员>",), ("方案 <A>",), ())
    report = MessageSummaryReport("群 <x>", "2026-08-30", "2026-08-30", "标题 <x>", "导语", analytics, (topic,), (topic,))
    html = render_summary_html(report)
    assert 'id="hot-topics"' in html
    assert 'id="top-topics"' in html
    assert 'id="contribution-donut"' in html
    assert 'id="daily-activity"' in html
    assert '<div id="hot-word-cloud" class="hot-cloud chart-container"' in html
    assert 'aria-label="群聊热点词云"' in html
    assert 'border-radius: 10px' in html
    assert "&lt;script&gt;" in html
    assert "<script>归纳" not in html
    assert "window.FulcrumViz" in html
    assert "runtime.cloudLayout" in html
    assert 'class="daily-chart-meta"' in html
    assert 'class="daily-x-labels' in html
    assert "消息量（条）" in html
    assert "活跃群友占比（%）" in html
    assert "<script src=" not in html
    assert len(html.encode("utf-8")) < 1024 * 1024
    assert "width: 1260px" in html
    assert "zoom: .85" in html


def test_hot_cloud_uses_local_topic_data_and_library_layout() -> None:
    analytics = SummaryAnalytics(
        300,
        2,
        None,
        None,
        (),
        (DailyActivity("2026-08-30", 300, 2, None),),
        MemberChangeStats(),
    )
    topics = (
        SummaryTopic("股票", "", (1, 2, 3), 3, (), (), ()),
        SummaryTopic("游戏", "", (4,), 1, (), (), ()),
    )
    report = MessageSummaryReport("群", "2026-08-30", "2026-08-30", "标题", "导语", analytics, topics, topics)
    html = render_summary_html(report)
    assert '"name":"股票","value":3,"messageCount":3' in html
    assert '"name":"游戏","value":1,"messageCount":1' in html
    assert "runtime.cloudLayout" in html
    assert 'class="cloud-connector"' not in html
    assert 'src="data:image/png;base64,' not in html


def test_capture_summary_image_uses_shared_kitesurf_request() -> None:
    analytics = SummaryAnalytics(1, 1, None, None, (), (DailyActivity("2026-08-30", 1, 1, None),), MemberChangeStats())
    report = MessageSummaryReport("群", "2026-08-30", "2026-08-30", "标题", "导语", analytics, (), ())
    captured: dict[str, object] = {}
    result = ScreenshotResult(b"png", "image/png", 1080, 900, 12)
    def fake_capture(request):
        captured["request"] = request
        return result

    with patch("plugins.message_summary.renderer.capture_screenshot", side_effect=fake_capture):
        actual = capture_summary_image(report)
    request = captured["request"]
    assert actual is result
    assert request.width == 1080
    assert request.full_page is True
    assert request.format == "png"


def test_summary_capture_prepares_real_and_missing_avatars(monkeypatch) -> None:
    """截图前把外部头像替换为内嵌图片，缺失头像使用默认图。"""

    analytics = SummaryAnalytics(
        2,
        1,
        None,
        None,
        (SpeakerContribution("a", "成员", "https://avatar.example/a.png", 2, 100.0),),
        (),
        MemberChangeStats(),
    )
    topic = SummaryTopic(
        "热点",
        "归纳",
        (1,),
        1,
        ("成员",),
        (),
        (),
        ("https://avatar.example/a.png",),
    )
    report = MessageSummaryReport("群", "2026-08-30", "2026-08-30", "标题", "导语", analytics, (topic,), (topic,))
    monkeypatch.setattr(summary_renderer, "_avatar_data_uri", lambda url: "data:image/png;base64,embedded")

    prepared = summary_renderer._prepare_report_avatars(report)
    rendered = render_summary_html(prepared)

    assert "https://avatar.example/a.png" not in rendered
    assert "data:image/png;base64,embedded" in rendered
    assert prepared.analytics.contributions[0].avatar_url.startswith("data:image/png;")


def test_summary_capture_only_prepares_visible_avatar_items(monkeypatch) -> None:
    """只缓存页面会展示的头像，避免大量未展示头像撑爆截图 HTML。"""

    contributions = tuple(
        SpeakerContribution(f"member-{index}", f"成员 {index}", f"https://avatar.example/{index}.png", 10, 10.0)
        for index in range(8)
    )
    topics = tuple(
        SummaryTopic(
            f"热点 {index}",
            "归纳",
            (index + 1,),
            1,
            ("成员",),
            (),
            (),
            tuple(f"https://avatar.example/topic-{index}-{member}.png" for member in range(5)),
        )
        for index in range(5)
    )
    analytics = SummaryAnalytics(80, 8, None, None, contributions, (), MemberChangeStats())
    report = MessageSummaryReport("群", "2026-08-30", "2026-08-30", "标题", "导语", analytics, topics, topics)
    calls: list[str] = []

    def fake_avatar_data_uri(url: str) -> str:
        calls.append(url)
        return "data:image/png;base64,embedded"

    monkeypatch.setattr(summary_renderer, "_avatar_data_uri", fake_avatar_data_uri)
    prepared = summary_renderer._prepare_report_avatars(report)

    assert len(calls) == 5 + 3 * 5
    assert prepared.analytics.contributions[4].avatar_url.startswith("data:image/png;")
    assert prepared.analytics.contributions[5].avatar_url == "https://avatar.example/5.png"
    assert prepared.top_topics[2].participant_avatar_urls[0].startswith("data:image/png;")
    assert prepared.top_topics[3].participant_avatar_urls[0] == "https://avatar.example/topic-3-0.png"


def test_cached_report_round_trip_has_no_raw_message_payload() -> None:
    analytics = SummaryAnalytics(2, 1, 3, 33.3, (SpeakerContribution("a", "成员", "", 2, 100.0),), (), MemberChangeStats())
    report = MessageSummaryReport("群", "2026-08-30", "2026-08-30", "标题", "导语", analytics, (), (), 7)
    restored = report_from_dict(report.to_dict())
    assert restored.to_dict() == report.to_dict()
    assert "方案" not in str(restored.to_dict())


def test_structured_report_passes_responses_reasoning_and_stream_options(monkeypatch) -> None:
    """群总结把管理后台的 Responses、推理强度和流式开关传给共享客户端。"""

    captured: dict[str, object] = {}

    class FakeClient:
        def __init__(self, **kwargs):
            captured["client"] = kwargs

        def generate(self, **kwargs):
            captured["generate"] = kwargs
            return PluginTextGenerationResult(
                text='{"headline":"上线准备","intro":"已确认","topics":[{"title":"方案","summary":"归纳","message_indexes":[1],"participant_ids":[],"quotes":[],"market_tags":[]}],"top_topics":[]}',
                token_usage=9,
            )

    monkeypatch.setattr("plugins.message_summary.report.PluginTextGenerationClient", FakeClient)
    batch = _batch()
    analytics = build_summary_analytics(None, batch)
    report = build_structured_report(
        db=None,
        batch=batch,
        analytics=analytics,
        api_base="https://llm.example.test/v1",
        api_key="key",
        model="reasoning-model",
        timeout_seconds=30,
        temperature=0.2,
        max_tokens=4000,
        user_id="tenant-1:binding-2",
        api_protocol="responses",
        reasoning_effort="high",
        stream=True,
    )
    assert report.token_usage == 9
    assert captured["client"]["api_base"] == "https://llm.example.test/v1"
    assert captured["generate"]["api_protocol"] == "responses"
    assert captured["generate"]["reasoning_effort"] == "high"
    assert captured["generate"]["stream"] is True
