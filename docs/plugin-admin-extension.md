# 插件后台扩展入口设计

> 2026-06-09 更新：`supplier_order_flow` 已经迁出 Fulcrum，由独立 `supplier-order-admin` 系统承载。Fulcrum 当前只保留插件目录发现、消息网关、Open API 和 Webhook 能力，不再在本项目内维护 `app/services/supplier_order_flow`、`app/templates/supplier_order_flow` 或 `/plugin-admin/supplier_order_flow` 这类收发单业务实现。

## 背景

当前插件管理能力主要覆盖三类场景：

- 插件目录发现：通过 `plugins/<code>/config.toml` 注册插件元数据。
- 普通配置：通过 `runtime` 默认值生成 schema 表单，或者直接编辑 `config_json`。
- 运行控制：在插件详情和运行状态页执行启停、重载、手动执行。

这套机制适合轻量插件，例如天气、消息总结、签到、简单 AI 回复。但群运营看板、积分商城这类插件可能具备业务子系统特征，仅靠 JSON 配置和运行摘要无法表达业务状态，也无法支撑进度追踪、异常处理、人工确认和审计。

因此需要增加一个通用的插件后台扩展入口，让复杂插件可以声明自己的管理后台，而不是每个插件都在主导航或主应用里硬编码一套路由和页面。

## 目标

插件后台扩展入口用于解决以下问题：

1. 在插件详情页、插件运行状态页展示“进入业务后台”入口。
2. 让复杂插件声明自己的后台导航、页面标题和能力说明。
3. 为插件业务页面提供统一权限、统一布局和统一返回路径。
4. 支持后续多个复杂插件复用同一套后台入口机制。
5. 保持插件业务逻辑和 Fulcrum 核心插件调度能力边界清晰。

## 非目标

1. 不允许插件任意注册 FastAPI 路由。
2. 不把复杂业务状态继续塞进 `BotPlugin.config_json`。
3. 不要求所有插件都实现独立后台。
4. 不在第一阶段实现动态前端组件加载。
5. 不把插件后台扩展设计成独立应用市场能力。

## 推荐架构

### 核心模块

```text
app/services/plugin_admin_registry.py
app/api/plugin_admin.py
app/templates/plugin_admin_shell.html
```

职责划分：

- `plugin_admin_registry.py`：读取插件后台声明，返回插件后台扩展定义。
- `plugin_admin.py`：提供统一后台入口路由和权限控制。
- `plugin_admin_shell.html`：提供统一页面壳、面包屑、插件导航和返回插件详情的入口。

复杂插件自己的业务页面仍然由项目内显式业务模块提供，例如：

```text
app/services/<business_plugin_code>/
app/templates/<business_plugin_code>/
```

插件目录只负责声明和消息入口：

```text
plugins/<business_plugin_code>/
  README.md
  config.toml
  plugin.py
```

## 插件声明契约

### config.toml 声明

插件可以在 `config.toml` 里增加 `[admin]` 区块：

```toml
[admin]
enabled = true
title = "业务工作台"
base_path = "/plugin-admin/<business_plugin_code>"
entry = "dashboard"
nav_items = ["dashboard", "settings"]
```

该声明只描述后台入口，不直接注册业务路由。

### plugin.py 声明

需要更强表达能力时，插件可以提供函数：

```python
def get_admin_extension():
    return {
        "enabled": True,
        "title": "业务工作台",
        "entry": "dashboard",
        "nav_items": [
            {"key": "dashboard", "label": "总览"},
            {"key": "settings", "label": "系统配置"},
        ],
    }
```

第一阶段优先支持 `config.toml`，后续再支持 `plugin.py` 动态声明。

## 路由设计

统一入口：

```text
GET /plugin-admin/{plugin_code}
GET /plugin-admin/{plugin_code}/{section}
```

行为：

1. 根据 `plugin_code` 查询插件是否存在。
2. 查询插件是否声明 `[admin] enabled = true`。
3. 校验当前用户是否有后台访问权限。
4. 根据 `section` 渲染对应业务页面。
5. 如果插件未声明后台，返回 404 或跳回插件详情页并给出提示。

第一阶段不提供通用写接口。具体业务写操作仍由业务模块暴露显式 POST 路由，例如：

```text
POST /plugin-admin/<business_plugin_code>/settings
POST /plugin-admin/<business_plugin_code>/actions/{action_id}/retry
```

## 页面入口

需要在以下位置展示后台入口：

- 插件详情页 `/plugins/{plugin_id}`。
- 插件运行状态页 `/plugins/runtime`。
- 插件列表页 `/plugins` 可选展示。

入口展示条件：

1. 插件声明了后台扩展。
2. 后台扩展处于启用状态。
3. 当前用户有访问权限。

按钮文案建议：

```text
进入业务后台
```

## 权限和安全

第一阶段沿用现有后台登录态，不新增独立权限模型。

后续可以扩展：

- 插件后台只读权限。
- 插件后台操作权限。
- 高风险动作二次确认。
- 操作审计日志。

插件后台扩展不得绕过现有登录校验，不允许直接暴露匿名接口。

## 与普通插件配置的关系

插件配置分三档：

### 普通配置

适合 API Key、开关、命令词、阈值等简单字段。

实现位置：

- `config.toml [runtime]`
- `BotPlugin.config_json`
- schema 表单

### 强配置

适合结构化配置，例如字段映射、模板选择、解析规则。

实现位置：

- 插件后台扩展页面。
- 独立业务表。
- 明确的增删改查接口。

### 业务工作台

适合批次、进度、异常、人工确认、审计、重试。

实现位置：

- 插件后台扩展页面。
- 独立业务 service。
- 独立业务表。

## 实施阶段

### 阶段一：设计和入口元数据

- 扩展插件发现逻辑，识别 `[admin]` 区块。
- 在 runtime meta 中返回后台扩展信息。
- 在插件详情页展示“进入业务后台”按钮。

### 阶段二：统一后台壳

- 增加 `/plugin-admin/{plugin_code}` 路由。
- 增加统一后台 shell 模板。
- 支持根据插件声明展示栏目导航。

### 阶段三：业务后台接入样例

- 增加一个业务插件后台页面。
- 增加业务所需的配置、记录和异常处理页面。
- 增加业务 service 和数据表。

### 阶段四：通用化和审计

- 增加插件后台操作审计。
- 增加插件后台权限细分。
- 增加后台扩展健康检查和运行摘要。

## 判断标准

该能力完成后，一个复杂插件应能做到：

1. 在插件详情页出现明确的业务后台入口。
2. 业务后台页面使用统一布局和登录权限。
3. 插件自己的复杂配置不再依赖手写 JSON。
4. 插件业务进度可以被查看、筛选和处理。
5. 后续复杂插件可以复用相同入口机制。
