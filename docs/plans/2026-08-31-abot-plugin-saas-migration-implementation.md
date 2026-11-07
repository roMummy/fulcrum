# A-BOT Plugin SaaS Native Migration Implementation Plan

> **For Claude:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task.

**Goal:** 把 A-BOT 最新业务插件按多租户、通用渠道和租户自备凭证模式迁移到 Multi Bot SaaS，并完成现有插件的逐项审核。

**Architecture:** 保留目录插件和 `BotPlugin` 租户绑定主线，新增独立敏感配置密文、可信插件运行上下文、统一外部 HTTP/状态/调度能力。插件不得导入 A-BOT 运行时、数据库、Redis 管理器或微信客户端，所有回复通过现有标准出站契约发送。

**Tech Stack:** FastAPI、SQLAlchemy、Alembic、Jinja2、现有目录插件运行时、现有 `TaskJob` Worker、Python `cryptography` AES-GCM、pytest/unittest 定向测试。

---

## 执行约束

- 只在 `feature/ipad-874-adapter` 当前功能分支继续实施，不提交 `.rcodex/`。
- 每个基础能力或插件完成后立即使用中文 commit，不把多个未完成插件堆在一个提交中。
- 插件代码和复杂业务分支必须写详细中文注释。
- 不增加 A-BOT 兼容入口、旧类适配器、双配置读取、别名路由或隐式公共密钥降级。
- 每次提交只执行该提交直接相关的轻量测试；所有插件完成后再做组合验证。
- `/data/abot` 只作为只读业务来源，实施期间再次 `git fetch` 后若源提交变化，先更新迁移台账再继续。

### Task 1: 建立插件迁移台账与目录契约测试

**Files:**
- Create: `docs/plugin-migration-ledger.md`
- Create: `tests/test_plugin_migration_catalog.py`
- Modify: `README.md`

**Step 1: 写目录契约测试**

测试固定三组插件代码：21 个已有审核项、15 个新增迁移项、6 个明确排除项。断言新增和已有插件最终都存在 `config.toml` 与 `plugin.py`，排除插件和 4 个单机管理插件不出现在 SaaS 插件目录。

```python
def test_confirmed_plugin_migration_scope():
    assert TARGET_EXISTING | TARGET_NEW == discover_expected_plugin_codes()
    assert not (EXCLUDED_CONTENT | PLATFORM_NATIVE) & discover_expected_plugin_codes()
```

**Step 2: 运行测试并确认当前失败**

Run: `.venv/bin/pytest -q tests/test_plugin_migration_catalog.py`

Expected: FAIL，提示 15 个新增插件目录尚不存在。

**Step 3: 建立台账**

台账记录 A-BOT 源提交、插件代码、当前 SaaS 状态、外部成本归属、敏感字段、数据依赖、调度动作、迁移提交和验证命令。README 只增加台账入口，不把研发说明展示到业务 UI。

**Step 4: 提交**

```bash
git add docs/plugin-migration-ledger.md tests/test_plugin_migration_catalog.py README.md
git commit -m "计划：建立A-BOT插件迁移台账"
```

### Task 2: 增加插件敏感配置 AES-GCM 密文

**Files:**
- Modify: `requirements.txt`
- Modify: `app/core/settings.py`
- Modify: `app/models/entities.py`
- Create: `app/services/plugin_secrets.py`
- Create: `migrations/versions/20260831_0011_plugin_secret_config.py`
- Create: `tests/test_plugin_secrets.py`

**Step 1: 写失败测试**

覆盖同一绑定可加解密、不同租户或绑定不能交换密文、错误主密钥不能返回明文、密文不包含原值、缺少主密钥时正式环境拒绝保存敏感配置。

```python
def test_plugin_secret_cipher_is_bound_to_tenant_and_binding(monkeypatch):
    cipher = encrypt_plugin_secret_config(tenant_id=1, binding_id=10, config={"api_key": "secret"})
    assert decrypt_plugin_secret_config(tenant_id=1, binding_id=10, encrypted=cipher) == {"api_key": "secret"}
    with pytest.raises(PluginSecretError):
        decrypt_plugin_secret_config(tenant_id=2, binding_id=10, encrypted=cipher)
```

**Step 2: 添加依赖与设置**

在 `requirements.txt` 增加固定主版本范围的 `cryptography`。在 `Settings` 增加 `plugin_secret_key`，读取 `ABOT_PLUGIN_SECRET_KEY`，不使用 `secret_key` 或固定默认值兜底。

**Step 3: 增加模型和迁移**

给 `BotPlugin` 增加 `secret_config_encrypted: Text | None` 和 `secret_config_version: String(20) | None`。迁移只增加新列，不复制 `config_json` 中的历史敏感值。

**Step 4: 实现密文封装**

使用 AES-GCM、随机 12 字节 nonce、版本前缀和 `tenant_id:binding_id:version` AAD。模块只接受字典并输出版本化密文，不提供旧算法、明文或空密钥兼容读取。

**Step 5: 运行测试**

Run: `.venv/bin/pytest -q tests/test_plugin_secrets.py tests/test_alembic_lifecycle.py`

Expected: PASS。

**Step 6: 提交**

```bash
git add requirements.txt app/core/settings.py app/models/entities.py app/services/plugin_secrets.py migrations/versions/20260831_0011_plugin_secret_config.py tests/test_plugin_secrets.py
git commit -m "安全：增加租户插件敏感配置密文"
```

### Task 3: 分离插件普通配置与敏感配置表单

**Files:**
- Modify: `app/services/plugin_runtime/registry.py`
- Modify: `app/services/plugin_schemas.py`
- Modify: `app/services/crud.py`
- Modify: `app/main.py`
- Modify: `app/templates/_plugin_config_form.html`
- Create: `tests/test_plugin_secret_forms.py`

**Step 1: 写失败测试**

断言敏感字段不进入 `config_json`、HTML 不回显密钥、空提交保留现有密文、勾选清除才删除密钥、新绑定缺少敏感字段时保持未配置。

**Step 2: 扩展 schema**

目录配置通过 `[secrets] fields = [...]` 显式声明敏感字段。字段名关键词只用于清单静态检查，不再决定落库位置。

**Step 3: 重构保存结果**

把 `merge_schema_config` 改成返回普通配置、敏感配置变更和清除字段的结构化结果。`bind_plugin` 与 `update_binding_config` 在可信绑定主键确定后调用插件密钥服务。

**Step 4: 收敛页面**

敏感输入只显示“已配置/未配置”，不显示掩码片段和 JSON 预览。增加独立清除复选框；页面不出现密文、原文或内部字段名说明。

**Step 5: 运行测试**

Run: `.venv/bin/pytest -q tests/test_plugin_secret_forms.py tests/test_swiss_ui.py -k plugin`

Expected: PASS。

**Step 6: 提交**

```bash
git add app/services/plugin_runtime/registry.py app/services/plugin_schemas.py app/services/crud.py app/main.py app/templates/_plugin_config_form.html tests/test_plugin_secret_forms.py
git commit -m "安全：分离插件普通配置与敏感配置"
```

### Task 4: 清除共享凭证并只在运行时合并密钥

**Files:**
- Modify: `plugins/ai_auto_response/config.toml`
- Modify: `plugins/dify/config.toml`
- Modify: `plugins/douyin_parser/config.toml`
- Modify: `plugins/global_news/config.toml`
- Modify: `plugins/message_summary/config.toml`
- Modify: `plugins/weather/config.toml`
- Modify: `app/services/plugin_runtime/contracts.py`
- Modify: `app/services/plugin_runtime/executors.py`
- Modify: `app/services/plugin_runtime/service.py`
- Modify: `app/services/plugin_directory.py`
- Create: `scripts/scrub_plugin_plaintext_secrets.py`
- Create: `tests/test_plugin_runtime_secrets.py`

**Step 1: 写失败测试**

扫描所有 `plugins/*/config.toml`，断言敏感字段默认值为空。构造绑定密文后断言插件执行请求能在内存中获得密钥，但日志、任务载荷、执行 debug 和普通配置中都不出现密钥。

**Step 2: 删除内置值**

删除 6 个已发现插件中的可用共享凭证，补 `[secrets]` 声明。不得把原值移动到环境样例、测试夹具或迁移脚本。

**Step 3: 增加可信运行上下文**

给 `PluginExecutionRequest` 增加不可由插件载荷构造的 `PluginExecutionContext`，至少包含 tenant、binding、bot、channel、conversation、message、sender 和聊天类型。

**Step 4: 运行时合并**

执行器根据可信绑定解密敏感配置，和普通配置仅在函数调用内合并。执行完成后不把合并配置放入结果、缓存或 debug。

**Step 5: 增加一次性清理脚本**

脚本在租户会话中枚举绑定，按照显式 secrets 声明删除 `config_json` 的敏感字段，并清空无法确认归属的历史插件密文。脚本输出数量，不输出字段值。

**Step 6: 运行测试**

Run: `.venv/bin/pytest -q tests/test_plugin_runtime_secrets.py tests/test_simplified_bot_lifecycle.py`

Expected: PASS。

**Step 7: 提交**

```bash
git add plugins app/services/plugin_runtime app/services/plugin_directory.py scripts/scrub_plugin_plaintext_secrets.py tests/test_plugin_runtime_secrets.py
git commit -m "安全：删除插件共享凭证和明文运行链路"
```

### Task 5: 建立插件通用 HTTP、状态、限流和回复能力

**Files:**
- Modify: `app/models/entities.py`
- Create: `app/services/plugin_runtime/http_client.py`
- Create: `app/services/plugin_runtime/state_store.py`
- Create: `app/services/plugin_runtime/rate_limit.py`
- Create: `app/services/plugin_runtime/replies.py`
- Create: `migrations/versions/20260831_0012_plugin_state_records.py`
- Create: `tests/test_plugin_common_services.py`

**Step 1: 写失败测试**

覆盖外部 HTTP 超时、响应体上限、重定向上限、敏感请求头脱敏；状态记录租户隔离与过期；限流按租户、绑定、用户和群分桶；回复只允许标准消息类型。

**Step 2: 增加状态表**

新增 `PluginStateRecord`，字段包含 tenant、bot_plugin、namespace、state_key、payload_json、expires_at，并建立租户内唯一约束。该表只承载小型插件状态，不替代修仙等事务型业务表。

**Step 3: 实现 HTTP 客户端**

使用标准库网络能力和统一响应对象，默认连接/总超时、最大响应体、有限重定向；只对明确幂等的 GET 和查询请求重试。

**Step 4: 实现回复构造器**

提供 text/image/file/video 标准结构，并校验 URL、文件归属和最大数量。插件不得自行调用渠道客户端。

**Step 5: 运行测试并提交**

Run: `.venv/bin/pytest -q tests/test_plugin_common_services.py tests/test_tenant_db_scope.py`

```bash
git add app/models/entities.py app/services/plugin_runtime migrations/versions/20260831_0012_plugin_state_records.py tests/test_plugin_common_services.py
git commit -m "能力：增加插件通用网络状态与回复服务"
```

### Task 6: 统一插件定时任务并删除插件专用轮询器

**Files:**
- Modify: `app/services/task_queue.py`
- Modify: `app/services/async_workers.py`
- Modify: `app/services/plugin_runtime/contracts.py`
- Create: `app/services/plugin_runtime/scheduler.py`
- Create: `tests/test_plugin_scheduler.py`

**Step 1: 写失败测试**

断言 `plugin_scheduled_action` 任务从任务表租户归属建立执行上下文，不读取 payload 中的 `tenant_id`；相同绑定、动作和计划时间只创建一个任务；高副作用动作失败后不自动重试。

**Step 2: 增加任务类型和处理器**

任务 payload 只保存 `binding_id`、`action_key`、目标会话和非敏感参数。Worker 在租户会话中重新加载绑定和密钥后调用插件动作。

**Step 3: 删除专用轮询**

删除 `async_workers.py` 中 `douyu`、`daily_news` 的进程内时间字典和 `SessionLocal()` 全局扫描，把现有定时插件接入统一 scheduler。

**Step 4: 运行测试并提交**

Run: `.venv/bin/pytest -q tests/test_plugin_scheduler.py tests/test_tenant_security_matrix.py`

```bash
git add app/services/task_queue.py app/services/async_workers.py app/services/plugin_runtime tests/test_plugin_scheduler.py
git commit -m "能力：统一租户插件定时任务调度"
```

### Task 7: 审核已有 AI 与内容插件

**Files:**
- Modify: `plugins/ai_auto_response/*`
- Modify: `plugins/dify/*`
- Modify: `plugins/message_summary/*`
- Modify: `plugins/daily_news/*`
- Modify: `plugins/global_news/*`
- Create: `tests/test_existing_ai_content_plugins.py`
- Modify: `docs/plugin-migration-ledger.md`

**Steps:**

1. 逐个对照 A-BOT 当前命令、触发条件、Dify `text` 输出契约、总结图片、日报调度和新闻回复。
2. 每个插件先增加该插件的失败用例，再补齐实现；不得共用平台默认 API Key。
3. 每完成一个插件分别运行：`.venv/bin/pytest -q tests/test_existing_ai_content_plugins.py -k <plugin_code>`。
4. 每个插件分别提交中文 commit，例如 `插件：更新消息总结最新业务规则`。
5. 台账记录 A-BOT 源提交、SaaS 提交和实际测试。

### Task 8: 审核已有查询与解析插件

**Files:**
- Modify: `plugins/weather/*`
- Modify: `plugins/music/*`
- Modify: `plugins/douyin_parser/*`
- Modify: `plugins/duanju_search/*`
- Modify: `plugins/douyu/*`
- Create: `tests/test_existing_query_plugins.py`
- Modify: `docs/plugin-migration-ledger.md`

**Steps:**

1. 核对天气订阅、音乐结果、抖音媒体、短剧搜索、斗鱼房间/鱼吧/高能推送和日报行为。
2. 外部请求全部改用统一 HTTP 服务；媒体返回统一 replies。
3. 斗鱼所有定时动作使用 Task 6 的 scheduler，不保留插件专用线程或轮询字典。
4. 每个插件定向测试并单独中文 commit。

### Task 9: 审核已有群与互动插件

**Files:**
- Modify: `plugins/group_auto_invite/*`
- Modify: `plugins/group_member_change/*`
- Modify: `plugins/group_virtual/*`
- Modify: `plugins/guess_song/*`
- Modify: `plugins/inactive_rank/*`
- Create: `tests/test_existing_group_plugins.py`
- Modify: `docs/plugin-migration-ledger.md`

**Steps:**

1. 核对自动拉群、成员变更、跨群、猜歌和潜水排行命令与群权限。
2. 群作用域只读取现有 `GroupPluginScope`，不复制 A-BOT `Feature` 或 `GroupBotManager`。
3. 排行和群成员查询必须限定可信 channel/chatroom。
4. 每个插件定向测试并单独中文 commit。

### Task 10: 审核已有积分与任务插件

**Files:**
- Modify: `plugins/game_task/*`
- Modify: `plugins/jd_sign_token/*`
- Modify: `plugins/lucky_pot/*`
- Modify: `plugins/message_push_task/*`
- Modify: `plugins/message_sign/*`
- Modify: `plugins/point_trade/*`
- Create: `tests/test_existing_stateful_plugins.py`
- Modify: `docs/plugin-migration-ledger.md`

**Steps:**

1. 核对游戏任务、京东令牌、幸运池、定时推送、签到补签和积分交易规则。
2. 令牌进入敏感配置或专用密文，不写普通业务 JSON。
3. 积分增减、转账、奖励和开奖使用事务与幂等业务键。
4. 每个插件定向测试并单独中文 commit。

### Task 11: 迁移 `epic_free`

**Files:**
- Create: `plugins/epic_free/config.toml`
- Create: `plugins/epic_free/plugin.py`
- Create: `tests/test_plugin_epic_free.py`
- Modify: `docs/plugin-migration-ledger.md`

实现当前免费游戏查询、地区/语言配置、只在结果变化时推送和统一定时动作。外部 GET 使用通用 HTTP 服务，缓存摘要放 `PluginStateRecord`。

Run: `.venv/bin/pytest -q tests/test_plugin_epic_free.py`

Commit: `插件：迁移Epic免费游戏推送`

### Task 12: 迁移 `github_opengraph`

**Files:**
- Create: `plugins/github_opengraph/config.toml`
- Create: `plugins/github_opengraph/plugin.py`
- Create: `tests/test_plugin_github_opengraph.py`
- Modify: `docs/plugin-migration-ledger.md`

实现 GitHub URL 自动匹配、单消息链接数量限制、OpenGraph 图片地址生成和标准图片回复。散列盐进入敏感配置，插件不下载或保存仓库内容。

Run: `.venv/bin/pytest -q tests/test_plugin_github_opengraph.py`

Commit: `插件：迁移GitHub链接预览`

### Task 13: 迁移 `robot_menu`

**Files:**
- Create: `plugins/robot_menu/config.toml`
- Create: `plugins/robot_menu/plugin.py`
- Create: `tests/test_plugin_robot_menu.py`
- Modify: `docs/plugin-migration-ledger.md`

实现“菜单/功能菜单”文本命令，从当前机器人启用的插件 manifest 和 matcher 生成租户菜单。删除 A-BOT 图片模板、自动撤回和微信 At 依赖，不展示未启用或当前群强制停用的插件。

Run: `.venv/bin/pytest -q tests/test_plugin_robot_menu.py`

Commit: `插件：迁移机器人功能菜单`

### Task 14: 迁移 `gscore_adapter`

**Files:**
- Create: `plugins/gscore_adapter/config.toml`
- Create: `plugins/gscore_adapter/plugin.py`
- Create: `tests/test_plugin_gscore_adapter.py`
- Modify: `docs/plugin-migration-ledger.md`

把 GScore 地址和鉴权配置放入租户绑定，使用统一 HTTP 服务转发受支持消息并返回标准图片或文本。禁用时不建立外部连接，不允许外部响应覆盖租户和渠道主键。

Run: `.venv/bin/pytest -q tests/test_plugin_gscore_adapter.py`

Commit: `插件：迁移GScore适配能力`

### Task 15: 迁移 `ai_gen_image`

**Files:**
- Create: `plugins/ai_gen_image/config.toml`
- Create: `plugins/ai_gen_image/plugin.py`
- Create: `tests/test_plugin_ai_gen_image.py`
- Modify: `docs/plugin-migration-ledger.md`

实现“AI绘图/绘图/画图/生成图片”命令、文生图和带附件编辑请求。API Base、模型和超时为普通配置，API Key 为租户密文；请求进入任务队列，结果通过标准图片回复发送。

Run: `.venv/bin/pytest -q tests/test_plugin_ai_gen_image.py`

Commit: `插件：迁移租户自备AI绘图`

### Task 16: 迁移 `maibot_adapter`

**Files:**
- Create: `plugins/maibot_adapter/config.toml`
- Create: `plugins/maibot_adapter/plugin.py`
- Create: `app/services/plugin_runtime/maibot_client.py`
- Create: `tests/test_plugin_maibot_adapter.py`
- Modify: `requirements.txt`
- Modify: `docs/plugin-migration-ledger.md`

实现租户自己的 MaiBot WebSocket 地址和 API Key、群/私聊采集开关、回复开关、连接恢复和消息维度映射。连接管理按绑定隔离；关闭绑定或修改配置时停止旧连接，不使用全局单例。

Run: `.venv/bin/pytest -q tests/test_plugin_maibot_adapter.py`

Commit: `插件：迁移租户MaiBot适配器`

### Task 17: 迁移 `codex_quota_allocator`

**Files:**
- Create: `plugins/codex_quota_allocator/config.toml`
- Create: `plugins/codex_quota_allocator/plugin.py`
- Create: `tests/test_plugin_codex_quota_allocator.py`
- Modify: `docs/plugin-migration-ledger.md`

迁移当前 CPA/CLIProxyAPI 和 Sub2API 的查询、调整与重置确认流程。管理密钥和管理员 API Key 必须是租户密文；所有写动作要求管理员角色、用户限流和二次确认状态，未知接口版本直接拒绝，不猜测或降级路由。

Run: `.venv/bin/pytest -q tests/test_plugin_codex_quota_allocator.py`

Commit: `插件：迁移Codex额度管理`

### Task 18: 迁移 `newapi_coupon`

**Files:**
- Create: `plugins/newapi_coupon/config.toml`
- Create: `plugins/newapi_coupon/plugin.py`
- Create: `app/models/plugin_business.py`
- Create: `migrations/versions/20260831_0013_coupon_bindings.py`
- Create: `tests/test_plugin_newapi_coupon.py`
- Modify: `docs/plugin-migration-ledger.md`

实现租户 NewAPI 地址、管理员凭证、套餐配置、邮箱绑定、领券幂等和通知。新增租户归属的券账号绑定表，数据库唯一约束防止同一上游账号或用户重复领取；不复制 Redis 绑定和全局锁。

Run: `.venv/bin/pytest -q tests/test_plugin_newapi_coupon.py`

Commit: `插件：迁移NewAPI领券业务`

### Task 19: 迁移 `daily_ranking`

**Files:**
- Create: `plugins/daily_ranking/config.toml`
- Create: `plugins/daily_ranking/plugin.py`
- Create: `tests/test_plugin_daily_ranking.py`
- Modify: `docs/plugin-migration-ledger.md`

基于 SaaS 消息、群成员和积分表计算日榜，报告图片使用租户媒体目录生成，奖励写积分事务，定时推送接统一 scheduler。不得使用 A-BOT 消息库或联系人缓存。

Run: `.venv/bin/pytest -q tests/test_plugin_daily_ranking.py`

Commit: `插件：迁移群聊每日排行`

### Task 20: 迁移 `dota2_stats`

**Files:**
- Create: `plugins/dota2_stats/config.toml`
- Create: `plugins/dota2_stats/plugin.py`
- Create: `tests/test_plugin_dota2_stats.py`
- Modify: `docs/plugin-migration-ledger.md`

实现 Steam/Dota ID 绑定、个人查询、近期比赛、群排行和定时更新。OpenDota Key 为租户密文；绑定和缓存保存在 `PluginStateRecord`，外部请求使用通用 HTTP 服务。

Run: `.venv/bin/pytest -q tests/test_plugin_dota2_stats.py`

Commit: `插件：迁移Dota2战绩查询`

### Task 21: 迁移 `member_context`

**Files:**
- Create: `plugins/member_context/config.toml`
- Create: `plugins/member_context/plugin.py`
- Create: `tests/test_plugin_member_context.py`
- Modify: `docs/plugin-migration-ledger.md`

基于租户消息和群成员数据生成成员画像、增量摘要和定时刷新状态。画像存入 `PluginStateRecord`，state key 必须包含可信 channel/chatroom/member，不接受命令正文指定其他租户资源。

Run: `.venv/bin/pytest -q tests/test_plugin_member_context.py`

Commit: `插件：迁移群成员画像`

### Task 22: 迁移 `member_roast`

**Files:**
- Create: `plugins/member_roast/config.toml`
- Create: `plugins/member_roast/plugin.py`
- Create: `tests/test_plugin_member_roast.py`
- Modify: `docs/plugin-migration-ledger.md`

实现“锐评/吐槽”指令、群成员目标解析、画像复用、样本压缩和 LLM 调用。LLM 地址、模型和密钥由租户维护；按用户和群限流，不发送完整历史消息到日志。

Run: `.venv/bin/pytest -q tests/test_plugin_member_roast.py`

Commit: `插件：迁移群成员锐评`

### Task 23: 迁移 `value_rank`

**Files:**
- Create: `plugins/value_rank/config.toml`
- Create: `plugins/value_rank/plugin.py`
- Create: `tests/test_plugin_value_rank.py`
- Modify: `docs/plugin-migration-ledger.md`

实现身价、社交热度、搭子、社交桥梁、趋势、周报和重算命令。所有统计只覆盖当前租户和群，结果缓存放 `PluginStateRecord`，图片报告使用租户媒体目录。

Run: `.venv/bin/pytest -q tests/test_plugin_value_rank.py`

Commit: `插件：迁移群成员身价排行`

### Task 24: 迁移 `fun_command_play`

**Files:**
- Create: `plugins/fun_command_play/config.toml`
- Create: `plugins/fun_command_play/plugin.py`
- Create: `app/models/plugin_business.py`
- Create: `migrations/versions/20260831_0014_fun_command_rules.py`
- Create: `tests/test_plugin_fun_command_play.py`
- Modify: `docs/plugin-migration-ledger.md`

新增租户归属的趣味命令规则表，支持文本、图片、视频和链接卡片标准回复。媒体文件必须位于租户目录或可信 URL，规则命令在同一机器人内唯一，不复制 A-BOT 全局规则表和 Redis 媒体缓存。

Run: `.venv/bin/pytest -q tests/test_plugin_fun_command_play.py`

Commit: `插件：迁移趣味命令播放`

### Task 25: 迁移 `xiuxian`

**Files:**
- Create: `plugins/xiuxian/config.toml`
- Create: `plugins/xiuxian/plugin.py`
- Create: `app/models/cultivation.py`
- Create: `app/services/cultivation.py`
- Create: `migrations/versions/20260831_0015_cultivation_game.py`
- Create: `tests/test_plugin_xiuxian.py`
- Modify: `docs/plugin-migration-ledger.md`

按 A-BOT 当前命令迁移注册、状态、闭关、出关、聚灵、签到、坊市、背包、使用、突破、劫掠、赠送、门派、历练和炼丹。玩家、背包、流水、门派和冷却全部是租户表；积分换灵石使用现有积分事务；转账、购买、赠送和突破使用数据库锁与幂等键。

Run: `.venv/bin/pytest -q tests/test_plugin_xiuxian.py`

Commit: `插件：迁移多租户修仙玩法`

### Task 26: 删除占位执行器并核对平台原生替代能力

**Files:**
- Modify: `app/services/plugin_runtime/executors.py`
- Modify: `app/services/plugin_runtime/registry.py`
- Modify: `app/services/plugin_directory.py`
- Modify: `app/templates/plugins.html`
- Modify: `app/templates/plugin_detail.html`
- Create: `tests/test_plugin_native_boundaries.py`
- Modify: `docs/plugin-migration-ledger.md`

**Steps:**

1. 删除 `PlaceholderExecutor`、`scaffolded`、`planned` 和 `stub` 运行状态。
2. 没有真实执行入口的插件标记为无效，不在 UI 显示“已迁移”。
3. 断言 `plugin_manager`、`stats_collector`、`system_updater`、`trendradar_permission` 不在插件目录。
4. 断言插件管理、统计、平台运维和群作用域页面覆盖对应能力。
5. 运行 `.venv/bin/pytest -q tests/test_plugin_native_boundaries.py tests/test_platform_operations.py tests/test_platform_analytics.py`。

Commit: `重构：删除插件迁移占位与单机管理入口`

### Task 27: 完成组合验证、文档和部署准备

**Files:**
- Modify: `README.md`
- Modify: `CHANGELOG.md`
- Modify: `docs/plugin-migration-ledger.md`
- Modify: `.env.example`
- Modify: `scripts/deploy_restart_mba.sh`
- Create: `scripts/verify_plugin_migration.py`

**Step 1: 完成目录与敏感信息检查**

Run: `.venv/bin/python scripts/verify_plugin_migration.py`

Expected: 36 个目标插件全部有效；排除插件不存在；配置目录无非空敏感默认值；没有 A-BOT 运行时导入。

**Step 2: 运行集中轻量组合验证**

Run: `.venv/bin/pytest -q tests/test_plugin_*.py tests/test_simplified_bot_lifecycle.py tests/test_tenant_db_scope.py tests/test_tenant_security_matrix.py`

Expected: PASS。此处仍不是全量历史回归，提交说明必须如实记录。

**Step 3: 更新部署脚本**

部署前检查 `ABOT_PLUGIN_SECRET_KEY`，执行 Alembic 升级和明文敏感配置清理脚本，再重启服务。缺少密钥时直接中止，不生成弱默认值。

**Step 4: 提交**

```bash
git add README.md CHANGELOG.md docs/plugin-migration-ledger.md .env.example scripts/deploy_restart_mba.sh scripts/verify_plugin_migration.py
git commit -m "文档：完成A-BOT插件迁移交付说明"
```

### Task 28: 推送并部署到 192.168.2.142

**Files:**
- No repository changes expected.

**Steps:**

1. 确认工作区只剩未跟踪 `.rcodex/`，所有研发改动均已提交。
2. 推送 `feature/ipad-874-adapter` 到 origin。
3. 通过 SSH 登录 `192.168.2.142`，进入正式部署目录执行 `git pull`。
4. 安装新增 Python 依赖，设置独立 `ABOT_PLUGIN_SECRET_KEY`，执行 `alembic upgrade head`。
5. 执行 `scripts/scrub_plugin_plaintext_secrets.py`，确认只输出处理数量。
6. 执行 `scripts/deploy_restart_mba.sh`。
7. 验证 `8280` 正式页面、健康接口、平台插件清单、租户插件绑定、一个无外部成本插件和一个租户自备凭证插件。
8. 报告部署提交、地址、实际验证范围以及需要租户重新录入凭证的插件清单。
