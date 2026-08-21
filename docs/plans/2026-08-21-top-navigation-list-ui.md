# 顶部导航与紧凑列表界面优化 Implementation Plan

> **For Claude:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task.

**Goal:** 将双后台桌面侧栏替换为顶部主导航，修复二级菜单滚动条，并把表格内嵌编辑表单统一改为紧凑列表和弹窗编辑，同时精简研发过程与重复解释文案。

**Architecture:** 继续使用 FastAPI、Jinja2、原生 CSS 和原生 JavaScript，不改变现有路由、表单字段、权限校验与数据库结构。后台壳层只保留一套导航数据，桌面展示为顶部菜单，移动端展示为可展开面板；员工和套餐管理继续提交现有服务端接口，只调整呈现与编辑入口。

**Tech Stack:** FastAPI、Jinja2、原生 CSS、原生 JavaScript、Pytest、Chromium Headless

---

### Task 1: 更新顶部导航与列表静态契约

**Files:**
- Modify: `tests/test_scandinavian_ui.py`

**Step 1: 更新后台壳层契约**

把旧侧栏断言替换为顶部导航断言，要求 `base.html` 包含桌面顶部栏、主导航、低频“更多”菜单、账号菜单和移动端导航触发器，并断言不再存在 `app-sidebar` 结构。

**Step 2: 增加二级导航与紧凑列表契约**

静态检查 `style.css` 中的二级导航允许换行且没有 `overflow-x: auto`；检查企业员工、平台员工和套餐页面包含记录列表、操作菜单与编辑弹窗，同时不再出现 `td colspan` 包裹编辑表单。

**Step 3: 运行定向测试确认契约会在旧结构上失败**

Run: `pytest -q tests/test_scandinavian_ui.py`

Expected: FAIL，失败点仅指向尚未实现的顶部导航或紧凑列表结构。

**Step 4: 提交契约**

```bash
git add tests/test_scandinavian_ui.py
git commit -m "测试：更新顶部导航与紧凑列表界面契约"
```

### Task 2: 将双后台壳层改为顶部导航

**Files:**
- Modify: `app/templates/base.html`
- Modify: `app/static/css/style.css`

**Step 1: 重构共享模板结构**

- 删除桌面侧栏结构和旧侧栏专用类名；
- 新增统一顶部栏，左侧保留品牌，中部展示常用入口，低频入口进入 `details.nav-more`；
- 右侧使用账号菜单承载身份摘要、账号安全和带 CSRF 的退出操作；
- 移动端继续使用无脚本复选框展开导航，链接来源仍为 `primary_nav_items` 和 `secondary_nav_items`；
- 复用现有“点击外部及 Esc 关闭更多菜单”脚本，并让账号菜单具备相同行为。

**Step 2: 重写壳层样式**

- `.app-frame` 改为纵向页面结构；
- 顶部栏采用稳定高度、克制背景和轻边界，不使用悬浮卡片；
- 工作区宽度只受内容宽度和视口边距限制，不再扣除侧栏宽度；
- 820 像素以下把主导航展开为全宽纵向菜单，保证菜单、账号操作和页面内容不重叠。

**Step 3: 修复二级导航滚动条**

将 `.section-tabs` 改为 `flex-wrap: wrap`，移除所有断点内的横向滚动规则；标签允许自然换行，并保持当前项底部短线。

**Step 4: 运行壳层定向检查**

Run: `pytest -q tests/test_scandinavian_ui.py tests/test_saas_login.py::test_enterprise_and_platform_login_pages_are_separate`

Expected: 顶部壳层和身份边界断言通过；尚未完成的列表断言允许继续失败。

**Step 5: 提交顶部壳层**

```bash
git add app/templates/base.html app/static/css/style.css tests/test_scandinavian_ui.py
git commit -m "界面：将双后台主导航调整为顶部布局"
```

### Task 3: 统一员工和套餐管理列表

**Files:**
- Modify: `app/templates/tenant_users.html`
- Modify: `app/templates/platform_users.html`
- Modify: `app/templates/platform_plans.html`
- Modify: `app/static/css/style.css`

**Step 1: 建立通用紧凑记录样式**

增加 `record-list`、`record-row`、`record-primary`、`record-meta` 和 `record-actions` 等通用样式。桌面端使用稳定网格对齐摘要字段，移动端自动堆叠；操作菜单宽度固定，不因文案变化推动布局。

**Step 2: 重构企业员工清单**

每条记录只显示账号、手机号、姓名、角色和状态。管理员在右侧“更多”菜单中打开该员工编辑弹窗或提交重置密码；无管理权限的员工只看到摘要，不渲染管理入口。编辑弹窗继续提交 `/tenant/users/{id}`，字段和 CSRF 不变。

**Step 3: 重构平台员工清单**

采用同一记录结构，摘要显示平台账号、姓名、固定权限和状态。编辑弹窗继续提交 `/platform/users/{id}`，重置密码仍是独立表单。

**Step 4: 重构套餐清单**

套餐摘要保留名称、编码、机器人额度、价格、公开和状态信息；原来位于下一表格行的大表单移入对应编辑弹窗。旧 Provider 节点额度字段因服务端仍要求提交而保留在表单中，但去除“旧”与“兼容”的可见研发措辞，使用中性业务标签。

**Step 5: 运行管理页面定向测试**

Run: `pytest -q tests/test_tenant_admin.py tests/test_platform_admin.py tests/test_platform_billing_pages.py tests/test_scandinavian_ui.py`

Expected: PASS。

**Step 6: 提交紧凑列表**

```bash
git add app/templates/tenant_users.html app/templates/platform_users.html app/templates/platform_plans.html app/static/css/style.css tests/test_scandinavian_ui.py
git commit -m "界面：统一员工与套餐紧凑列表交互"
```

### Task 4: 治理页面可见解释文案

**Files:**
- Modify: `app/templates/*.html`
- Modify: `tests/test_scandinavian_ui.py`

**Step 1: 盘点可见研发过程文案**

使用 `rg` 检查 `研发`、`迁移`、`兼容`、`旧版`、`调试`、`当前阶段`、`后续操作`、`仅用于` 等词，只处理用户可见模板文本，不删除必要的中文代码注释。

**Step 2: 删除或改写低价值文案**

- 删除重复说明页面用途的 `page_intro` 和空状态解释；
- 把内部迁移状态、旧实现兼容和调试方式改为用户可理解的业务状态或直接移除；
- 删除安装页对部署过程、数据库迁移过程和环境文件的研发式说明；
- 保留权限、安全、额度、绑定约束、错误、一次性密码及不可逆操作信息。

**Step 3: 增加文案治理静态断言**

代表性模板不得出现已删除的研发过程词组；保留一次性密码、确认操作和身份边界相关文本断言。

**Step 4: 运行代表性页面测试**

Run: `pytest -q tests/test_scandinavian_ui.py tests/test_public_marketing_pages.py tests/test_saas_login.py tests/test_platform_operations.py`

Expected: PASS。

**Step 5: 提交文案治理**

```bash
git add app/templates tests/test_scandinavian_ui.py
git commit -m "界面：精简研发过程与重复解释文案"
```

### Task 5: 响应式检查与轻量收尾

**Files:**
- Modify: `app/static/css/style.css`
- Modify: `tests/test_scandinavian_ui.py`

**Step 1: 执行语法与空白检查**

Run: `pytest -q tests/test_scandinavian_ui.py tests/test_tenant_admin.py tests/test_platform_admin.py tests/test_platform_billing_pages.py`

Run: `git diff --check`

Expected: PASS，且无空白错误。

**Step 2: 启动本地服务并检查关键页面**

使用项目现有启动方式，在桌面和移动视口检查企业员工、平台员工和套餐管理页：顶部导航不遮挡内容、二级菜单无滚动条、记录列表不产生页面级横向滚动、菜单和弹窗可操作。

**Step 3: 根据截图修正响应式细节**

只处理本轮布局引入的文字溢出、菜单定位、弹窗高度和列表对齐问题，不扩大到无关业务页面。

**Step 4: 提交验证收尾**

```bash
git add app/static/css/style.css tests/test_scandinavian_ui.py
git commit -m "界面：完成顶部导航与列表响应式收尾"
```
