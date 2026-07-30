# Robot Lifecycle Simplification Implementation Plan

> **For Claude:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task.

**Goal:** 删除机器人分组、模板、标签、slug、环境、自定义命令前缀和复制流程，把机器人创建简化为只填写名称。

**Architecture:** 先用契约测试锁定精简后的模型、表单和路由，再从 SQLAlchemy 模型向服务层、Web 路由和 Jinja2 页面逐层删除旧概念。机器人继续作为渠道、插件、会话和消息的父对象，插件命令前缀固定为 `/`，统计与监控直接覆盖当前租户全部数据。

**Tech Stack:** Python 3.11、FastAPI、SQLAlchemy 2.x、Pydantic、Jinja2、Pytest、SQLite 测试数据库。

**Design:** `docs/plans/2026-07-30-simplify-bot-lifecycle-design.md`

---

## 执行规则

- 所有实现只修改 `/data/multi-bot-saas`，不改动 `/data/multi-bot-admin` 的用户工作区。
- 每个复杂删除点补充详细中文注释，解释保留的兼容边界或固定默认值。
- 测试进程必须在收集测试前设置独立 SQLite 数据库，禁止连接开发或生产数据库。
- 每个任务完成后运行 `git diff --check`，并使用中文提交信息提交。
- 遇到测试失败或异常行为时先使用 `@systematic-debugging` 定位根因。

### Task 1: 锁定精简后的机器人契约

**Files:**
- Create: `tests/test_simplified_bot_lifecycle.py`
- Modify: `app/schemas/forms.py`
- Modify: `app/models/entities.py`
- Modify: `app/models/__init__.py`

**Step 1: 编写失败测试**

测试必须在导入应用前设置 `APP_ENV=test`、`DATA_DIR`、`DATABASE_URL` 和 `LOG_DIR`，并断言：

```python
DELETED_TABLES = {"bot_groups", "config_templates", "bot_tags", "bot_tag_links"}
DELETED_BOT_FIELDS = {"slug", "command_prefix", "group_id", "config_template_id", "environment"}


def test_removed_bot_metadata_is_absent_from_database_contract() -> None:
    assert DELETED_TABLES.isdisjoint(Base.metadata.tables)
    assert DELETED_BOT_FIELDS.isdisjoint(Bot.__table__.columns.keys())


def test_bot_forms_only_expose_business_fields() -> None:
    assert set(BotCreate.model_fields) == {"name"}
    assert set(BotUpdate.model_fields) == {"name", "description"}
```

同时断言 `MessageActivityHourly` 和 `PluginActivityHourly` 不再包含 `environment`。

**Step 2: 运行测试确认失败**

Run:

```bash
APP_ENV=test DATA_DIR=/data/multi-bot-saas/.run/test-runtime/simplified-contract DATABASE_URL=sqlite:////data/multi-bot-saas/.run/test-runtime/simplified-contract/test.db LOG_DIR=/data/multi-bot-saas/.run/test-runtime/simplified-contract/logs AUTH_SEED_DEMO_USERS=false .venv/bin/python -m pytest tests/test_simplified_bot_lifecycle.py -q
```

Expected: 因旧表、旧字段和旧表单字段仍存在而失败。

**Step 3: 删除模型和表单字段**

- `Bot` 只保留 `id`、`name`、`description`、`status`、`is_enabled` 和现有业务关系；
- 删除 `BotGroup`、`ConfigTemplate`、`BotTag`、`BotTagLink`；
- 删除两张小时统计表的 `environment` 字段；
- `BotCreate` 只接收 `name`；
- `BotUpdate` 只接收 `name` 和可选 `description`；
- 清理模型导出。

**Step 4: 运行目标测试**

Expected: 模型和表单契约测试通过；应用导入暂时可能因服务层旧引用失败，继续完成 Task 2 后再次运行。

**Step 5: 提交**

```bash
git add app/models/entities.py app/models/__init__.py app/schemas/forms.py tests/test_simplified_bot_lifecycle.py
git commit -m "模型：删除机器人复杂元数据结构"
```

### Task 2: 精简服务层和统计逻辑

**Files:**
- Modify: `app/services/crud.py`
- Modify: `app/services/ops.py`
- Modify: `app/services/analytics.py`
- Modify: `app/services/plugin_directory.py`
- Modify: `app/services/runtime.py`
- Test: `tests/test_simplified_bot_lifecycle.py`

**Step 1: 增加失败测试**

使用临时 SQLite Session 验证：

```python
def test_create_and_update_bot_use_minimal_contract(db_session) -> None:
    bot = create_bot(db_session, BotCreate(name="客服机器人"))
    assert bot.name == "客服机器人"
    assert bot.description is None
    assert bot.is_enabled is True
    assert bot.status == "running"

    update_bot(db_session, bot, BotUpdate(name="售后机器人", description="处理售后咨询"))
    assert bot.name == "售后机器人"
    assert bot.description == "处理售后咨询"
```

再断言插件静态上下文始终使用 `/`，不访问 `channel.bot.command_prefix`。

**Step 2: 运行测试确认失败**

Expected: 旧 CRUD、复制和环境逻辑仍依赖已删除字段。

**Step 3: 实现最小服务逻辑**

- `create_bot` 固定创建为启用、`running`；
- `update_bot` 只更新名称和说明，不隐式改变启停状态；
- 删除标签同步函数；
- 删除 `clone_bot`、环境规范化、环境汇总和按环境解析关联对象；
- `build_monitoring_snapshot` 不再接收环境参数；
- 小时统计按时间、机器人、渠道和业务维度聚合，不再按环境拆分；
- 插件调度上下文写死 `command_prefix="/"`，并用中文注释说明 SaaS 只保留统一命令语法；
- 删除运行时为 `bots.environment` 补列的兼容逻辑。

**Step 4: 运行服务层和插件相关测试**

```bash
.venv/bin/python -m pytest tests/test_simplified_bot_lifecycle.py tests/test_wechat_winauto_chatrooms.py -q
```

Expected: 新增测试和插件命令相关测试通过。

**Step 5: 提交**

```bash
git add app/services/crud.py app/services/ops.py app/services/analytics.py app/services/plugin_directory.py app/services/runtime.py tests/test_simplified_bot_lifecycle.py
git commit -m "服务：精简机器人创建与统计逻辑"
```

### Task 3: 删除旧 Web 路由并简化创建接口

**Files:**
- Modify: `app/main.py`
- Test: `tests/test_simplified_bot_lifecycle.py`
- Modify: `tests/test_multi_bot_admin.py`

**Step 1: 增加失败测试**

断言应用路由不再包含：

```python
DELETED_PATHS = {
    "/groups",
    "/groups/{group_id}",
    "/groups/{group_id}/edit",
    "/templates",
    "/templates/{template_id}",
    "/templates/{template_id}/edit",
    "/bots/{bot_id}/clone",
}


def test_removed_business_routes_are_not_registered() -> None:
    assert DELETED_PATHS.isdisjoint({route.path for route in app.routes})
```

通过管理员登录后提交 `POST /bots`，只发送 `name`，断言返回机器人详情地址；重复名称应重定向回列表并显示中文错误。

**Step 2: 运行测试确认失败**

Expected: 旧路由仍注册，创建接口仍要求 slug。

**Step 3: 精简 `app/main.py`**

- 删除旧模型、环境和复制服务导入；
- 删除机器人元数据加载函数、环境标签和环境链接拼装；
- 列表按机器人 ID 倒序加载，并计算渠道和插件数量；
- 创建接口只接收 `name`，去除空白后校验，默认启用；
- 创建成功跳转 `/bots/{id}?notice=机器人已创建，请继续添加企业微信账号`；
- 编辑接口只接收 `name` 和 `description`；
- 捕获名称唯一约束冲突，回滚事务并返回明确中文提示；
- 删除全部分组、模板和复制路由；
- Dashboard、监控、排行、趋势和页面链接全部取消环境参数和环境过滤；
- 删除状态检查审计中的环境字段。

**Step 4: 更新旧测试并运行 Web 回归**

删除 `test_group_template_and_bot_metadata_flow`，改为最小创建、编辑和列表流程；删除跨环境复制断言，保留同一测试中的日志和监控断言。

```bash
.venv/bin/python -m pytest tests/test_simplified_bot_lifecycle.py tests/test_multi_bot_admin.py -q
```

Expected: Web 契约通过；历史测试不再引用已删除字段。

**Step 5: 提交**

```bash
git add app/main.py tests/test_simplified_bot_lifecycle.py tests/test_multi_bot_admin.py
git commit -m "接口：删除机器人旧业务路由"
```

### Task 4: 重做机器人页面和总览展示

**Files:**
- Modify: `app/templates/bots.html`
- Modify: `app/templates/bot_detail.html`
- Modify: `app/templates/dashboard.html`
- Modify: `app/templates/plugins.html`
- Modify: `app/templates/base.html`
- Delete: `app/templates/groups.html`
- Delete: `app/templates/group_detail.html`
- Delete: `app/templates/templates.html`
- Delete: `app/templates/template_detail.html`
- Test: `tests/test_simplified_bot_lifecycle.py`

**Step 1: 增加失败测试**

读取 Jinja2 模板源码并断言：

- 创建表单只包含 `name="name"`；
- 不包含 `group_id`、`config_template_id`、`tags`、`slug`、`environment`、`command_prefix`；
- 详情页不包含复制按钮和复制表单；
- Dashboard 不包含“分组 / 环境”和“模板 / 标签”；
- 四个旧模板文件不存在。

**Step 2: 运行测试确认失败**

Expected: 旧表单、旧列和旧模板仍存在。

**Step 3: 实现精简页面**

- 机器人列表只保留名称、状态、企业微信账号数、插件数、启用状态和操作；
- 创建弹窗只保留名称输入和提交/取消按钮；
- 详情摘要只展示状态、企业微信账号数、插件数和会话数；
- 编辑弹窗只保留名称和说明；
- 删除复制弹窗；
- Dashboard 机器人矩阵直接展示名称、状态、渠道、插件、会话和消息；
- 插件概览删除环境文案；
- 机器人模块二级导航只保留机器人入口；
- 删除四个旧模板文件。

**Step 4: 运行模板和页面测试**

```bash
.venv/bin/python -m pytest tests/test_simplified_bot_lifecycle.py tests/test_multi_bot_admin.py -q
```

Expected: 页面测试通过，HTML 不再出现被删除业务概念。

**Step 5: 提交**

```bash
git add app/templates app/main.py tests/test_simplified_bot_lifecycle.py tests/test_multi_bot_admin.py
git commit -m "界面：简化机器人创建和维护流程"
```

### Task 5: 清理残余引用并完成回归

**Files:**
- Modify: `README.md`
- Modify: `docs/plans/2026-07-30-multi-bot-saas-implementation.md`
- Modify: any remaining file reported by `rg`

**Step 1: 搜索残余引用**

```bash
rg -n "BotGroup|ConfigTemplate|BotTag|BotTagLink|bot_groups|config_templates|bot_tags|bot_tag_links|config_template_id|command_prefix|clone_bot|KNOWN_ENVIRONMENTS" app tests README.md
rg -n "bot\.slug|Bot\.environment|bot\.environment|name=\"(group_id|config_template_id|tags|slug|environment|command_prefix)\"" app tests
```

Expected: 只允许企业微信群聊、积分等独立领域继续使用它们自己的 `group_id`；机器人旧概念无残留。

**Step 2: 更新文档和主 SaaS 计划**

把后续 Alembic、多租户模型和测试清单中的四个废弃模型、机器人环境、复制流程删除，确保后续任务不会重新引入已删除能力。

**Step 3: 运行目标回归**

```bash
APP_ENV=test DATA_DIR=/data/multi-bot-saas/.run/test-runtime/final DATABASE_URL=sqlite:////data/multi-bot-saas/.run/test-runtime/final/test.db LOG_DIR=/data/multi-bot-saas/.run/test-runtime/final/logs AUTH_SEED_DEMO_USERS=true .venv/bin/python -m pytest tests/test_simplified_bot_lifecycle.py tests/test_saas_branding.py tests/test_workwechat_provider.py tests/test_workwechat_cloud_poll.py tests/test_workwechat_contacts.py tests/test_workwechat_chatrooms.py tests/test_wechat_winauto_provider.py tests/test_wechat_winauto_chatrooms.py -q
```

Expected: 企业微信核心回归全部通过。

**Step 4: 运行完整可用测试集和静态检查**

```bash
APP_ENV=test DATA_DIR=/data/multi-bot-saas/.run/test-runtime/full DATABASE_URL=sqlite:////data/multi-bot-saas/.run/test-runtime/full/test.db LOG_DIR=/data/multi-bot-saas/.run/test-runtime/full/logs AUTH_SEED_DEMO_USERS=true .venv/bin/python -m pytest -q
git diff --check
git status --short
```

Expected: 新增回归通过；若完整旧套件仍有基线已知失败，逐项确认与本次改动无关并记录。

**Step 5: 提交**

```bash
git add README.md docs app tests
git commit -m "文档：同步机器人精简后的SaaS实施边界"
```

### Task 6: 继续 SaaS 主计划

完成本计划后，从原实施计划的 Alembic 基线任务继续。生成基线时必须以精简后的 `Base.metadata` 为准，禁止重新创建已删除的表和字段。
