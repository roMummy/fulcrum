from __future__ import annotations

import re
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parents[1]
STYLE_PATH = PROJECT_ROOT / "app/static/css/style.css"
FONT_STYLE_PATH = PROJECT_ROOT / "app/static/css/fonts.css"
TEMPLATE_ROOT = PROJECT_ROOT / "app/templates"


def _read_project_file(path: Path) -> str:
    """统一读取前端源文件，失败时让断言直接暴露缺失文件或编码问题。"""

    return path.read_text(encoding="utf-8")


def test_scandinavian_tokens_and_forbidden_visual_patterns() -> None:
    """全站样式必须使用已确认的北欧极简 token，并彻底移除旧装饰语言。

    这里检查的是产品级硬约束，不尝试枚举每个组件的具体像素值。渐变、玻璃模糊
    和全圆角是旧版界面产生强装饰感与卡片感的主要来源，因此不允许通过局部页面
    重新引入；必要状态反馈应改用颜色、边框和亮度变化表达。
    """

    stylesheet = _read_project_file(STYLE_PATH)
    font_stylesheet = _read_project_file(FONT_STYLE_PATH)

    for required_token in (
        "--bg: #f5f0eb;",
        "--text: #3d3d3d;",
        "--primary: #5a7a6b;",
        "--wood: #a89279;",
        "--radius-lg: 6px;",
        "--surface: rgba(255, 255, 255, 0.46);",
        "--border: rgba(168, 146, 121, 0.22);",
        "--space-section: 56px;",
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


def test_backend_shell_uses_top_navigation_and_keeps_platform_identity_separate() -> None:
    """双后台必须共享顶部导航，同时保留平台与企业身份的清晰边界。

    桌面侧栏会长期占用业务工作区，且和移动顶部栏形成两套结构。本轮统一为顶部
    导航后，只允许保留一套导航数据和一个响应式壳层，避免后续入口在两处产生差异。
    """

    base_template = _read_project_file(TEMPLATE_ROOT / "base.html")
    platform_template = _read_project_file(TEMPLATE_ROOT / "platform_base.html")

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


def test_marketing_and_auth_pages_preserve_product_and_accessibility_contracts() -> None:
    """营销与认证页面换结构后仍需保留真实产品素材和表单可访问性语义。"""

    marketing_home = _read_project_file(TEMPLATE_ROOT / "marketing_home.html")
    marketing_base = _read_project_file(TEMPLATE_ROOT / "marketing_base.html")
    enterprise_login = _read_project_file(TEMPLATE_ROOT / "enterprise_login.html")
    register = _read_project_file(TEMPLATE_ROOT / "register.html")

    assert "/static/images/yaoshu-console.webp" in marketing_home
    assert 'class="marketing-product-visual"' in marketing_home
    assert 'role="alert"' in marketing_base
    assert 'autocomplete="username"' in enterprise_login
    assert 'autocomplete="current-password"' in enterprise_login
    for autocomplete in ("organization", "name", "tel", "new-password"):
        assert f'autocomplete="{autocomplete}"' in register


def test_dashboards_use_data_bands_without_nested_cards() -> None:
    """两个总览必须依靠连续数据带和单层工作表面组织信息。"""

    tenant_dashboard = _read_project_file(TEMPLATE_ROOT / "dashboard.html")
    platform_dashboard = _read_project_file(TEMPLATE_ROOT / "platform_dashboard.html")

    assert 'class="metric-band' in tenant_dashboard
    assert 'class="metric-band' in platform_dashboard
    for forbidden_class in ("dashboard-hero-card", "nested-card", "accent-card"):
        assert forbidden_class not in tenant_dashboard
        assert forbidden_class not in platform_dashboard


def test_overview_grids_align_cards_without_fixed_heights() -> None:
    """总览卡片必须通过网格拉伸对齐，不能依赖容易溢出的固定高度。

    企业总览包含五组双列业务卡片，必须统一声明总览专用网格语义，避免通用的
    相邻卡片外边距让右侧卡片下移。平台总览沿用连续指标带，但需要独立规则稳定
    每个指标项的说明位置，使两类总览保持一致的纵向节奏。
    """

    tenant_dashboard = _read_project_file(TEMPLATE_ROOT / "dashboard.html")
    platform_dashboard = _read_project_file(TEMPLATE_ROOT / "platform_dashboard.html")
    stylesheet = _read_project_file(STYLE_PATH)

    assert tenant_dashboard.count("overview-card-grid") == 5
    assert "platform-metric-band" in platform_dashboard
    assert ".overview-card-grid > .work-surface" in stylesheet
    assert ".platform-metric-band .metric-item" in stylesheet


def test_shared_styles_define_mobile_navigation_and_stable_work_surfaces() -> None:
    """共享样式必须显式覆盖移动导航与单层业务表面，避免依赖桌面自然挤压。"""

    stylesheet = _read_project_file(STYLE_PATH)

    assert ".work-surface" in stylesheet
    assert ".metric-band" in stylesheet
    assert "@media (max-width: 820px)" in stylesheet
    assert ".app-nav-toggle" in stylesheet


def test_section_navigation_wraps_without_horizontal_scrollbar() -> None:
    """二级导航必须自然换行，不能再通过横向滚动隐藏入口。

    只检查 `.section-tabs` 的基础规则，因为移动端应继承相同的换行语义；若在媒体
    查询中重新加入横向滚动，后面的全局断言仍会捕获该回归。
    """

    stylesheet = _read_project_file(STYLE_PATH)
    section_tabs_rule = stylesheet.split(".section-tabs {", 1)[1].split("}", 1)[0]

    assert "flex-wrap: wrap;" in section_tabs_rule
    assert "overflow-x" not in section_tabs_rule
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
        assert 'class="record-list"' in template_source
        assert 'class="record-row"' in template_source
        assert 'class="record-actions"' in template_source

    assert 'data-modal-open="edit-tenant-user-' in tenant_users
    assert 'data-modal-open="edit-platform-user-' in platform_users
    assert 'data-modal-open="edit-plan-' in platform_plans
    assert '<td colspan="5">' not in tenant_users
    assert '<td colspan="4">' not in platform_users
    assert '<td colspan="9">' not in platform_plans


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

    assert '<div class="table-scroll">' in bots_template
    assert '<tr><td colspan="7"><div class="empty-state">' not in bots_template
    assert '<h1 id="platform-login-title">平台管理后台</h1>' in platform_login


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
