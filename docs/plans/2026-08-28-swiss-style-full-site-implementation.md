# 全站 Swiss Style 视觉重构 Implementation Plan

> **For Claude:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task.

**Goal:** 将营销站点、认证页面、企业工作台和平台管理后台统一切换为实用型 Swiss Style，并解决后台业务分区不清晰的问题。

**Architecture:** 直接替换现有共享 CSS token 和组件基线，通过当前 Jinja 模板的既有语义覆盖全站；只为营销首屏、认证分栏和平台大盘补充必要结构类。删除 Scandinavian 测试契约并重命名为 Swiss Style 契约，不增加主题切换或兼容样式。

**Tech Stack:** FastAPI、Jinja2、原生 CSS、Pytest、Playwright Chromium、Docker Compose

---

### Task 1: 建立 Swiss Style 静态契约

**Files:**
- Rename: `tests/test_scandinavian_ui.py` -> `tests/test_swiss_ui.py`
- Modify: `tests/test_swiss_ui.py`

**Step 1: 重命名测试文件**

将旧 Scandinavian 测试文件直接重命名，不保留旧测试入口。

**Step 2: 修改视觉 token 断言**

将暖灰、松绿和 `6px` 圆角断言替换为以下 Swiss Style 契约：

```python
for required_token in (
    "--bg: #fafafa;",
    "--surface: #ffffff;",
    "--surface-muted: #f0f0f0;",
    "--text: #0a0a0a;",
    "--signal: #ff0000;",
    "--info: #0057b8;",
    "--warning: #ffcc00;",
    "--radius-lg: 0;",
):
    assert required_token in stylesheet
```

增加导航硬边界、直角业务表面、营销首屏结构、认证分栏和平台大盘分区断言。保留渐变、玻璃模糊、全圆角和整页横向溢出禁令。

**Step 3: 运行测试确认当前样式不符合新契约**

Run: `.venv/bin/python -m pytest tests/test_swiss_ui.py -q`

Expected: FAIL，失败项集中在旧 token、旧圆角、旧表面和缺失的 Swiss 结构类。

**Step 4: 提交测试契约**

```bash
git add tests/test_scandinavian_ui.py tests/test_swiss_ui.py
git commit -m "测试：建立全站Swiss Style视觉契约"
```

### Task 2: 替换全局视觉 token 与共享组件

**Files:**
- Modify: `app/static/css/style.css`
- Modify: `app/templates/base.html`
- Modify: `app/templates/marketing_base.html`

**Step 1: 替换全局 token**

将背景、表面、文字、边界、主色、信息色、警示色、阴影和圆角直接替换为 Swiss Style 色板。删除旧暖灰透明表面和松绿视觉主线，不创建旧变量别名。

**Step 2: 重构共享组件**

直接修改以下组件：

- 顶部导航：白底、黑色底边、红色品牌标记和当前导航信号；
- 页面标题：更强字号、黑色文字、红色眉题；
- `.page-section`：白色实体表面、黑色顶部规则、稳定内边距；
- `.editor-surface`、`.work-surface`、`.panel`：白底黑边直角表面；
- `.metric-band`：连续白色数据网格和黑色顶部规则；
- 按钮、徽标、输入框、表格、记录列表、弹窗：直角、黑边、红蓝黄状态映射；
- 图表：红色主趋势、蓝色次趋势、黄色警示趋势。

**Step 3: 更新浏览器主题色**

将两个基础模板的 `theme-color` 改为 `#fafafa`，并更新相关测试断言。

**Step 4: 运行全局静态契约**

Run: `.venv/bin/python -m pytest tests/test_swiss_ui.py -q`

Expected: Swiss token 和共享组件断言通过；页面专用结构断言可能仍失败。

**Step 5: 提交全局基线**

```bash
git add app/static/css/style.css app/templates/base.html app/templates/marketing_base.html tests/test_swiss_ui.py
git commit -m "界面：建立全站Swiss Style视觉基线"
```

### Task 3: 重构营销与认证页面

**Files:**
- Modify: `app/templates/marketing_home.html`
- Modify: `app/templates/enterprise_login.html`
- Modify: `app/templates/register.html`
- Modify: `app/templates/platform_login.html`
- Modify: `app/static/css/style.css`
- Test: `tests/test_swiss_ui.py`

**Step 1: 增加营销首屏 Swiss 网格语义**

在现有真实控制台截图和文案外增加明确的内容列、产品列和红色信号块语义。保留 `/static/images/yaoshu-console.webp`，不增加装饰插画。

**Step 2: 重构营销业务区**

能力、流程、价格和 CTA 使用编号、黑色规则、灰色分区和推荐套餐红色信号条。删除柔和卡片悬浮和大圆角表现。

**Step 3: 重构认证分栏**

企业登录与注册沿用 `marketing-auth-layout`，将上下文列改为红色或黑色实体区，表单列改为白色直角表面。平台登录通过现有认证壳使用相同黑白红结构，同时保留平台身份边界和字段名称。

**Step 4: 运行营销与认证定向测试**

Run: `.venv/bin/python -m pytest tests/test_swiss_ui.py -q`

Expected: PASS。

**Step 5: 提交营销与认证页面**

```bash
git add app/templates/marketing_home.html app/templates/enterprise_login.html app/templates/register.html app/templates/platform_login.html app/static/css/style.css tests/test_swiss_ui.py
git commit -m "界面：重构Swiss Style营销与认证体验"
```

### Task 4: 完成双后台与数据大盘分区

**Files:**
- Modify: `app/templates/dashboard.html`
- Modify: `app/templates/platform_dashboard.html`
- Modify: `app/static/css/style.css`
- Test: `tests/test_swiss_ui.py`

**Step 1: 校准企业总览**

为企业核心指标、趋势、排行、风险、状态和机器人总览使用统一业务表面。保留当前数据和链接，不增加卡片嵌套。

**Step 2: 增加平台大盘组合表面**

平台大盘按以下结构分区：

```html
<section class="platform-analytics-surface platform-analytics-trends-surface">...</section>
<section class="platform-analytics-surface platform-analytics-operations-surface">...</section>
<section class="page-section platform-channel-summary">...</section>
<section class="page-section platform-tenant-rankings">...</section>
<section class="platform-analytics-surface platform-analytics-health-surface">...</section>
```

组合表面内部继续使用双列 `.platform-analytics-grid`，移动端切为单列。内部 `.page-section` 必须移除重复背景和顶部规则，保持单层结构。

**Step 3: 运行定向测试与差异检查**

Run: `.venv/bin/python -m pytest tests/test_swiss_ui.py tests/test_platform_admin.py -q`

Run: `git diff --check`

Expected: PASS。

**Step 4: 提交后台与大盘**

```bash
git add app/templates/dashboard.html app/templates/platform_dashboard.html app/static/css/style.css tests/test_swiss_ui.py
git commit -m "界面：完成双后台Swiss Style业务分区"
```

### Task 5: 浏览器校准与生产部署

**Files:**
- Modify when required: `app/static/css/style.css`
- Modify when required: `tests/test_swiss_ui.py`

**Step 1: 构建本地应用镜像并差异替换应用容器**

Run: `docker compose build app`

Run: `docker compose up -d --no-deps app`

Expected: `multi-bot-saas-app` healthy。

**Step 2: 使用 Playwright 核验代表页面**

在 `1536x960` 和 `390x844` 视口核验：

- `/`；
- `/login`；
- `/register`；
- `/platform/login`；
- `/dashboard`；
- `/platform/dashboard`。

检查根节点横向溢出、导航折叠、按钮边界、表单宽度、业务表面背景、平台大盘五个分区和表格自身滚动。

**Step 3: 修复浏览器核验发现的问题**

只修改与 Swiss Style 重构直接相关的布局或视觉阻断问题，并重新运行相关截图检查。

**Step 4: 运行最终轻量验证**

Run: `.venv/bin/python -m pytest tests/test_swiss_ui.py tests/test_platform_admin.py -q`

Run: `git diff --check`

Run: `curl -fsS http://127.0.0.1:8280/healthz`

Run: `curl -fsS http://127.0.0.1:8280/readyz`

Expected: 测试通过、无差异错误、健康与就绪接口成功。

**Step 5: 提交浏览器校准**

```bash
git add app/static/css/style.css tests/test_swiss_ui.py
git commit -m "界面：完成Swiss Style响应式校准"
```

**Step 6: 推送当前分支并核验提交状态**

Run: `git push origin ui/scandinavian-refactor`

Expected: 远程分支指向最终提交，工作区仅保留用户自有 `.rcodex/`。
