# 运营后台简化 Implementation Plan

> **For Claude:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task.

**Goal:** 让群、群友、好友、消息各只有一个入口，群页面默认进入插件启停与配置，非开发人员看不到技术字段。

**Architecture:** 复用现有 Chatroom / ChatroomMember / WorkWechatContact / Conversation 模型和 IdentityDirectory；群页面沿用 /channels/{id}/chatrooms/{id}，改成标签式；新增跨渠道 /chatrooms 与 /contacts 列表；删除 /conversations 全部页面。

**Tech Stack:** FastAPI、Jinja2、SQLAlchemy、MariaDB（线上）、pytest。

设计文档：docs/plans/2026-11-08-ops-console-simplification-design.md

约束（AGENTS.md）：每批写中文注释、提交说明用中文并如实写已执行的测试；被替代的旧页面直接删除，不留跳转与兼容分支；只部署到 201。模板改动必须用真实上下文渲染一次再部署。

---

## 批次 1：放开微桥群级插件接口 + 群页面改标签式（价值最高，先上线）

### Task 1: 群级插件 5 个接口放开 wechat_webridge

**Files:**
- Modify: `app/main.py`（`/channels/{id}/chatrooms/{id}/scopes/{bot_plugin_id}/enable|disable|reset|config|mode`，约 3437-3700 行）
- Test: `tests/test_webridge_chatroom_scopes.py`（新建）

**Step 1:** 写失败测试：用 sqlite 建 Channel(wechat_webridge)、Chatroom、BotPlugin，POST enable 接口，期望 303 而不是 400。
**Step 2:** 运行，期望 FAIL（400 Channel is not ...）。
**Step 3:** 把五处 `channel.channel_type not in {"wechat_ipad","wechat_winauto","workwechat"}` 抽成模块常量 `GROUP_SCOPE_CHANNEL_TYPES`，加入 `wechat_webridge`，错误信息同步更新。
**Step 4:** 运行测试，期望 PASS。
**Step 5:** 提交。

### Task 2: 群页面数据按用户语言重组

**Files:**
- Create: `app/services/wechat_webridge/chatrooms.py` 中扩展 `build_webridge_chatroom_detail`
- Modify: `app/services/workwechat_chatrooms.py:880-925`（binding_items 增加 `friendly_*` 字段）
- Test: `tests/test_webridge_chatroom_detail.py`

要点：每个插件输出 `name`、`description`、`enabled`（有效启停）、`customized`（是否群内自定义）；`技术信息` 收为单独字典 `tech`（群外部 ID、同步深度、观测来源、runtime 状态、最近执行摘要）。
测试：构造一个继承与一个覆盖的插件，断言 `customized` 与 `enabled` 正确，且主字段中不含 `mode`/`effect`/`action_scope`。

### Task 3: 群页面模板改标签式

**Files:**
- Modify: `app/templates/chatroom_detail.html`（整体重写为四个标签：插件、成员、消息、资料；底部 `<details>` 技术信息）
- Modify: `app/main.py` 的 `channel_chatroom_detail_page`，增加 `tab` 参数，默认 `plugins`

要点：
- 插件标签：每个插件一行，开关用单个表单提交到现有 mode 接口（`enabled`/`inherited`），配置展开区 include `_plugin_config_form.html`；去掉 `Binding #`、`bot_plugin`、`runtime=`、`mode=`、`effect=` 字样。
- 成员标签：沿用 `identity` 宏；「同步成员」按钮。
- 消息标签：只取最近 30 条，不分页；「在消息中心打开」链接到 `/messages?conversation_id=`。
- 资料标签：群名、备注、纳管状态表单。
**验证：** 用线上 3 个群的真实数据在 201 容器里渲染 4 个标签，无异常。
**提交并部署 201。**

---

## 批次 2：群管理与好友两个跨渠道入口

### Task 4: /chatrooms 跨渠道群列表

**Files:**
- Create: `app/services/chatroom_directory.py`（跨渠道查询、搜索、渠道与纳管筛选、分页，复用 IdentityDirectory）
- Create: `app/templates/chatrooms.html`
- Modify: `app/main.py`（新路由）、`app/web/tenant_navigation.py`（新增「群管理」）
- Test: `tests/test_chatroom_directory.py`

每行：群头像、群名、渠道名、成员数、已启用插件数、纳管状态；整行链接到群页面。

### Task 5: /contacts 跨渠道好友列表与轻量详情

**Files:**
- Create: `app/services/contact_directory.py`、`app/templates/contacts.html`、`app/templates/contact_detail.html`
- Modify: `app/main.py`、`app/web/tenant_navigation.py`
- Test: `tests/test_contact_directory.py`

详情：头像、昵称、备注、所在的共同群（来自 ChatroomMember）、「查看聊天」跳转消息中心。

### Task 6: 主导航收敛为 6 项

**Files:**
- Modify: `app/web/tenant_navigation.py`（总览、群管理、好友、消息、运营能力、账号接入）
- Modify: `app/templates/base.html`（账号菜单加入开发文档、套餐、企业设置）
- Test: `tests/test_tenant_navigation.py`

---

## 批次 3：消息收口并删除重复入口

### Task 7: 删除 /conversations

**Files:**
- Modify: `app/main.py`（删除 `conversations_page`、`conversation_detail_page`）
- Delete: `app/templates/conversations.html`、`app/templates/conversation_detail.html`
- Modify: `app/web/tenant_navigation.py`（去掉「会话」「消息记录」子导航）
- Modify: `app/templates/messages.html`（去掉「查看会话管理」；群会话头部加「群设置」链接）

**验证：** `rg "/conversations" app tests` 应无残留引用；访问 /conversations 返回 404。

### Task 8: 去掉其余重复的消息列表

**Files:**
- Modify: `app/templates/wechat_webridge_dashboard.html`（删除消息标签，改为跳转链接；「群组」「群成员」标签改为指向群管理）
- Modify: `app/services/wechat_webridge/dashboard.py`（删除不再使用的消息与群查询）
- Modify: `app/templates/channel_detail.html:197`（最近消息整块改为一行摘要加跳转）
- Modify: `app/templates/dashboard.html`、`app/templates/chatroom_list.html`（群链接统一指向群页面）

### Task 9: 全局链接收口与回归

**Files:** 全仓库
- `rg "chatrooms/\{\{" app/templates` 逐一核对，所有群链接指向群页面。
- 运行：`LOG_DIR=$(mktemp -d) .venv/bin/python -m pytest tests/test_wechat_webridge_capabilities.py tests/test_channel_catalog.py tests/test_webridge_chatroom_scopes.py tests/test_chatroom_directory.py tests/test_contact_directory.py tests/test_tenant_navigation.py`
- 已知历史失败：`test_workwechat_chatrooms.py` 与 `test_wechat_winauto_chatrooms.py` 的 9 个失败（夹具缺 bots.tenant_id），本计划不处理。

---

## 部署与验证（每批一次）

沿用 201 部署流程：`.env.deploy` → fetch/ff-only 合并 → 回滚镜像打标签 → 叠层构建 → `up -d --no-build --no-deps app` → `/readyz` 200 → 日志无 Traceback → 容器内真实数据渲染页面。每批结束更新 `/data/servers/servers/192.168.2.201/server.md` 维护记录。

## 风险

- 群页面的开关沿用现有 mode 接口，行为不变；只是换了文案与布局。
- 删除 /conversations 后，若有外部书签会 404，属于预期（AGENTS.md 要求不留跳转）。
- 导航收敛会影响其他渠道（企微、iPad）的用户，批次 2 上线前需确认 /channels 仍可从「账号接入」进入。
