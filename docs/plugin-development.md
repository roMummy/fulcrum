# Fulcrum 插件开发规范

本文说明如何为 Fulcrum 编写插件：目录结构、清单 `config.toml`、入口函数、配置与密钥、平台提供的能力、安全规则和测试方法。

> **状态标记**：文中标注「已实现」的内容与当前代码一致；标注「规划中」的是已确定方向、尚未实现的能力，请不要依赖。

## 1. 插件是什么

插件是 `plugins/<code>/` 目录下的一个 Python 包，由平台扫描、加载并在收到消息或到点时调用。一个插件可以：

- 响应命令或关键词（如 `天气 北京`）；
- 观察消息但不回复（如 MaiBot 适配器）；
- 定时执行并推送（如每日订阅）。

平台负责：渠道收发、租户隔离、绑定到机器人、群级开关、配置表单、密钥加密、执行日志和超时控制。插件只写业务逻辑。

### 目录结构

```text
plugins/
  weather/
    config.toml      # 清单：身份、触发条件、配置字段声明（必需）
    plugin.py        # 入口（必需）
    service.py       # 可选：拆分的业务代码
```

- 目录名就是插件编码 `code`，必须与清单里的 `plugin.code` 一致，建议小写字母、数字、下划线。
- 以 `_` 开头的目录（如 `_shared`）是共用代码，不是插件。
- 同时存在 `config.toml` 和 `plugin.py` 才会被识别为插件。

## 2. 清单 `config.toml`

清单使用**标准 TOML**。清单是插件的唯一声明来源：触发条件、配置表单、校验都由它派生。清单有错误时，该插件被标记为「无效」，页面上能看到原因，**不影响其他插件**。

### 2.1 `[plugin]` 基本信息

```toml
[plugin]
code = "weather"            # 必须与目录名一致
display_name = "天气"
plugin_type = "rule"        # message / observer / command / rule / ai / system / scheduled / timer / cron
version = "2.0.0"
author = "your-name"
description = "天气查询与每日订阅推送"
default_enabled = false
```

### 2.2 `[capability]` 能力与调度

```toml
[capability]
channel_types = ["wechat_webridge", "telegram"]   # 适用的渠道类型
chat_types = ["direct", "group"]                  # 适用的会话类型
message_types = ["text"]                          # 适用的消息类型
priority = 90              # 数字越小越先执行，默认 100
consume_policy = "continue"
timeout_ms = 30000         # 单次执行超时，默认 10000
```

`consume_policy` 决定本插件**成功执行后**是否继续让后面的插件处理同一条消息：

| 取值 | 行为 |
|---|---|
| `continue`（默认） | 继续交给后面的插件 |
| `stop` / `consume` / `exclusive` | 成功执行后停止，后面的插件不再处理 |

### 2.3 `[matcher]` 触发条件

```toml
[matcher]
commands = ["天气", "订阅天气"]   # 命令（带命令前缀，如 /天气）
keywords = ["天气"]               # 关键词
require_mention = false          # 群里是否必须 @ 机器人
```

这三项是**默认值**。每个机器人绑定可以在配置里用同名键 `commands` / `keywords` / `require_mention` 覆盖它们。如果你想让用户在页面上改这三项，请在 `[[config.fields]]` 里用相同的 key 声明，并保证默认值与这里一致（仓库的测试会检查）。

需要更复杂的判断时，在 `plugin.py` 里实现 `matches_event(context)`（见第 3 节）。

### 2.4 `[secrets]` 密钥字段

```toml
[secrets]
fields = ["api_key"]
```

声明为密钥的字段**加密保存**，页面不回显，不会出现在普通配置里。`kind = "secret"` 的配置字段会自动按密钥处理，不必在这里重复列出。

### 2.5 `[runtime]` 默认配置

```toml
[runtime]
default_city = "北京"
timeout_seconds = 15
```

插件配置的默认值，运行时作为 `request.config` 的基础。**不要在这里写密钥**，也不要写真实地址或账号。

### 2.6 `[ai]` AI 调度声明（可选）

```toml
[ai]
routable = true        # 是否允许 AI 把自然语言改写成命令调用
risk = "low"           # 只有 low 允许 AI 直接调度
summary = "查询某个城市的天气"
examples = ["北京今天天气怎么样"]
needs_args = "城市名"
```

### 2.7 `[[config.fields]]` 配置字段声明（已实现）

声明后，平台在插件页自动生成配置表单：中文标签、说明、范围、分组、下拉选项都来自声明。**没有声明的插件仍按 `[runtime]` 默认值推断**（标签是字段名转换，没有说明），功能不受影响，但体验较差，建议声明。

```toml
[[config.fields]]
key = "timeout_seconds"      # 必需。小写字母开头，字母/数字/下划线，≤64 位，同一清单内不重复
label = "请求超时(秒)"        # 必需。页面上显示的名称
label_en = "Timeout"         # 可选。英文名（英文界面使用，规划中）
kind = "number"              # 默认 text，见下表
default = 15
min = 5                      # number 的范围，保存时超出会被钳制到边界
max = 60
hint = "调用天气接口时最长等待多久。"
group = "接口"                # 页面上的小节标题，相同 group 的字段排在一起
placeholder = "https://..."  # 输入框占位提示
required = false             # 必填标记
depends_on = "some_bool"     # 依赖另一个 bool 字段（规划中：页面上依赖项关闭时本项变淡）
```

**字段类型 `kind`**

| kind | 页面控件 | 保存值 | 说明 |
|---|---|---|---|
| `text` | 单行输入 | 字符串 | |
| `textarea` | 多行输入 | 字符串 | |
| `number` | 数字输入 | 整数或小数 | 带小数点按小数保存；超出 `min`/`max` 钳制；不是数字回到默认值 |
| `bool` | 开关 | `true`/`false` | |
| `select` | 下拉 | 字符串 | 必须提供 `options` |
| `tags` | 逗号分隔输入 | 字符串列表 | 全角逗号也可以 |
| `url` | 网址输入 | 字符串 | |
| `time` | 时间选择 | `"HH:MM"` | 格式不对回到默认值 |
| `secret` | 密码输入 | 加密保存 | 不能有 `default`；留空表示保持不变；可勾选「清除」 |

`select` 的选项可以写成字符串列表，或带显示名的表：

```toml
[[config.fields]]
key = "mode"
label = "模式"
kind = "select"
default = "fast"
options = ["fast", { value = "slow", label = "慢速" }]
```

**校验规则**（不通过则插件标记为无效，并显示具体原因）：key 格式与重复、缺少 `label`、非法 `kind`、`secret` 带默认值、`number` 的 `default` 必须是数字且在 `min`/`max` 内、`min` 不能大于 `max`、`select` 必须有 `options` 且 `default` 在其中、`bool` 的 `default` 必须是布尔值、`tags` 的 `default` 必须是字符串列表、`time` 必须是 `HH:MM`、`depends_on` 必须指向另一个 `bool` 字段。

### 2.8 完整示例

参考仓库里的 `plugins/weather/config.toml` 和 `plugins/robot_menu/config.toml`，它们已经按本规范声明了配置字段。

## 3. 入口函数 `plugin.py`

入口都是模块级函数（也可以提供 `plugin` / `PLUGIN` 对象，方法同名）。全部可选，按需实现。

| 函数 | 何时调用 | 用途 |
|---|---|---|
| `matches_event(context)` | 每条候选消息 | 自定义匹配，返回 `True/False` 或 `{"matched": bool, "reason": str, "debug": {...}}` |
| `execute_action(request)` | 后台「重载」、无状态执行 | 返回 `PluginExecutionResult`。`request.action == "reload"` 用于健康检查 |
| `execute_message_action(db, request)` | 由真实聊天消息触发 | 需要读写数据库的业务（如订阅）放这里，`db` 是当前租户的会话 |
| `execute_scheduled_action(db, request)` | 到点的定时任务 | 批量推送等 |
| `handle_event(context)` | 观察型插件 | 处理事件，不要求回复 |

`context`（`DirectoryPluginContext`）包含：`plugin_code`、`binding_id`、`bot_id`、`channel_id`、`channel_type`、`chat_type`、`message_type`、`content`、`sender_id`、`conversation_external_id`、`mentioned_self`、`command_name`、`matched_keyword`、`binding_config` 等。

`request`（`PluginExecutionRequest`）包含：`action`、`config`（默认值 + 绑定配置 + 解密后的密钥）、`payload`、`context`（`PluginExecutionContext`：`tenant_id`、`binding_id`、`bot_id`、`channel_id`、`conversation_id`、`sender_id` 等）。

### 返回值

```python
from app.services.plugin_runtime.contracts import PluginExecutionResult
from app.services.plugin_runtime.replies import text_reply

return PluginExecutionResult(
    ok=True,
    status="success",
    message="查询完成",
    output={"reply_text": "北京：晴 12℃"},   # 或 {"replies": [text_reply("...")]}
)
```

平台会把 `output` 自动转成出站消息：

- 文字：`reply_text`（也识别 `answer` / `summary` / `text` / `message`，按此顺序取第一个非空值）；
- 多条或多媒体：`replies` 列表，用 `replies.py` 里的 `text_reply`、`image_reply`、`file_reply`、`link_reply`、`video_reply` 构造；
- 其他：`outbound_messages`、各类卡片 XML 字段。

## 4. 配置与密钥

配置分三层，后者覆盖前者：

1. **插件默认**：`[runtime]` 与 `[[config.fields]]` 的 `default`；
2. **机器人绑定配置**：管理员在插件页为某个机器人保存的配置；
3. **群级覆盖**：群详情页里对单个群的覆盖（可「恢复默认」）。

约定：

- 密钥只通过 `kind = "secret"` 字段或 `[secrets]` 声明，运行时合并进 `request.config`，**永远不要写进清单默认值或日志**。
- 读取配置时对每个值做类型和范围校验（例如天气插件的 `_bounded_int`），不要假设保存的内容一定合法。
- 清单里没声明的配置键，保存时会保留原值，不会被表单丢掉。

## 5. 平台提供的能力（已实现）

通过 `app.services.plugin_runtime` 使用，**不要自己造轮子**，这些都做了租户隔离和边界检查：

| 模块 | 能力 |
|---|---|
| `replies` | 构造各类回复，校验文件归属和资源地址 |
| `http_client.PluginHttpClient` | 受限的 HTTP 调用：校验 URL、限制重定向次数和响应大小、脱敏地址、统一超时 |
| `state_store.PluginStateStore` | 当前租户、当前绑定内的小型 JSON 状态（单条 ≤ 64KB，支持过期） |
| `rate_limit.PluginRateLimiter` | 按用户/群的频率限制 |
| `text_generation` | 调用租户配置的模型生成文本 |

## 6. 安全规则

插件运行在服务进程内，**拥有服务端代码执行权限**，所以：

- **只有平台管理员能安装插件**，租户只能启用和配置。不要运行来源不明的插件。
- **身份必须来自可信上下文**（`request.context`），不要信任 `payload` 里的租户、用户、群字段。需要读写数据库的操作放进 `execute_message_action` / `execute_scheduled_action`，使用平台给的租户会话。
- **不要直接访问文件系统和网络**：文件通过 `plugins/_shared` 的授权目录边界，网络用 `PluginHttpClient`（防止访问内网地址）。
- **不要保存或打印密钥**，错误信息里也不要带出凭证。
- 单次执行受 `timeout_ms` 限制，不要在入口里做长时间阻塞。

## 7. 测试

- 测试放在 `tests/test_plugin_<code>.py`。执行入口函数时传入构造好的 `PluginExecutionRequest`，外部 HTTP 用桩替换，**不要访问真实服务**。
- 测试数据使用虚构值（微信号、群 ID、昵称都不要用真实的）。
- 运行：`scripts/run_tests_isolated.sh tests/test_plugin_<code>.py`（每个文件独立进程和测试库）。
- 仓库有一个通用检查：所有随仓库发布的清单必须有效，且声明的 `commands` / `keywords` / `require_mention` 默认值与 `[matcher]` 一致（`tests/test_plugin_manifest.py`）。
- 只在内网使用、不随仓库发布的插件目录，把目录名写进环境变量 `FULCRUM_PRIVATE_PLUGINS`（逗号分隔），目录校验会忽略它们。

## 8. 从推断表单迁移到声明字段

已有插件不改也能运行。要获得完整的配置页，按下面做：

1. 为每个 `[runtime]` 里用户需要调整的键写一个 `[[config.fields]]`，补上中文 `label`、`hint`、范围和 `group`；
2. 密钥用 `kind = "secret"`；
3. 触发方式（`commands` / `keywords` / `require_mention`）如需用户可改，同样声明，**默认值与 `[matcher]` 保持一致**；
4. 不需要用户改的内部参数不用声明，它们保持默认；
5. 运行 `tests/test_plugin_manifest.py` 确认清单有效。

## 9. 已知限制与规划

| 项 | 状态 |
|---|---|
| 声明式配置字段与自动生成的配置表单 | 已实现 |
| 标准 TOML 清单与校验、无效清单隔离 | 已实现 |
| 配置表单中 `depends_on` 的变淡效果、英文界面（`label_en`） | 规划中 |
| 把插件配置从弹窗改成与群功能一致的卡片 | 规划中 |
| 定时任务的声明（现在由平台调度器按插件编码写死） | 规划中 |
| 素材/规则集合与图片上传（自动生成的列表管理页） | 规划中 |
| 「素材回复」等免代码配方 | 规划中 |
| 在后台上传安装、卸载、回滚插件包（热插拔） | 规划中 |
| 多文件插件子模块的热重载 | 规划中（目前只检查入口文件的修改时间） |
