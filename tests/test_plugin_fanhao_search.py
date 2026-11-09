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



def test_fanhao_matches_without_enabled_key_in_config() -> None:
    """回归:绑定配置里没有(或残留 enabled=false)时,命令仍应匹配,开关只看绑定状态。"""

    class Ctx:
        content = "番号 ABC-123"
        binding_config: dict = {}

    assert fanhao.matches_event(Ctx())["matched"] is True
    Ctx.binding_config = {"enabled": False}
    assert fanhao.matches_event(Ctx())["matched"] is True


def test_fanhao_requests_carry_age_cookie_and_browser_ua() -> None:
    """回归:JavBus 请求必须带 dv=1 和浏览器 UA,否则镜像站会重定向到年龄验证页。"""

    from plugins.fanhao_search.service import _javbus_headers

    headers = _javbus_headers("https://example.test/ABC-123")
    assert headers["Cookie"] == "dv=1"
    assert "Chrome/" in headers["User-Agent"]
    assert headers["Referer"] == "https://example.test/ABC-123"


def test_fanhao_defaults_include_fallback_site_and_parse_real_markup() -> None:
    """回归:不配置也有备用站;真实页面的封面在 <a class=bigImage>,演员在 star-name 中。"""

    assert load_config({}).javbus_urls == ("https://www.javbus.com", "https://www.cdnbus.casa")
    markup = (
        '<div class="row movie"><a class="bigImage" href="/p.jpg"><img src="/pics/c.jpg"></a>'
        '<div class="info"><p><span class="header">發行日期:</span> 2022-05-20</p></div>'
        '<div class="star-name"><a href="/star/1" title="x">葵つかさ</a></div></div><h3>SSIS-406 标题</h3>'
    )
    detail = parse_movie_detail(markup, "SSIS-406", base_url="https://www.cdnbus.casa")
    assert detail is not None
    assert detail.image_url == "https://www.cdnbus.casa/pics/c.jpg"
    assert detail.stars == ("葵つかさ",)
