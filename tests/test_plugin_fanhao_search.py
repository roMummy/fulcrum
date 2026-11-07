from __future__ import annotations

from app.services.plugin_runtime.contracts import PluginExecutionRequest
from plugins.fanhao_search import plugin as fanhao
from plugins.fanhao_search.service import format_image_result, format_movie_result, load_config, normalize_code, parse_image_matches, parse_magnets, parse_movie_detail


def test_fanhao_config_uses_bounded_site_list() -> None:
    config = load_config({"enabled": True, "javbus_base_url": "javascript:alert(1)", "avscan_result_count": 8})
    assert config.javbus_urls[0] == "https://www.javbus.com"
    assert config.avscan_result_count == 2
    assert config.points_cost == 100


def test_normalize_code_adds_separator_and_rejects_markup() -> None:
    assert normalize_code("ssis406") == "SSIS-406"
    assert normalize_code("<script>") == ""


def test_parse_movie_detail_extracts_stable_fields() -> None:
    markup = """
    <div class="movie"><h3>测试影片</h3><div class="bigImage"><img src="/cover.jpg"></div>
    <div class="info"><p><span class="header">发行日期:</span> 2026-01-02</p>
    <p><span class="header">长度:</span> 120 分钟</p><p><span class="header">演员:</span><a>演员甲</a><a>演员乙</a></p></div></div>
    <script>var gid = 12; var uc = 34;</script>
    """
    detail = parse_movie_detail(markup, "SSIS-406", base_url="https://example.test")
    assert detail is not None
    assert detail.title == "测试影片"
    assert detail.image_url == "https://example.test/cover.jpg"
    assert detail.length_minutes == 120
    assert detail.gid == "12"


def test_parse_magnets_prefers_larger_rows() -> None:
    magnets = parse_magnets('<table><tr><td><a href="magnet:?xt=urn:btih:1">字幕</a></td><td>2GB</td></tr><tr><td><a href="magnet:?xt=urn:btih:2">普通</a></td><td>1GB</td></tr></table>')
    assert magnets[0]["url"].endswith("1")
    assert magnets[0]["subtitle"] is True


def test_parse_image_matches_filters_similarity_and_count() -> None:
    config = load_config({"avscan_result_count": 2, "avscan_min_similarity": 60})
    matches = parse_image_matches({"results": [{"video_code": "ABC-1", "best_similarity": 99, "frames": []}, {"video_code": "LOW-1", "best_similarity": 20, "frames": []}, {"video_code": "ABC-2", "best_similarity": 80, "frames": []}]}, config)
    assert [item.code for item in matches] == ["ABC-1", "ABC-2"]


def test_fanhao_formatters_are_user_facing_only() -> None:
    config = load_config({})
    detail = parse_movie_detail('<div class="movie"><h3>影片</h3></div>', "ABC-1", base_url=config.javbus_urls[0])
    assert detail is not None
    assert "影片" in format_movie_result(detail)
    assert "ABC-1" in format_image_result((__import__("plugins.fanhao_search.service", fromlist=["ImageMatch"]).ImageMatch("ABC-1", 91.2, "封面"),), detail)


def test_fanhao_reload_does_not_call_external_service() -> None:
    result = fanhao.execute_action(PluginExecutionRequest(action="reload", config={"enabled": True}))
    assert result.ok is True
    assert result.output["javbus_site_count"] >= 1

