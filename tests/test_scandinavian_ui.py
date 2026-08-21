from __future__ import annotations

from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parents[1]
STYLE_PATH = PROJECT_ROOT / "app/static/css/style.css"
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

    for required_token in (
        "--bg: #f5f0eb;",
        "--text: #3d3d3d;",
        "--primary: #5a7a6b;",
        "--wood: #a89279;",
        "--radius-lg: 6px;",
    ):
        assert required_token in stylesheet

    for forbidden_pattern in (
        "linear-gradient(",
        "radial-gradient(",
        "backdrop-filter:",
        "border-radius: 999px",
    ):
        assert forbidden_pattern not in stylesheet


def test_backend_shell_uses_sidebar_and_keeps_platform_identity_separate() -> None:
    """双后台必须共享安静的侧边壳层，同时保留平台与企业身份的清晰边界。"""

    base_template = _read_project_file(TEMPLATE_ROOT / "base.html")
    platform_template = _read_project_file(TEMPLATE_ROOT / "platform_base.html")

    for required_class in (
        'class="app-sidebar"',
        'class="app-sidebar-nav"',
        'class="app-main"',
        'class="page-header"',
    ):
        assert required_class in base_template

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


def test_shared_styles_define_mobile_navigation_and_stable_work_surfaces() -> None:
    """共享样式必须显式覆盖移动导航与单层业务表面，避免依赖桌面自然挤压。"""

    stylesheet = _read_project_file(STYLE_PATH)

    assert ".work-surface" in stylesheet
    assert ".metric-band" in stylesheet
    assert "@media (max-width: 820px)" in stylesheet
    assert ".app-nav-toggle" in stylesheet
