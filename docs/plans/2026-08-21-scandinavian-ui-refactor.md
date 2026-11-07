# 曜枢 AI 北欧极简 UI 重构 Implementation Plan

> **For Claude:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task.

**Goal:** 将营销页、注册登录页、企业租户后台和平台方后台统一重构为适合 SaaS 产品的北欧极简视觉，并删除旧视觉路径。

**Architecture:** 继续使用 FastAPI、Jinja2 和单一静态样式文件，不引入新的前端运行时。先用静态视觉契约锁定禁止项和必备结构，再重构共享页面壳层、公开页面、总览页面与通用业务组件；现有路由、权限、表单提交和数据上下文保持不变。

**Tech Stack:** FastAPI、Jinja2、原生 CSS、原生 JavaScript、Pytest、Chromium Headless

---

### Task 1: 建立北欧极简视觉契约

**Files:**
- Create: `tests/test_scandinavian_ui.py`
- Modify: `app/static/css/style.css`

**Step 1: 编写当前会失败的静态视觉契约**

测试读取 `style.css`、`base.html`、`marketing_base.html` 和代表性页面模板，并校验：

- 存在暖灰白、炭灰、苔绿、木色等设计 token；
- 全局样式不存在渐变、999 像素圆角和重阴影；
- 双后台壳层具有侧边导航和独立身份标识结构；
- 营销首屏仍使用真实控制台素材；
- 认证表单保留 label、autocomplete 和错误播报语义。

**Step 2: 运行测试确认旧视觉不满足契约**

Run: `pytest -q tests/test_scandinavian_ui.py`

Expected: FAIL，指出旧渐变、胶囊圆角或新壳层结构尚未实现。

**Step 3: 提交视觉契约**

```bash
git add tests/test_scandinavian_ui.py
git commit -m "测试：增加北欧极简视觉契约"
```

### Task 2: 重构企业与平台后台共享壳层

**Files:**
- Modify: `app/templates/base.html`
- Modify: `app/templates/platform_base.html`
- Modify: `app/static/css/style.css`

**Step 1: 调整共享模板结构**

- 桌面端改为窄侧边导航和主工作区；
- 企业后台与平台后台根据请求路径呈现不同身份标签和首页地址；
- 页面标题、介绍、模块导航和主要操作放入无卡片页面头；
- 保留现有 CSRF 注销、确认弹窗、焦点管理和导航上下文变量。

**Step 2: 重写基础 token 与共享壳层 CSS**

- 采用已确认的暖灰白、炭灰、苔绿和自然木色色板；
- 删除渐变、胶囊导航、玻璃效果和无意义阴影；
- 建立桌面侧栏、移动端顶部栏、主内容区和页面头的稳定尺寸；
- 为键盘焦点和交互状态提供低干扰但清晰的反馈。

**Step 3: 执行模板与视觉契约检查**

Run: `pytest -q tests/test_scandinavian_ui.py tests/test_saas_login.py::test_enterprise_and_platform_login_pages_are_separate`

Expected: 壳层相关断言通过；尚未迁移的公开页断言允许继续失败。

**Step 4: 提交共享壳层**

```bash
git add app/templates/base.html app/templates/platform_base.html app/static/css/style.css tests/test_scandinavian_ui.py
git commit -m "界面：重构双后台北欧极简壳层"
```

### Task 3: 重构营销、价格与认证页面

**Files:**
- Modify: `app/templates/marketing_base.html`
- Modify: `app/templates/marketing_home.html`
- Modify: `app/templates/pricing.html`
- Modify: `app/templates/enterprise_login.html`
- Modify: `app/templates/register.html`
- Modify: `app/templates/platform_login.html`
- Modify: `app/templates/setup.html`
- Modify: `app/templates/setup_completed.html`
- Modify: `app/static/css/style.css`

**Step 1: 重构营销站点结构**

- 首屏使用真实控制台画面、暖色背景和无卡片文字层；
- 能力、流程、价格和行动区采用全宽内容带与自然分隔；
- 套餐作为可比较业务对象保留轻量表面，不使用悬浮阴影。

**Step 2: 重构认证和安装页结构**

- 企业登录与注册使用安静双栏布局；
- 平台登录与首次安装使用相同设计语言但明确标注平台身份；
- 输入框采用透明或极浅背景、轻边界和明确 focus 状态；
- 不删除验证码、错误提示、密码说明和安全语义。

**Step 3: 运行公开页面定向测试**

Run: `pytest -q tests/test_public_marketing_pages.py tests/test_public_site_auth.py::test_public_pages_are_accessible_but_dashboard_requires_tenant_login tests/test_saas_login.py::test_enterprise_and_platform_login_pages_are_separate tests/test_scandinavian_ui.py`

Expected: PASS。

**Step 4: 提交公开与认证页面**

```bash
git add app/templates/marketing_base.html app/templates/marketing_home.html app/templates/pricing.html app/templates/enterprise_login.html app/templates/register.html app/templates/platform_login.html app/templates/setup.html app/templates/setup_completed.html app/static/css/style.css tests/test_scandinavian_ui.py
git commit -m "界面：重构营销站点与认证页面"
```

### Task 4: 重构企业与平台总览

**Files:**
- Modify: `app/templates/dashboard.html`
- Modify: `app/templates/platform_dashboard.html`
- Modify: `app/static/css/style.css`

**Step 1: 将指标卡片改为连续数据带**

企业总览和平台总览都使用连续指标区域，通过字号、留白和必要状态色区分信息，不再为每个指标绘制独立卡片。

**Step 2: 重组趋势、排行、告警和最近租户**

- 趋势图使用必要的单层工作表面；
- 排行和告警使用分组列表，不再卡片套卡片；
- 平台最近租户使用单一表格区域。

**Step 3: 运行代表性页面测试**

Run: `pytest -q tests/test_public_site_auth.py::test_owner_and_employee_use_the_same_enterprise_login tests/test_platform_admin.py -k 'dashboard or pages' tests/test_scandinavian_ui.py`

Expected: PASS；若 `-k` 未选中用例，则补充直接页面渲染检查并在提交说明中如实记录。

**Step 4: 提交总览页面**

```bash
git add app/templates/dashboard.html app/templates/platform_dashboard.html app/static/css/style.css tests/test_scandinavian_ui.py
git commit -m "界面：重构企业与平台运营总览"
```

### Task 5: 统一列表、详情、表单和业务工作台

**Files:**
- Modify: `app/templates/*.html`
- Modify: `app/static/css/style.css`

**Step 1: 迁移通用业务页面语义**

- 列表页统一为单一表格表面；
- 详情页统一使用定义列表、自然分栏和必要操作区；
- 表单只在需要聚合字段时使用表面，不为单个字段建卡；
- 模块标签改为底线导航；
- 状态标签改为小圆角矩形；
- 删除不再被模板引用的旧视觉选择器。

重点覆盖机器人、渠道、插件、消息、会话、联系人、群聊、团队、套餐、订单、绑定、租户、平台员工、监控和日志页面。

**Step 2: 保留高复杂度业务组件能力**

二维码登录、JSON 编辑器、日志查看器、确认弹窗和动态操作台继续保持功能，但改用同一色板、边界、圆角和状态反馈。

**Step 3: 执行代表性定向测试**

Run: `pytest -q tests/test_tenant_web_isolation.py tests/test_tenant_billing_pages.py tests/test_platform_billing_pages.py tests/test_platform_operations.py tests/test_provider_node_pages.py tests/test_scandinavian_ui.py`

Expected: PASS。

**Step 4: 提交通用业务页面**

```bash
git add app/templates app/static/css/style.css tests/test_scandinavian_ui.py
git commit -m "界面：统一全站业务页面视觉语义"
```

### Task 6: 响应式、视觉检查与本地交付

**Files:**
- Modify: `app/static/css/style.css`
- Modify: `tests/test_scandinavian_ui.py`

**Step 1: 完成移动端规则**

- 侧边导航在移动端转为顶部可展开导航；
- 表格可横向滚动或按已有页面语义转为记录布局；
- 双栏认证、详情和表单自然堆叠；
- 固定格式组件使用稳定高度、宽度或纵横比，避免内容引发布局跳动。

**Step 2: 运行轻量验证**

Run: `pytest -q tests/test_scandinavian_ui.py tests/test_public_marketing_pages.py tests/test_saas_login.py::test_enterprise_and_platform_login_pages_are_separate tests/test_public_site_auth.py::test_public_pages_are_accessible_but_dashboard_requires_tenant_login`

Run: `git diff --check`

Expected: PASS，且无空白错误。

**Step 3: 启动本地服务并截图检查**

使用项目现有运行配置启动本地服务，至少检查以下页面的桌面和移动视口：

- `/`
- `/pricing`
- `/login`
- `/register`
- `/platform/login`
- 企业 `/dashboard` 及一个列表/详情页面
- 平台 `/platform/dashboard` 及一个列表/详情页面

重点检查非空白、首屏构图、导航可用、文字不溢出、组件不重叠和真实素材正常显示。

**Step 4: 提交响应式与验证收尾**

```bash
git add app/static/css/style.css tests/test_scandinavian_ui.py
git commit -m "界面：完成北欧极简响应式与视觉收尾"
```

## 完成标准

- 四类页面使用同一套 Scandinavian SaaS 视觉语言；
- 页面不再依赖大面积卡片、厚重边框、渐变和强阴影建立层级；
- 企业与平台后台身份和菜单边界不变；
- 公开注册、登录、验证码、套餐、订单和后台操作流程不变；
- 轻量定向测试、模板渲染和桌面/移动视觉检查完成；
- 每个实施单元均使用中文提交，并如实记录实际执行的测试。
