from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
from datetime import datetime
from zoneinfo import ZoneInfo

from plugins.sehuatang_push import service as sehuatang_service
from plugins.sehuatang_push.pdf_renderer import build_pdf_html
from plugins.sehuatang_push.service import load_config, parse_movie_list
from app.services.plugin_runtime.scheduler import _build_plugin_schedule_specs


def test_sehuatang_config_requires_explicit_target_conversations() -> None:
    config = load_config({"enabled": True, "target_conversation_ids": [1, "2", 0, "bad"]})
    assert config.target_conversation_ids == (1, 2)
    assert config.max_movies == 120


def test_parse_movie_list_extracts_only_complete_cards() -> None:
    markup = '<a class="movie-box" href="/ABC-1"><img title="标题"/><date>ABC-1</date><date>2026-11-07</date></a><a class="movie-box"><date>bad</date></a>'
    movies = parse_movie_list(markup, "https://example.test")
    assert len(movies) == 1
    assert movies[0].code == "ABC-1"
    assert movies[0].detail_url == "https://example.test/ABC-1"


def test_pdf_html_does_not_embed_raw_markup() -> None:
    html = build_pdf_html("目录", [{"code": "ABC-1", "title": "<script>", "release_date": "2026-11-07", "detail_url": "https://example.test/ABC-1"}])
    assert "&lt;script&gt;" in html
    assert "<script>" not in html


def test_pdf_renderer_missing_browser_is_stable(monkeypatch, tmp_path: Path) -> None:
    # 先提供一条结构完整的假列表响应，确保用例确实覆盖到 PDF 渲染器，
    # 而不是因为测试客户端缺少 request 方法提前失败。
    class FakeClient:
        def request(self, *args, **kwargs):
            return SimpleNamespace(
                status_code=200,
                text='<a class="movie-box" href="/ABC-1"><img title="标题"/><date>ABC-1</date><date>2026-11-07</date></a>',
            )

    monkeypatch.setattr(sehuatang_service, "render_html_to_pdf", lambda *args, **kwargs: (_ for _ in ()).throw(RuntimeError("browser missing")))
    try:
        sehuatang_service.build_pdf(FakeClient(), 1, load_config({"enabled": True, "source_urls": ["https://example.test"]}))
    except Exception as exc:
        assert "browser missing" in str(exc)
    else:  # pragma: no cover
        raise AssertionError("缺少浏览器时未失败")


def test_sehuatang_schedule_requires_explicit_targets_and_emits_daily_actions() -> None:
    """调度只为租户显式选择的目标会话创建每日 PDF 任务。"""

    current = datetime(2026, 11, 7, 16, 0, tzinfo=ZoneInfo("Asia/Shanghai"))
    assert _build_plugin_schedule_specs("sehuatang_push", {"enabled": True, "push_time": "17:30", "target_conversation_ids": [11]}, current) == ()
    specs = _build_plugin_schedule_specs(
        "sehuatang_push",
        {"enabled": True, "push_time": "15:30", "target_conversation_ids": [11, "12", 0, "bad", 11]},
        current,
    )
    assert [item.action_key for item in specs] == ["daily_pdf_push", "daily_pdf_push"]
    assert [item.conversation_id for item in specs] == [11, 12]
