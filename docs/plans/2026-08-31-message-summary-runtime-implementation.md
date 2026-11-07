# Message Summary Runtime Implementation Plan

> **For Claude:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task.

**Goal:** 让群聊消息总结通过可信租户消息服务读取历史，并使用租户自备 Dify 工作流完成手工和定时总结。

**Architecture:** 平台新增消息查询服务与数据库感知目录执行入口，插件只获得标准化消息记录，不持有数据库会话。`message_summary` 固定使用群聊、日期范围、Dify workflow `data.outputs.text` 和标准回复契约，定时动作复用现有租户任务队列。

**Tech Stack:** FastAPI、SQLAlchemy、现有目录插件运行时、`PluginHttpClient`、`PluginStateStore`、统一插件 scheduler、pytest。

---

### Task 1: 增加租户安全消息读取服务

**Files:**
- Create: `app/services/plugin_runtime/messages.py`
- Modify: `app/services/plugin_runtime/__init__.py`
- Create: `tests/test_plugin_message_reader.py`

**Steps:**

1. 写消息范围、排序、文本格式化和跨租户/跨机器人/私聊拒绝测试。
2. 实现不可变消息记录、读取结果和 `PluginMessageReader`。
3. 限制最大条数，只读取文本内容，不返回 ORM 对象和原始载荷。
4. 运行 `.venv/bin/pytest -q tests/test_plugin_message_reader.py`。
5. 执行 `git diff --check`，中文提交 `能力：增加租户插件消息读取服务`。

### Task 2: 增加数据库感知消息插件执行入口

**Files:**
- Modify: `app/services/plugin_runtime/executors.py`
- Modify: `app/services/plugin_runtime/service.py`
- Modify: `app/services/plugin_runtime/__init__.py`
- Modify: `app/services/plugin_directory.py`
- Modify: `app/main.py`
- Modify: `tests/test_plugin_runtime_secrets.py`
- Modify: `tests/test_multi_bot_admin.py`

**Steps:**

1. 写绑定密钥仍只在内存合并、伪造上下文不能覆盖可信主键的入口测试。
2. 新增 `DirectoryRuntimeExecutor.execute_message(db, request)` 和 `execute_binding_message_action(...)`。
3. 让 `message_summary` 动态派发及手工执行使用数据库感知入口。
4. 删除 `build_execution_payload` 中群总结消息注入及相关旧测试断言。
5. 运行 `.venv/bin/pytest -q tests/test_plugin_runtime_secrets.py tests/test_multi_bot_admin.py -k 'message_summary or binding_message_action'`。
6. 执行 `git diff --check`，中文提交 `能力：增加数据库感知消息插件执行入口`。

### Task 3: 重构群总结插件业务主线

**Files:**
- Modify: `plugins/message_summary/plugin.py`
- Modify: `plugins/message_summary/config.toml`
- Modify: `tests/test_existing_ai_content_plugins.py`
- Modify: `tests/test_multi_bot_admin.py`

**Steps:**

1. 写 `总结/summary`、指定日期、默认昨天、最低 100 条和仅群聊测试。
2. 删除 `日报`、固定地址、`SessionLocal`、原生 HTTP、聊天应用模式和技术字段。
3. 使用 `PluginMessageReader`、`PluginHttpClient` 和标准文本/图片回复。
4. 固定请求 Dify workflow，响应只读取 `data.outputs.text`；结果不返回消息正文或完整响应。
5. 运行 `.venv/bin/pytest -q tests/test_existing_ai_content_plugins.py -k message_summary tests/test_plugin_message_reader.py`。
6. 执行 `git diff --check`，中文提交 `插件：重构群聊消息总结主线`。

### Task 4: 接入统一群总结定时任务

**Files:**
- Modify: `app/services/plugin_runtime/scheduler.py`
- Modify: `plugins/message_summary/plugin.py`
- Modify: `tests/test_plugin_scheduler.py`
- Modify: `tests/test_existing_ai_content_plugins.py`

**Steps:**

1. 写群总结调度扫描、脱敏载荷、预生成和推送测试。
2. 将 `message_summary` 加入统一扫描范围，按明确配置的群会话生成两个动作。
3. 实现 `execute_scheduled_action`，在任务执行时重新读取租户消息和密钥。
4. 使用 `PluginStateStore` 保存短期摘要结果；任务和结果不保存消息正文或密钥。
5. 运行 `.venv/bin/pytest -q tests/test_plugin_scheduler.py -k message_summary tests/test_existing_ai_content_plugins.py -k message_summary`。
6. 执行 `git diff --check`，中文提交 `插件：接入群总结统一定时任务`。

### Task 5: 更新迁移台账并执行组合轻量验证

**Files:**
- Modify: `docs/plugin-migration-ledger.md`

**Steps:**

1. 记录 A-BOT 基线、SaaS 提交、成本归属和实际验证命令。
2. 运行 `.venv/bin/pytest -q tests/test_existing_ai_content_plugins.py tests/test_plugin_runtime_secrets.py tests/test_plugin_message_reader.py tests/test_plugin_scheduler.py`。
3. 运行 `git diff --check` 和插件目录配置解析检查。
4. 中文提交 `文档：记录群总结插件迁移结果`。

