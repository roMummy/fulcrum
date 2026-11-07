# 北欧运营工作台视觉基线 Implementation Plan

> **For Claude:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task.

**Goal:** 把营销页面、企业后台和平台后台统一为具有稳定字体、温暖色调、开放表面、充分留白和低边界密度的北欧运营工作台。

**Architecture:** 继续使用现有 Jinja2 模板和单一 `style.css`，先重建全局字体与设计 token，再调整共享壳层和基础组件，最后校准营销页、总览和代表性列表页面。所有旧视觉兼容样式直接删除，页面继续通过现有语义类共享能力，不新增第二套主题或页面级覆盖体系。

**Tech Stack:** FastAPI、Jinja2、原生 CSS、Fontsource 可变字体资源、Pytest、Playwright

---

### Task 1: 建立稳定字体与全局视觉 token

**Files:**
- Create: `app/static/fonts/albert-sans/`
- Create: `app/static/fonts/noto-sans-sc/`
- Create: `app/static/fonts/FONT-LICENSES.md`
- Modify: `app/static/css/style.css:1-143`
- Modify: `tests/test_scandinavian_ui.py:18-44`

**Step 1: 扩展失败的视觉基线测试**

在 `test_scandinavian_tokens_and_forbidden_visual_patterns()` 中增加以下契约：

```python
for required_rule in (
    '@font-face',
    'font-family: "Albert Sans";',
    'font-family: "Noto Sans SC";',
    '--bg: #f5f0eb;',
    '--surface: rgba(255, 255, 255, 0.46);',
    '--border: rgba(168, 146, 121, 0.22);',
    '--space-section: 56px;',
):
    assert required_rule in stylesheet

assert 'font-size: 15px;' in stylesheet
assert (PROJECT_ROOT / "app/static/fonts/FONT-LICENSES.md").exists()
```

新增字体目录检查，确认 Albert Sans 和 Noto Sans SC 的 `woff2` 文件存在，避免样式重新退化为未安装的系统字体。

**Step 2: 运行测试并确认失败**

Run: `.venv/bin/python -m pytest tests/test_scandinavian_ui.py::test_scandinavian_tokens_and_forbidden_visual_patterns -q`

Expected: FAIL，原因是自托管字体和新的空间 token 尚不存在。

**Step 3: 引入字体资源和许可说明**

从 Fontsource `5.3.0` 包提取以下资源：

- Albert Sans Latin 与 Latin Extended 可变字体；
- Noto Sans SC 的完整 Unicode 分片可变字体与对应 `unicode-range` 声明；
- 两个字体包的 OFL 许可信息。

字体样式必须改用 `/static/fonts/...` 绝对路径，不能依赖 CDN。`FONT-LICENSES.md` 记录包名、版本、来源和许可证。

**Step 4: 重建根 token 和字体层级**

在 `style.css` 顶部增加带详细中文注释的 `@font-face` 和 token：

```css
:root {
  --bg: #f5f0eb;
  --bg-soft: #faf8f5;
  --surface: rgba(255, 255, 255, 0.46);
  --surface-strong: rgba(255, 255, 255, 0.76);
  --border: rgba(168, 146, 121, 0.22);
  --space-section: 56px;
  --space-block: 36px;
}

body {
  font-family: "Noto Sans SC", "Albert Sans", sans-serif;
  font-size: 15px;
  font-weight: 380;
}
```

英文眉题、数字和品牌允许使用 Albert Sans；中文正文始终优先使用 Noto Sans SC。标题字重不超过 `500`，辅助文字默认不低于 `12px`。

**Step 5: 运行定向测试和静态检查**

Run: `.venv/bin/python -m pytest tests/test_scandinavian_ui.py::test_scandinavian_tokens_and_forbidden_visual_patterns -q`

Run: `git diff --check`

Expected: 全部通过。

**Step 6: 提交**

```bash
git add app/static/fonts app/static/css/style.css tests/test_scandinavian_ui.py
git commit -m "界面：建立北欧字体与全局视觉基线"
```

### Task 2: 降低后台壳层重量并建立开放页面层级

**Files:**
- Modify: `app/static/css/style.css:145-755`
- Modify: `app/templates/base.html`
- Modify: `app/templates/platform_base.html`
- Modify: `tests/test_scandinavian_ui.py:46-143`

**Step 1: 增加共享壳层契约**

扩展后台壳层测试，检查：

- 企业和平台继续共享 `.app-topbar`、`.workspace`、`.page-header`；
- 顶栏使用暖色透明表面，不使用连续白色导航块；
- 页面页头不再依赖贯穿全宽的 `border-bottom`；
- 二级导航自然换行且移动端不显示滚动条；
- 页面主区块使用统一的 `--space-section` 间距。

**Step 2: 运行测试并确认失败**

Run: `.venv/bin/python -m pytest tests/test_scandinavian_ui.py -k "backend_shell or section_navigation or shared_styles" -q`

Expected: FAIL，原因是旧顶栏、页头边界和空间规则仍存在。

**Step 3: 调整顶部导航和账户入口**

在共享 CSS 中：

- 将顶栏高度收敛到 `68px`，使用暖灰半透明底色和极轻底边界；
- 品牌标记降低纯黑面积，文字层级改用字体与色温区分；
- 导航激活态只保留低透明度暖白底和文字色，不再同时使用粗字重与底部粗线；
- 下拉浮层使用暖白强表面和更轻的扩散阴影；
- 保持现有 `details/summary` 结构、键盘语义和移动菜单逻辑。

**Step 4: 调整工作区、页头和二级导航**

- 工作区桌面顶部留白提升到 `48px`，底部留白提升到 `88px`；
- 页头底部间距使用 `40px`，删除贯穿边界；
- 后台主标题调整为 `36px`、轻字重；
- 眉题使用木色、Albert Sans 和小型大写字母，不通过过粗字重强调；
- 二级导航使用更疏的间距和单一激活标识，窄屏自然换行并隐藏任何可见滚动条。

**Step 5: 运行定向测试**

Run: `.venv/bin/python -m pytest tests/test_scandinavian_ui.py -k "backend_shell or section_navigation or shared_styles" -q`

Run: `git diff --check`

Expected: 全部通过。

**Step 6: 提交**

```bash
git add app/static/css/style.css app/templates/base.html app/templates/platform_base.html tests/test_scandinavian_ui.py
git commit -m "界面：重塑后台导航与页面层级"
```

### Task 3: 去除卡片墙并统一数据、列表和表单表面

**Files:**
- Modify: `app/static/css/style.css:532-1475`
- Modify: `app/templates/dashboard.html`
- Modify: `app/templates/platform_dashboard.html`
- Modify: `app/templates/tenant_users.html`
- Modify: `app/templates/account_onboarding.html`
- Modify: `app/templates/platform_users.html`
- Modify: `app/templates/platform_plans.html`
- Modify: `tests/test_scandinavian_ui.py:89-168`

**Step 1: 增加开放表面测试**

新增静态契约：

```python
assert ".page-section" in stylesheet
assert ".editor-surface" in stylesheet
assert "background: transparent;" in stylesheet
assert 'class="page-section' in tenant_dashboard
assert 'class="page-section' in platform_dashboard
```

并检查：

- `.work-surface` 不再作为所有页面区块的默认白色背景；
- 总览页使用 `.page-section` 表达无框页面区块；
- 员工新增表单使用 `.editor-surface`；
- `record-list`、`record-row`、`table` 和 `metric-band` 不使用纵向网格线；
- 旧页面级 `.work-surface` 结构在本次代表性页面中被直接替换，不保留兼容别名。

**Step 2: 运行测试并确认失败**

Run: `.venv/bin/python -m pytest tests/test_scandinavian_ui.py -k "dashboards or overview or account_and_plan" -q`

Expected: FAIL，原因是页面仍使用连续工作表面。

**Step 3: 建立单一表面语义**

在 CSS 中明确三类结构，并写详细中文注释：

- `.page-section`：默认透明、无边界，用于页面级内容分组；
- `.editor-surface`：暖白强表面，用于表单编辑器、价格方案和风险操作；
- `.work-surface`：只保留给对话框、独立工具和确有边界的复杂业务面板。

删除与新结构冲突的相邻卡片外边距和嵌套表面降级规则，不新增旧类别名。

**Step 4: 重构指标、列表、表格和表单**

- 指标带使用开放网格和列间留白，仅在大分组间保留轻边界；
- 指标数字提升字号并启用等宽数字特性；
- 数据表删除纵向边界，表头使用低透明度峡湾蓝底色；
- 记录列表改为稳定行高和轻横向分隔，操作按钮固定在末列；
- 输入框使用暖白表面和低透明边界，聚焦时只显示松绿焦点环；
- 徽标降低字重，避免所有状态都形成实色小方块。

**Step 5: 替换代表页面结构**

将企业总览、平台总览、员工管理、账号接入、平台员工和套餐管理的页面级 `.work-surface` 直接替换为 `.page-section` 或 `.editor-surface`。保留现有路由、表单字段、权限和提交地址，不增加旧结构兼容路径。

**Step 6: 运行定向测试**

Run: `.venv/bin/python -m pytest tests/test_scandinavian_ui.py -k "dashboards or overview or account_and_plan or mobile_data_table" -q`

Run: `.venv/bin/python -m pytest tests/test_platform_admin.py tests/test_tenant_admin.py -q`

Expected: 全部通过；若仓库不存在指定测试文件，改用现有同名路由测试并如实记录。

**Step 7: 提交**

```bash
git add app/static/css/style.css app/templates/dashboard.html app/templates/platform_dashboard.html app/templates/tenant_users.html app/templates/account_onboarding.html app/templates/platform_users.html app/templates/platform_plans.html tests/test_scandinavian_ui.py
git commit -m "界面：统一开放数据区与业务列表"
```

### Task 4: 强化营销页面的品牌感和留白节奏

**Files:**
- Modify: `app/static/css/style.css:2079-2555`
- Modify: `app/templates/marketing_home.html`
- Modify: `app/templates/marketing_base.html`
- Modify: `tests/test_scandinavian_ui.py:72-87`

**Step 1: 增加营销结构契约**

测试继续保留真实产品截图、注册登录入口和无障碍语义，并新增：

- 首屏品牌名称是唯一 `h1`；
- 首屏使用 `.marketing-hero-stage` 和 `.marketing-hero-copy`；
- 能力与流程使用开放编号结构，不使用卡片类；
- 价格方案继续使用真实套餐数据和操作地址；
- 页面不新增功能说明或研发过程文案。

**Step 2: 运行测试并确认失败**

Run: `.venv/bin/python -m pytest tests/test_scandinavian_ui.py -k "marketing_and_auth" -q`

Expected: FAIL，原因是新首屏语义尚不存在。

**Step 3: 重构营销首屏**

- 保留 `曜枢 AI` 命名并作为首屏最强视觉信号；
- 将产品价值说明放在品牌下方，不把说明放进卡片；
- 产品截图作为低对比背景素材，但减少与正文叠加的线条和文字噪声；
- 首屏高度保持下一节内容提示可见；
- 主要动作和次级动作继续指向现有注册与价格入口。

**Step 4: 调整能力、流程、价格与行动区**

- 大区块桌面使用 `96px` 至 `128px` 纵向留白；
- 能力和流程使用编号、标题、短正文和留白形成层级；
- 价格卡仅保留一条必要边界，推荐方案通过色温而非粗边框突出；
- 深色行动区改为炭黑和暖白组合，不使用纯黑；
- 页脚降低信息重量并保持导航可访问。

**Step 5: 运行定向测试**

Run: `.venv/bin/python -m pytest tests/test_scandinavian_ui.py -k "marketing_and_auth" -q`

Run: `git diff --check`

Expected: 全部通过。

**Step 6: 提交**

```bash
git add app/static/css/style.css app/templates/marketing_home.html app/templates/marketing_base.html tests/test_scandinavian_ui.py
git commit -m "界面：提升营销页面品牌与留白质感"
```

### Task 5: 完成响应式校准和真实浏览器验证

**Files:**
- Modify: `app/static/css/style.css:2556-3013`
- Modify: `tests/test_scandinavian_ui.py`
- Modify: `CHANGELOG.md`

**Step 1: 补充响应式契约**

增加以下检查：

- `1100px`、`820px` 和 `560px` 三个断点继续存在；
- 移动端页面左右内边距不小于 `20px`；
- 页面级区块在移动端恢复单列和自然高度；
- 二级导航无可见滚动条；
- 表格只在 `.table-scroll` 内滚动；
- `prefers-reduced-motion` 会关闭非必要过渡。

**Step 2: 调整响应式样式**

- 平板减少指标和双列区块列数；
- 移动端保持 `15px` 正文，不使用视口宽度缩放字号；
- 顶栏展开菜单、账户入口、按钮组和表单控件不能互相遮挡；
- 营销首屏、价格卡、指标带、记录行和数据表都保持稳定宽度；
- 删除与新基线冲突的旧移动端覆盖规则。

**Step 3: 运行轻量自动检查**

Run: `.venv/bin/python -m pytest tests/test_scandinavian_ui.py -q`

Run: `.venv/bin/python -m pytest tests/test_platform_admin.py tests/test_tenant_admin.py -q`

Run: `git diff --check`

Expected: 全部通过；只记录实际执行结果，不描述为全量回归。

**Step 4: 启动当前分支服务**

在未占用端口启动当前代码，使用 `.run/release-test-accounts.json` 读取测试账号，不打印密码：

```bash
.venv/bin/python -m uvicorn app.main:app --host 127.0.0.1 --port 8290
```

**Step 5: 使用 Playwright 检查桌面与移动端**

检查 `1536x864` 和 `390x844`：

- `/` 营销首页；
- `/dashboard` 企业总览；
- `/platform/dashboard` 平台总览；
- 账号接入列表；
- 企业员工列表。

验证字体资源实际加载、页面无横向溢出、文字不遮挡、导航菜单可操作、列表行稳定、后台不再形成连续卡片墙。截图写入 `.run/ui-audit/`，不提交运行产物。

**Step 6: 记录变更和验证**

在 `CHANGELOG.md` 中记录：

- 自托管字体与视觉 token；
- 后台开放页面层级；
- 指标、列表、表格、表单和营销页面调整；
- 实际执行的测试和浏览器视口。

**Step 7: 提交**

```bash
git add app/static/css/style.css tests/test_scandinavian_ui.py CHANGELOG.md
git commit -m "界面：完成北欧工作台响应式校准"
```

