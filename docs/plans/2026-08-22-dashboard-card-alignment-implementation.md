# 总览页卡片对齐 Implementation Plan

> **For Claude:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task.

**Goal:** 让企业总览同排卡片自动等高，并统一平台概览指标的内部纵向节奏。

**Architecture:** 为企业总览的双列容器增加页面专用语义类，只重置其直接子卡片的外边距和拉伸方式；平台概览复用现有 `platform-metric-band` 类统一说明文字位置。所有规则限定在总览页面，不修改全局卡片间距。

**Tech Stack:** FastAPI、Jinja2、原生 CSS、Pytest

---

### Task 1: 增加总览布局契约检查

**Files:**
- Modify: `tests/test_scandinavian_ui.py`

**Step 1: 编写失败检查**

在现有斯堪的纳维亚界面检查中增加以下约束：

```python
def test_overview_grids_align_cards_without_fixed_heights() -> None:
    tenant_dashboard = _read_project_file(TEMPLATE_ROOT / "dashboard.html")
    platform_dashboard = _read_project_file(TEMPLATE_ROOT / "platform_dashboard.html")
    stylesheet = _read_project_file(STATIC_ROOT / "css/style.css")

    assert tenant_dashboard.count("overview-card-grid") == 5
    assert "platform-metric-band" in platform_dashboard
    assert ".overview-card-grid > .work-surface" in stylesheet
    assert ".platform-metric-band .metric-item" in stylesheet
```

**Step 2: 运行检查并确认失败**

Run: `.venv/bin/pytest tests/test_scandinavian_ui.py::test_overview_grids_align_cards_without_fixed_heights -q`

Expected: FAIL，原因是企业总览模板尚未声明 `overview-card-grid`。

### Task 2: 实现企业和平台总览对齐规则

**Files:**
- Modify: `app/templates/dashboard.html`
- Modify: `app/templates/platform_dashboard.html`
- Modify: `app/static/css/style.css`

**Step 1: 标记企业总览双列网格**

为五个企业总览双列容器增加 `overview-card-grid`，保留原有 `panel-grid`、`panel-grid-balanced` 和 `dashboard-top-grid` 类。

**Step 2: 补充总览专用样式**

增加带详细中文注释的样式规则：

```css
/* 总览双列网格只由 gap 管理间距，避免通用相邻卡片外边距造成右侧卡片下移。 */
.overview-card-grid {
  align-items: stretch;
  margin-bottom: 20px;
}

.overview-card-grid > .work-surface {
  display: flex;
  flex-direction: column;
  height: 100%;
  margin: 0;
}

.overview-card-grid > .work-surface + .work-surface {
  margin-top: 0;
}

/* 平台指标说明固定在指标项底部，使不同长度说明保持一致的阅读基线。 */
.platform-metric-band .metric-item {
  display: flex;
  flex-direction: column;
}

.platform-metric-band .metric-item .muted-text {
  margin-top: auto;
  padding-top: 7px;
}
```

在移动端将总览卡片高度恢复为 `auto`，由单列内容自然撑开。

**Step 3: 运行定向检查**

Run: `.venv/bin/pytest tests/test_scandinavian_ui.py::test_overview_grids_align_cards_without_fixed_heights -q`

Expected: PASS

Run: `.venv/bin/pytest tests/test_scandinavian_ui.py -q`

Expected: PASS

Run: `git diff --check`

Expected: 无输出，退出码为 0。

### Task 3: 提交与差异部署

**Files:**
- Modify: `CHANGELOG.md`

**Step 1: 记录变更和实际验证**

在变更日志中记录企业总览等高、平台指标对齐以及本次实际执行的轻量检查，不描述为全量回归。

**Step 2: 提交代码**

```bash
git add app/templates/dashboard.html app/templates/platform_dashboard.html app/static/css/style.css tests/test_scandinavian_ui.py CHANGELOG.md docs/plans/2026-08-22-dashboard-card-alignment-implementation.md
git commit -m "界面：统一总览卡片高度"
```

**Step 3: 本机差异部署**

```bash
MBS_ENV_FILE=.run/production.env docker compose --env-file .env build app
MBS_ENV_FILE=.run/production.env docker compose --env-file .env up -d --no-deps --force-recreate app
```

**Step 4: 验证正式服务**

检查容器为 `healthy`，`/healthz`、`/readyz`、`/dashboard` 登录边界和 `/platform/dashboard` 登录边界正确。使用桌面和移动端视口确认同排卡片上下边缘一致，移动端无多余高度。
