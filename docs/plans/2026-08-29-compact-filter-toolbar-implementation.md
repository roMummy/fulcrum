# Compact Filter Toolbar Implementation Plan

> **For Claude:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task.

**Goal:** 将五个后台查询页面的独立大筛选卡片统一改成桌面紧凑、移动可折叠的筛选工具栏，让列表和日志优先进入首屏。

**Architecture:** 保持现有 GET 查询参数、路由和服务端筛选逻辑不变，只在 Jinja 模板中统一 `compact-filter` 结构，并在共享 Swiss Style 样式表中提供桌面、窄屏和移动端规则。筛选表单直接并入结果业务表面，使用原生 `details/summary` 完成移动折叠，不增加 JavaScript 状态或兼容分支。

**Tech Stack:** FastAPI、Jinja2、原生 HTML/CSS、Pytest、Playwright

---

### Task 1: 建立紧凑筛选结构约束

**Files:**
- Modify: `tests/test_swiss_ui.py`

**Step 1: 增加失败测试**

新增 `test_query_pages_share_compact_filter_toolbar_and_mobile_collapse`，要求：

- `messages.html`、`conversations.html`、`plugins.html`、`platform_logs.html` 和 `workwechat_contact_list.html` 都包含 `compact-filter`、`compact-filter-summary` 和 `compact-filter-form`。
- 五个页面的 GET 筛选表单不再使用 `form-grid`。
- 消息、会话和平台日志不再包含独立 `filter-card`。
- 共享样式包含 36px 控件、弹性关键词字段、移动端折叠与无横向溢出规则。

测试使用详细中文注释解释结构约束，避免只验证易碎的视觉文案。

**Step 2: 运行测试确认失败**

Run: `.venv/bin/python -m pytest tests/test_swiss_ui.py -q`

Expected: FAIL，指出模板尚未包含 `compact-filter` 结构。

### Task 2: 实现共享紧凑筛选样式

**Files:**
- Modify: `app/static/css/style.css`

**Step 1: 增加桌面共享规则**

新增带详细中文注释的以下样式语义：

- `.compact-filter`
- `.compact-filter-summary`
- `.compact-filter-summary-text`
- `.compact-filter-form`
- `.compact-filter-field`
- `.compact-filter-field-query`
- `.compact-filter-actions`
- `.compact-filter-count`

桌面端使用 flex/grid 混合布局，控件最小高度为 36px，关键词字段使用 `flex: 1`，枚举字段使用稳定宽度，操作区不占据整行。

**Step 2: 增加移动规则**

在 `@media (max-width: 720px)` 中让 `summary` 成为可见折叠入口，未展开时隐藏表单；展开后字段单列排列，按钮同一行并允许换行。桌面端隐藏原生 summary 标记但保持 details 内容展开。

**Step 3: 执行静态检查**

Run: `git diff --check`

Expected: PASS。

### Task 3: 收敛消息与会话页面

**Files:**
- Modify: `app/templates/messages.html`
- Modify: `app/templates/conversations.html`

**Step 1: 合并筛选和结果区域**

删除两个页面的独立筛选业务表面，把筛选工具栏放入列表 `work-surface` 顶部。保留所有原查询字段、选中状态、分页参数和重置地址。

**Step 2: 精简消息统计**

删除消息筛选卡片中的四项指标网格，把总匹配数、当前页结果数和入站/出站数量改为标题旁的紧凑摘要。删除筛选说明和装饰性 kicker，不改变列表字段与空状态。

**Step 3: 保持移动展开条件**

当任意查询条件有效时给 `details` 输出 `open`，无筛选条件时移动端默认折叠。添加中文模板注释说明判断只服务于 UI 展开状态，不改变查询行为。

### Task 4: 收敛插件、日志和联系人页面

**Files:**
- Modify: `app/templates/plugins.html`
- Modify: `app/templates/platform_logs.html`
- Modify: `app/templates/workwechat_contact_list.html`

**Step 1: 改造插件筛选**

保留插件刷新和运行状态动作，把五项筛选条件改成紧凑工具栏，关键词字段优先占宽，当前显示数量进入摘要。

**Step 2: 改造平台日志筛选**

删除独立筛选卡片，把文件和关键字筛选移动到日志结果区域。保留 Error/Info 查询参数拼接、分页和日志原文区域。

**Step 3: 改造联系人筛选**

把筛选工具栏并入联系人列表表面。保留同步联系人动作、联系人统计、筛选参数和列表内容，不改企微业务逻辑。

### Task 5: 定向验证并提交

**Files:**
- Modify: `CHANGELOG.md`

**Step 1: 记录变更**

在变更日志中记录全站后台筛选区改为紧凑工具栏、移动端可折叠以及有效内容优先展示。

**Step 2: 运行轻量测试**

Run: `.venv/bin/python -m pytest tests/test_swiss_ui.py -q`

Run: `.venv/bin/python -m compileall app tests/test_swiss_ui.py`

Run: `git diff --check`

Expected: 全部通过。不运行完整历史回归。

**Step 3: 提交实现**

```bash
git add app/static/css/style.css app/templates/messages.html app/templates/conversations.html app/templates/plugins.html app/templates/platform_logs.html app/templates/workwechat_contact_list.html tests/test_swiss_ui.py CHANGELOG.md
git commit -m "界面：统一后台紧凑筛选工具栏"
```

提交正文如实记录实际执行的定向测试，明确未执行全量历史回归。

### Task 6: 推送、部署和视觉验收

**Files:**
- Runtime only: `.run/ui-audit/compact-filters/`

**Step 1: 推送分支**

Run: `git push origin feature/ipad-874-adapter`

Expected: 远端分支更新到实现提交。

**Step 2: 远端差异部署**

在 `192.168.2.142:/data/multi-bot-saas` 执行：

```bash
git pull --ff-only origin feature/ipad-874-adapter
docker compose build app
docker compose run --rm app alembic upgrade head
docker compose up -d --no-deps app
```

不备份数据库、不重建 MariaDB、不删除卷。

**Step 3: 运行时验证**

检查 `/healthz`、`/readyz`、容器健康和迁移头。使用现有平台、企业测试账号验证五个筛选页面的 HTTP 状态。

**Step 4: 桌面与移动视觉检查**

使用 Playwright 在 `1440x900` 和 `390x844` 视口检查代表页面：

- 消息列表
- 插件清单
- 平台日志
- 企业微信联系人

截图写入 `.run/ui-audit/compact-filters/`，确认桌面筛选栏紧凑、移动默认折叠或按有效条件展开、列表首屏前移、页面无横向溢出和控制台错误。

