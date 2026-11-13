"""Verify the real plugin form searches a large catalog and submits IDs on desktop/mobile."""

from __future__ import annotations

import json
import os
from pathlib import Path

import pytest
from jinja2 import Environment, FileSystemLoader, select_autoescape

from app.services.plugin_schemas import build_plugin_config_view
from test_select_layout import browser  # noqa: F401

ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture
def form_page(browser):
    pages = []

    def create(size=(1280, 900), config=None):
        view = build_plugin_config_view("music", json.dumps(config or {}))
        environment = Environment(loader=FileSystemLoader(ROOT / "app/templates"), autoescape=select_autoescape())
        fragment = environment.get_template("_plugin_config_form.html").render(config_view=view)
        css = (ROOT / "app/static/css/style.css").read_text(encoding="utf-8")
        page = browser.new_page(viewport={"width": size[0], "height": size[1]}, reduced_motion="reduce")
        pages.append(page)
        page.set_default_timeout(8000)
        page.set_content(f'<meta charset="utf-8"><style>{css}</style><main style="max-width:780px;margin:24px auto;padding:16px"><form class="form-grid">{fragment}<button type="reset">重置</button><button type="submit">保存配置</button></form></main>')
        page.add_script_tag(content=(ROOT / "app/static/js/select.js").read_text(encoding="utf-8"))
        page.add_script_tag(content=(ROOT / "app/static/js/config-suggestions.js").read_text(encoding="utf-8"))
        page.locator("form").evaluate("form => form.addEventListener('submit', event => event.preventDefault())")
        return page

    yield create
    for page in pages:
        page.close()


@pytest.mark.parametrize("size", [(1280, 900), (390, 844)])
def test_search_by_name_or_id_and_submit_the_selected_id(form_page, size):
    page = form_page(size)
    combo = page.get_by_role("combobox", name="卡片来源", exact=True)
    assert combo.input_value() == "汽水音乐"
    combo.click()
    panel = page.get_by_role("listbox")
    assert panel.get_by_role("option").count() == 12
    assert "307" in panel.inner_text()
    assert page.locator('input[name="schema__app_id"]').count() == 0
    assert page.locator('[name="schema__app_name"]').count() == 0
    if os.environ.get("QA_SCREENSHOT_DIR"):
        directory = Path(os.environ["QA_SCREENSHOT_DIR"])
        directory.mkdir(parents=True, exist_ok=True)
        page.screenshot(path=str(directory / f"music-card-source-{size[0]}.png"))
    combo.fill("网易")
    option = panel.get_by_role("option").filter(has_text="网易云音乐")
    option.click()
    assert combo.input_value() == "网易云音乐"
    assert not panel.is_visible()
    assert page.locator('[name="schema__app_id"]').input_value() == "wx8dd6ecd81906fd84"
    assert page.locator("form").evaluate("form => new FormData(form).getAll('schema__app_id')") == ["wx8dd6ecd81906fd84"]
    combo.fill("wx79f2c441")
    assert panel.get_by_role("option").count() == 1
    combo.press("Enter")
    assert combo.input_value() == "酷狗音乐"
    assert page.locator('[name="schema__app_id"]').input_value() == "wx79f2c4418704b4f8"
    combo.click()
    assert page.evaluate("document.documentElement.scrollWidth <= innerWidth")
    box = panel.bounding_box()
    assert box["x"] >= 0 and box["x"] + box["width"] <= size[0]
    assert box["height"] <= 280


def test_duplicate_names_reset_and_search_text_never_becomes_form_value(form_page):
    page = form_page(config={"app_id": "wx904fb3ecf62c7dea"})
    combo = page.get_by_role("combobox", name="卡片来源", exact=True)
    assert combo.input_value() == "汽水音乐"
    combo.fill("摇一摇")
    panel = page.get_by_role("listbox")
    # “春晚摇一摇”也会匹配，两个同名来源仍必须按各自 ID 选择。
    assert panel.locator(".ui-select-option > span").all_text_contents().count("摇一摇") == 2
    panel.get_by_role("option").filter(has_text="wx485a97c844086dc9").click()
    page.get_by_role("button", name="保存配置", exact=True).click()
    assert page.locator('[name="schema__app_id"]').input_value() == "wx485a97c844086dc9"
    combo.fill("wx0000000000000002")
    assert panel.get_by_role("option").count() == 0
    combo.press("Tab")
    assert page.locator('[name="schema__app_id"]').input_value() == "wx485a97c844086dc9"
    page.get_by_role("button", name="重置", exact=True).click()
    page.wait_for_function("document.querySelector('[name=schema__app_id]').value === 'wx904fb3ecf62c7dea'")
    assert combo.input_value() == "汽水音乐"


def test_keyboard_navigation_keeps_the_page_still_and_escape_closes(form_page):
    page = form_page()
    combo = page.get_by_role("combobox", name="卡片来源", exact=True)
    combo.click()
    position = page.evaluate("scrollY")
    combo.press("ArrowDown")
    combo.press("Enter")
    assert combo.input_value() == "QQ音乐"
    combo.click()
    for _ in range(11):
        combo.press("ArrowDown")
    assert page.evaluate("scrollY") == position
    combo.press("Escape")
    assert not page.get_by_role("listbox").is_visible()
