"""真实浏览器检查企业／平台卡片趋势，数据与认证均来自临时测试库。"""

from datetime import timedelta
from pathlib import Path

import pytest
from sqlalchemy import select

from app.core.time import now_shanghai
from app.models.entities import Plugin, PluginActivityHourly, PluginExecutionLog
from test_plugin_usage import usage_context, usage_platform_client  # noqa: F401
from test_provider_node_pages import _login, provider_page_context  # noqa: F401
from test_group_plugin_settings_browser import _application_server
from test_select_layout import browser  # noqa: F401

expect = pytest.importorskip('playwright.sync_api').expect


@pytest.fixture()
def usage_preview_data(usage_context):
    """为大部分插件准备非零趋势，保留一个无调用插件验证真实空状态。"""

    client, ids = usage_context
    now = now_shanghai()
    hour = now.replace(minute=0, second=0, microsecond=0)
    with ids['factory']() as db:
        plugins = db.scalars(select(Plugin).order_by(Plugin.display_name)).all()
        for index, plugin in enumerate(plugins[:-1]):
            db.add(PluginActivityHourly(tenant_id=ids['alpha'], plugin_id=plugin.id, plugin_code=plugin.code,
                bucket_start=hour - timedelta(hours=2), matched_count=8 + index, success_count=7 + index, failed_count=1))
            db.add(PluginActivityHourly(tenant_id=ids['alpha'], plugin_id=plugin.id, plugin_code=plugin.code,
                bucket_start=hour, matched_count=3, success_count=3))
            db.add(PluginExecutionLog(tenant_id=ids['alpha'], plugin_id=plugin.id, plugin_code=plugin.code,
                matched=True, attempted=True, ok=True, status='completed', created_at=now - timedelta(minutes=index)))
        db.commit()
        ids['activity_order'] = [plugin.code for plugin in plugins]
    return client, ids


@pytest.mark.parametrize('width', [1440, 390])
@pytest.mark.parametrize('scope', ['tenant', 'platform'])
def test_plugin_usage_charts_ranges_pointer_keyboard_and_retry(usage_preview_data, usage_platform_client, browser, width, scope):
    """覆盖批量请求、交互读数、范围切换竞争和错误重试，确保手机没有横向溢出。"""

    client, ids = usage_preview_data
    if scope == 'platform':
        client = usage_platform_client
    else:
        _login(client, 'readonly')
    url = '/platform/plugins' if scope == 'platform' else '/plugins'
    context = browser.new_context(viewport={'width': width, 'height': 1000 if width > 500 else 844},
        reduced_motion='reduce', has_touch=width == 390)
    page = context.new_page()
    errors, calls = [], []
    page.on('pageerror', lambda error: errors.append(str(error)))
    page.on('request', lambda request: calls.append(request.url) if '/plugins/usage?' in request.url else None)
    try:
        with _application_server(client) as base:
            page.goto(base + url)
            card = page.locator('[data-usage-plugin]').first
            expect(card).to_have_attribute('aria-busy', 'false')
            expect(card.locator('[data-usage-value="matched"]')).not_to_have_text('—')
            # 两类目录都按真实活动排序，移动既有卡片时保留图表和设置交互。
            order = lambda: page.locator('[data-usage-plugin]').evaluate_all('nodes => nodes.map(node => node.dataset.usagePlugin)')
            assert order() == ids['activity_order']
            assert len(calls) == 1, '首屏所有卡片应共用一次批量请求'
            assert page.locator('[data-usage-plugin]').count() > 3
            expect(page.locator('[data-usage-readout]').get_by_text('本期暂无调用', exact=True).first).to_be_visible()
            svg = card.locator('svg')
            svg.scroll_into_view_if_needed()
            svg.focus()
            svg.press('Home')
            expect(svg).to_have_attribute('aria-valuenow', '1')
            svg.press('End')
            expect(svg).to_have_attribute('aria-valuenow', '24')
            if width == 390:
                svg.tap(position={'x': 80, 'y': 30})
            else:
                svg.hover(position={'x': 80, 'y': 30})
            assert 1 < int(svg.get_attribute('aria-valuenow')) < 24
            expect(card.locator('[data-usage-readout]')).to_contain_text('命中')
            page.locator('[data-usage-period="7d"]').click()
            expect(svg).to_have_attribute('aria-valuemax', '7')
            assert order() == ids['activity_order'], '切换图表范围不应改变最近活动顺序'
            # 同一浏览器事件循环中连续切换，确保第一个请求尚未返回就被新选择取代。
            page.evaluate("""() => {
                document.querySelector('[data-usage-period="30d"]').click();
                document.querySelector('[data-usage-period="24h"]').click();
            }""")
            expect(page.locator('[data-usage-period="24h"]')).to_have_attribute('aria-pressed', 'true')
            expect(svg).to_have_attribute('aria-valuemax', '24')
            expect(card).to_have_attribute('aria-busy', 'false')
            assert page.evaluate('document.documentElement.scrollWidth <= innerWidth')
            directory = Path('.run/plugin-usage')
            directory.mkdir(parents=True, exist_ok=True)
            page.evaluate('window.scrollTo(0, 0)')
            page.screenshot(path=str(directory / f'{scope}-{width}.png'), full_page=False)
            # 失败必须显示不可用和破折号；恢复后按原范围刷新，不能伪装成零调用。
            pattern = '**/plugins/usage?*'
            page.route(pattern, lambda route: route.fulfill(status=503, content_type='application/json', body='{}'))
            page.locator('[data-usage-refresh]').click()
            expect(page.locator('[data-usage-updated]')).to_contain_text('加载失败')
            expect(card.locator('[data-usage-value="matched"]')).to_have_text('—')
            expect(card.locator('[data-usage-readout]')).to_have_text('统计暂不可用')
            page.unroute(pattern)
            page.locator('[data-usage-refresh]').click()
            expect(card.locator('[data-usage-value="matched"]')).not_to_have_text('—')
            expect(page.locator('[data-usage-updated]')).to_contain_text('更新于')
            assert order() == ids['activity_order']
            assert page.evaluate('document.documentElement.scrollWidth <= innerWidth')
            assert not errors
    finally:
        context.close()
