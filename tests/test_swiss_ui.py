from __future__ import annotations

import re
import xml.etree.ElementTree as ET
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parents[1]
STYLE_PATH = PROJECT_ROOT / "app/static/css/style.css"
FONT_STYLE_PATH = PROJECT_ROOT / "app/static/css/fonts.css"
MARKETING_SCRIPT_PATH = PROJECT_ROOT / "app/static/js/marketing.js"
BRAND_MARK_PATH = PROJECT_ROOT / "app/static/brand-mark.svg"
FAVICON_PATH = PROJECT_ROOT / "app/static/favicon.svg"
TEMPLATE_ROOT = PROJECT_ROOT / "app/templates"


def _read_project_file(path: Path) -> str:
    """统一读取前端源文件，失败时让断言直接暴露缺失文件或编码问题。"""

    return path.read_text(encoding="utf-8")


def _read_css_rule(stylesheet: str, selector: str) -> str:
    """读取单个基础 CSS 规则，便于直接约束全局视觉基线而不匹配后续响应式覆盖。"""

    return stylesheet.split(f"{selector} {{", 1)[1].split("}", 1)[0]


def test_swiss_tokens_and_forbidden_visual_patterns() -> None:
    """全站必须使用已确认的 Swiss Style 色板和直角网格语言。

    这里约束产品级视觉基础，不枚举每个业务组件的像素值。黑白灰承担信息结构，
    红、蓝、黄只承担信号与状态；渐变、玻璃模糊和全圆角会破坏理性网格，因此不能
    通过局部页面重新引入。
    """

    stylesheet = _read_project_file(STYLE_PATH)
    font_stylesheet = _read_project_file(FONT_STYLE_PATH)

    for required_token in (
        "--bg: #fafafa;",
        "--surface: #ffffff;",
        "--surface-muted: #f0f0f0;",
        "--text: #0a0a0a;",
        "--signal: #ff0000;",
        "--info: #0057b8;",
        "--warning: #ffcc00;",
        "--radius-lg: 0;",
        "--space-section: 48px;",
    ):
        assert required_token in stylesheet

    # 字体资源必须随应用发布，不能继续依赖运行设备是否预装 Avenir、苹方或思源黑体。
    assert 'font-family: "Albert Sans";' in font_stylesheet
    assert 'font-family: "Noto Sans SC";' in font_stylesheet
    assert "font-size: 15px;" in stylesheet
    assert "font-weight: 380;" in stylesheet

    font_root = PROJECT_ROOT / "app/static/fonts"
    assert len(list((font_root / "albert-sans").glob("*.woff2"))) == 2
    assert len(list((font_root / "noto-sans-sc").glob("*.woff2"))) >= 100
    assert (font_root / "FONT-LICENSES.md").exists()

    for forbidden_pattern in (
        "linear-gradient(",
        "radial-gradient(",
        "backdrop-filter:",
        "border-radius: 999px",
    ):
        assert forbidden_pattern not in stylesheet


def test_backend_shell_uses_swiss_top_navigation_and_keeps_platform_identity_separate() -> None:
    """双后台必须共享 Swiss 顶部导航，同时保留平台与企业身份边界。

    桌面侧栏会长期占用业务工作区，且和移动顶部栏形成两套结构。本轮统一为顶部
    导航后，只允许保留一套导航数据和一个响应式壳层，避免后续入口在两处产生差异。
    """

    base_template = _read_project_file(TEMPLATE_ROOT / "base.html")
    platform_template = _read_project_file(TEMPLATE_ROOT / "platform_base.html")
    stylesheet = _read_project_file(STYLE_PATH)

    for required_class in (
        'class="app-topbar"',
        'class="app-primary-nav"',
        'class="app-main"',
        'class="page-header"',
    ):
        assert required_class in base_template

    assert 'class="nav-more"' in base_template
    assert 'class="account-menu nav-more"' in base_template
    assert 'class="app-sidebar"' not in base_template
    assert "企业工作台" in base_template
    assert "平台管理后台" in platform_template
    assert "request.url.path.startswith('/platform')" in base_template
    assert '<meta name="theme-color" content="#fafafa" />' in base_template

    # 顶栏、工作区和页头必须通过黑白网格与信号红建立层级，不能退回低对比暖灰表面。
    topbar_rule = _read_css_rule(stylesheet, ".app-topbar")
    topbar_inner_rule = _read_css_rule(stylesheet, ".app-topbar-inner")
    workspace_rule = _read_css_rule(stylesheet, ".workspace")
    page_header_rule = _read_css_rule(stylesheet, ".page-header")
    active_nav_rule = _read_css_rule(stylesheet, ".nav-link.active")

    assert "background: var(--surface);" in topbar_rule
    assert "border-bottom: 1px solid var(--border-strong);" in topbar_rule
    assert "min-height: 68px;" in topbar_inner_rule
    assert "padding: 44px 0 80px;" in workspace_rule
    assert "margin-bottom: 36px;" in page_header_rule
    assert "border-bottom: 1px solid var(--border);" in page_header_rule
    assert "color: var(--text);" in active_nav_rule
    assert ".nav-link.active::after" in stylesheet


def test_marketing_and_auth_pages_use_swiss_grid_and_preserve_accessibility_contracts() -> None:
    """营销与认证页面必须使用 Swiss 网格，并保留真实产品与表单语义。"""

    marketing_home = _read_project_file(TEMPLATE_ROOT / "marketing_home.html")
    marketing_base = _read_project_file(TEMPLATE_ROOT / "marketing_base.html")
    enterprise_login = _read_project_file(TEMPLATE_ROOT / "enterprise_login.html")
    register = _read_project_file(TEMPLATE_ROOT / "register.html")

    assert 'class="marketing-hero marketing-hero-stage"' in marketing_home
    assert 'class="marketing-hero-scene"' in marketing_home
    assert "data-hero-scene" in marketing_home
    assert 'aria-hidden="true"' in marketing_home
    assert 'class="marketing-hero-signal"' in marketing_home
    assert 'class="marketing-hero-copy"' in marketing_home
    assert "/static/js/marketing.js" in marketing_base
    assert "yaoshu-console.webp" not in marketing_home
    assert "marketing-product-visual" not in marketing_home
    assert marketing_home.count("<h1>") == 1
    assert "<h1>曜枢 AI</h1>" in marketing_home
    assert "marketing-hero-content" not in marketing_home
    assert 'role="alert"' in marketing_base
    assert '<meta name="theme-color" content="#fafafa" />' in marketing_base
    assert 'class="marketing-auth-context marketing-auth-context-signal"' in enterprise_login
    assert 'class="marketing-auth-context marketing-auth-context-signal"' in register
    assert 'autocomplete="username"' in enterprise_login
    assert 'autocomplete="current-password"' in enterprise_login
    for autocomplete in ("organization", "name", "tel", "new-password"):
        assert f'autocomplete="{autocomplete}"' in register


def test_pricing_grid_keeps_shared_container_centering() -> None:
    """套餐网格不得覆盖共享内容容器的自动居中外边距。

    价格页让同一个元素同时承担内容容器与三列网格。如果公共网格规则重置全部
    外边距，后声明的 `margin: 0` 会让 1200px 容器从视口左侧开始排列，因此这里只
    允许有序步骤列表单独清理浏览器默认外边距。
    """

    stylesheet = _read_project_file(STYLE_PATH)
    pricing_template = _read_project_file(TEMPLATE_ROOT / "pricing.html")
    grid_rule = re.search(
        r"\.marketing-feature-grid,\s*\.marketing-steps,\s*\.marketing-plan-grid\s*\{(?P<body>[^}]*)\}",
        stylesheet,
    )

    assert grid_rule is not None
    assert "margin: 0;" not in grid_rule.group("body")
    assert ".marketing-steps {\n  margin: 0;" in stylesheet
    assert 'class="marketing-section-inner marketing-plan-grid"' in pricing_template


def test_marketing_hero_interaction_respects_motion_and_keyboard_boundaries() -> None:
    """无截图 Hero 必须提供受限指针反馈，并为键盘与减少动态效果保留静态路径。"""

    stylesheet = _read_project_file(STYLE_PATH)
    script = _read_project_file(MARKETING_SCRIPT_PATH)

    for required_style in (
        ".marketing-hero-scene",
        ".marketing-hero-marker",
        ".marketing-hero:focus-within .marketing-hero-signal",
        "--hero-pointer-x",
        "@media (prefers-reduced-motion: reduce)",
    ):
        assert required_style in stylesheet

    for required_script in (
        'document.querySelector("[data-hero-scene]")',
        'window.matchMedia("(prefers-reduced-motion: reduce)")',
        'hero.addEventListener("pointermove"',
        'hero.addEventListener("pointerleave"',
        'scene.style.setProperty("--hero-pointer-x"',
        'scene.style.setProperty("--hero-pointer-y"',
    ):
        assert required_script in script


def test_swiss_brand_mark_and_rounded_favicon_share_one_geometry() -> None:
    """站内标志与圆角 favicon 必须共享内部几何，并彻底删除旧渐变机器人。

    SVG 使用 XML 解析而不是字符串猜测结构。站内版本保持直角红底，浏览器图标只
    改变外层圆角；白色 Y 路径和蓝色枢点必须完全相同，避免形成两套品牌标志。
    """

    assert BRAND_MARK_PATH.exists()
    brand_root = ET.parse(BRAND_MARK_PATH).getroot()
    favicon_root = ET.parse(FAVICON_PATH).getroot()
    namespace = {"svg": "http://www.w3.org/2000/svg"}

    assert not brand_root.findall(".//svg:linearGradient", namespace)
    assert not favicon_root.findall(".//svg:linearGradient", namespace)

    brand_background = brand_root.find("svg:rect", namespace)
    favicon_background = favicon_root.find("svg:rect", namespace)
    assert brand_background is not None
    assert favicon_background is not None
    assert brand_background.attrib.get("rx") in (None, "0")
    assert favicon_background.attrib.get("rx") == "14"
    assert brand_background.attrib.get("fill") == favicon_background.attrib.get("fill") == "#ff0000"

    brand_path = brand_root.find("svg:path", namespace)
    favicon_path = favicon_root.find("svg:path", namespace)
    assert brand_path is not None
    assert favicon_path is not None
    assert brand_path.attrib == favicon_path.attrib

    brand_pivot = brand_root.findall("svg:rect", namespace)[1]
    favicon_pivot = favicon_root.findall("svg:rect", namespace)[1]
    assert brand_pivot.attrib == favicon_pivot.attrib
    assert brand_pivot.attrib.get("fill") == "#0057b8"

    base_template = _read_project_file(TEMPLATE_ROOT / "base.html")
    marketing_base = _read_project_file(TEMPLATE_ROOT / "marketing_base.html")
    assert base_template.count('/static/brand-mark.svg') == 2
    assert marketing_base.count('/static/brand-mark.svg') == 1
    assert '<span class="brand-mark">曜</span>' not in base_template
    assert '<span class="marketing-brand-mark" aria-hidden="true">曜</span>' not in marketing_base


def test_dashboards_use_swiss_data_bands_and_single_layer_surfaces() -> None:
    """两个总览必须使用连续数据带和单层 Swiss 业务表面。"""

    tenant_dashboard = _read_project_file(TEMPLATE_ROOT / "dashboard.html")
    platform_dashboard = _read_project_file(TEMPLATE_ROOT / "platform_dashboard.html")

    assert 'class="metric-band' in tenant_dashboard
    assert 'class="metric-band' in platform_dashboard
    assert tenant_dashboard.count('class="page-section') >= 10
    assert platform_dashboard.count('class="page-section') >= 4
    assert platform_dashboard.count('class="platform-analytics-surface') == 3
    for required_class in (
        "analytics-range-control",
        "platform-analytics-kpis",
        "platform-analytics-chart-grid",
        "platform-adoption-funnel",
        "platform-channel-summary",
        "platform-tenant-rankings",
        "platform-tenant-health",
        "platform-subscription-summary",
        "platform-presence-summary",
        "platform-analytics-trends-surface",
        "platform-analytics-operations-surface",
        "platform-analytics-health-surface",
    ):
        assert required_class in platform_dashboard
    assert 'class="work-surface"' not in tenant_dashboard
    assert 'class="work-surface"' not in platform_dashboard
    for forbidden_class in ("dashboard-hero-card", "nested-card", "accent-card"):
        assert forbidden_class not in tenant_dashboard
        assert forbidden_class not in platform_dashboard


def test_overview_grids_align_swiss_surfaces_without_fixed_heights() -> None:
    """总览业务表面必须通过网格拉伸对齐，不能依赖固定高度。

    企业总览包含五组双列业务卡片，必须统一声明总览专用网格语义，避免通用的
    相邻卡片外边距让右侧卡片下移。平台总览沿用连续指标带，但需要独立规则稳定
    每个指标项的说明位置，使两类总览保持一致的纵向节奏。
    """

    tenant_dashboard = _read_project_file(TEMPLATE_ROOT / "dashboard.html")
    platform_dashboard = _read_project_file(TEMPLATE_ROOT / "platform_dashboard.html")
    stylesheet = _read_project_file(STYLE_PATH)

    assert tenant_dashboard.count("overview-card-grid") == 5
    assert "platform-metric-band" in platform_dashboard
    assert ".overview-card-grid > .page-section" in stylesheet
    assert ".platform-metric-band .metric-item" in stylesheet


def test_platform_analytics_uses_stable_charts_and_mobile_single_column_layout() -> None:
    """平台大盘必须有稳定图表、明确 Swiss 分区和移动单列布局。"""

    platform_dashboard = _read_project_file(TEMPLATE_ROOT / "platform_dashboard.html")
    stylesheet = _read_project_file(STYLE_PATH)

    assert platform_dashboard.count('class="analytics-chart"') == 2
    assert 'viewBox="0 0 100 100"' in platform_dashboard
    assert 'class="table-scroll"' in platform_dashboard
    assert ".platform-analytics-chart-grid" in stylesheet
    assert ".platform-analytics-surface" in stylesheet
    assert ".platform-analytics-surface > .platform-analytics-grid" in stylesheet
    assert "aspect-ratio: 16 / 7;" in stylesheet
    assert "@media (max-width: 820px)" in stylesheet
    assert ".platform-analytics-grid" in stylesheet


def test_shared_styles_define_mobile_navigation_and_swiss_work_surfaces() -> None:
    """共享样式必须显式覆盖移动导航与直角单层业务表面。"""

    stylesheet = _read_project_file(STYLE_PATH)

    assert ".work-surface" in stylesheet
    assert ".page-section" in stylesheet
    assert ".editor-surface" in stylesheet
    assert ".metric-band" in stylesheet
    page_section_rule = _read_css_rule(stylesheet, ".page-section")
    assert "background: var(--surface);" in page_section_rule
    assert "border-top: 4px solid var(--border-strong);" in page_section_rule
    assert "border-radius: var(--radius-lg);" in page_section_rule
    assert "@media (max-width: 820px)" in stylesheet
    assert ".app-nav-toggle" in stylesheet


def test_section_navigation_wraps_without_horizontal_scrollbar() -> None:
    """二级导航必须自然换行，不能再通过横向滚动隐藏入口。

    只检查 `.section-tabs` 的基础规则，因为移动端应继承相同的换行语义；若在媒体
    查询中重新加入横向滚动，后面的全局断言仍会捕获该回归。
    """

    stylesheet = _read_project_file(STYLE_PATH)
    section_tabs_rule = _read_css_rule(stylesheet, ".section-tabs")

    assert "flex-wrap: wrap;" in section_tabs_rule
    assert "overflow-x" not in section_tabs_rule
    assert "border-bottom" not in section_tabs_rule
    assert ".section-tabs {\n    overflow-x: auto;" not in stylesheet


def test_account_and_plan_management_use_compact_lists_and_edit_dialogs() -> None:
    """账号和套餐管理不能把大编辑表单塞入表格行。

    三个页面共享记录列表语义，但继续使用各自原有的服务端提交地址。这里约束结构
    而不复制业务字段断言，使后续增加字段时只需维护对应弹窗，不会重新引入行内表单。
    """

    tenant_users = _read_project_file(TEMPLATE_ROOT / "tenant_users.html")
    platform_users = _read_project_file(TEMPLATE_ROOT / "platform_users.html")
    platform_plans = _read_project_file(TEMPLATE_ROOT / "platform_plans.html")

    for template_source in (tenant_users, platform_users, platform_plans):
        assert 'class="editor-surface"' in template_source
        assert 'class="page-section"' in template_source
        assert 'class="record-list"' in template_source
        assert 'class="record-row"' in template_source
        assert 'class="record-actions"' in template_source

    assert 'data-modal-open="edit-tenant-user-' in tenant_users
    assert 'data-modal-open="edit-platform-user-' in platform_users
    assert 'data-modal-open="edit-plan-' in platform_plans
    assert '<td colspan="5">' not in tenant_users
    assert '<td colspan="4">' not in platform_users
    assert '<td colspan="9">' not in platform_plans

    account_onboarding = _read_project_file(TEMPLATE_ROOT / "account_onboarding.html")
    assert account_onboarding.count('class="page-section"') == 2


def test_visible_copy_omits_development_process_and_keeps_required_warnings() -> None:
    """后台可见文案只表达业务任务，不展示重构、迁移和接口调试过程。

    安全、身份和不可逆操作提示不能随文案精简一起删除，因此同时检查一次性密码、
    平台入口边界和服务重启中断提示仍然存在。
    """

    template_source = "\n".join(
        _read_project_file(path)
        for path in sorted(TEMPLATE_ROOT.glob("*.html"))
    )

    for development_copy in (
        "新增入口改成弹窗后",
        "新增动作收进弹窗后",
        "改成卡片视图后",
        "当前页面只保留目录扫描结果",
        "数据库连接和结构迁移由部署环境负责",
        "旧节点兼容额度",
        "已迁移功能型",
        ">迁移状态<",
        "统一出站接口：",
        "这里可以直接调用当前系统接入的 Wechat iPad 接口",
        "`wechat_ipad` 需要先部署独立 provider",
        "把 bootstrap 管理员和演示账号继续收敛",
    ):
        assert development_copy not in template_source

    assert "一次性初始密码" in _read_project_file(TEMPLATE_ROOT / "tenant_users.html")
    assert "此入口仅供平台管理员使用" in _read_project_file(TEMPLATE_ROOT / "platform_login.html")
    assert "服务会短暂中断" in _read_project_file(TEMPLATE_ROOT / "platform_monitoring.html")


def test_mobile_data_table_and_platform_login_keep_page_boundaries() -> None:
    """移动表格只能在自身容器滚动，平台登录页必须保留唯一页面级标题。

    机器人表格有稳定的桌面最小宽度，如果遗漏滚动容器会直接把 390 像素移动视口
    撑到 720 像素以上。平台登录又使用独立认证壳层，因此需要显式主标题承接页面语义。
    """

    bots_template = _read_project_file(TEMPLATE_ROOT / "bots.html")
    platform_login = _read_project_file(TEMPLATE_ROOT / "platform_login.html")
    stylesheet = _read_project_file(STYLE_PATH)

    assert '<div class="table-scroll">' in bots_template
    assert '<tr><td colspan="7"><div class="empty-state">' not in bots_template
    assert '<h1 id="platform-login-title">平台管理后台</h1>' in platform_login
    assert ".page-section:has(> .table)" in stylesheet
    assert ".marketing-nav::-webkit-scrollbar" in stylesheet
    assert "width: min(100% - 40px, 520px);" in stylesheet
    assert "body {\n    font-size: 15px;" in stylesheet
    assert ".dashboard-kpi-grid .metric-item:nth-child(5n)" not in stylesheet


def test_all_templates_have_migrated_away_from_legacy_card_tokens() -> None:
    """全站模板不得继续引用旧卡片语义或通过内联样式恢复大圆角。

    新样式不提供旧类名兼容层，因此这里同时约束模板和 CSS。这样后续新增页面必须
    明确选择工作表面、指标单元或内部小节，而不能继续使用含义模糊的通用卡片。
    """

    template_source = "\n".join(
        _read_project_file(path)
        for path in sorted(TEMPLATE_ROOT.glob("*.html"))
    )
    stylesheet = _read_project_file(STYLE_PATH)
    # 只解析真实 class 属性，避免把 Jinja 循环变量或普通业务文字误判成旧视觉类名。
    template_classes = {
        token
        for class_value in re.findall(r'class="([^"]*)"', template_source)
        for token in class_value.split()
    }

    for legacy_token in ("card", "stat-card", "nested-card", "subcard", "inset-panel"):
        assert legacy_token not in template_classes

    for legacy_selector in (".card", ".stat-card", ".subcard", ".inset-panel"):
        assert legacy_selector not in stylesheet

    assert "border-radius:999px" not in template_source
    assert "border-radius: 999px" not in template_source
    assert "{% for work-surface" not in template_source


def test_query_pages_share_compact_filter_toolbar_and_mobile_collapse() -> None:
    """后台查询页必须共享紧凑筛选结构，并在移动端把结果内容放在首屏前部。

    这些页面的筛选字段数量不同，不能继续套用面向数据录入的双列 `form-grid`。
    测试只约束共享结构和关键尺寸，不绑定具体业务文案，确保后续增加筛选条件时仍沿用
    同一条紧凑路径，而不会重新创建独立大筛选卡片或页面专用兼容样式。
    """

    stylesheet = _read_project_file(STYLE_PATH)
    query_templates = (
        "messages.html",
        "conversations.html",
        "plugins.html",
        "platform_logs.html",
        "workwechat_contact_list.html",
    )

    for template_name in query_templates:
        template_source = _read_project_file(TEMPLATE_ROOT / template_name)
        assert 'class="compact-filter' in template_source
        assert 'open data-filter-active="' in template_source
        assert 'class="compact-filter-summary"' in template_source
        assert 'class="compact-filter-form"' in template_source
        assert re.search(r'<form[^>]+method="get"[^>]+class="compact-filter-form"', template_source)
        assert not re.search(r'<form[^>]+method="get"[^>]+class="form-grid"', template_source)

    for template_name in ("messages.html", "conversations.html", "platform_logs.html"):
        assert "filter-card" not in _read_project_file(TEMPLATE_ROOT / template_name)

    compact_form_rule = _read_css_rule(stylesheet, ".compact-filter-form")
    compact_control_rule = _read_css_rule(
        stylesheet,
        ".compact-filter-field input,\n.compact-filter-field select",
    )
    assert "display: flex;" in compact_form_rule
    assert "flex-wrap: wrap;" in compact_form_rule
    assert "min-height: 36px;" in compact_control_rule
    assert ".compact-filter-field-query" in stylesheet
    assert "@media (max-width: 820px)" in stylesheet
    assert ".compact-filter-summary" in stylesheet

    base_template = _read_project_file(TEMPLATE_ROOT / "base.html")
    assert 'document.querySelectorAll("details.compact-filter")' in base_template
    assert 'window.matchMedia("(max-width: 820px)")' in base_template
    assert 'filter.dataset.filterActive === "true"' in base_template
