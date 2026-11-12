# 标准示例插件 hello_plugin

这是一个可以直接安装、也可以直接复制来改的最小插件。完整规范见文档中心的《插件开发规范》。

## 文件

| 文件 | 作用 |
|---|---|
| `config.toml` | 清单:身份、触发条件、配置表单声明(必需) |
| `plugin.py` | 入口(必需):无状态回复 `execute_action`、消息 `execute_message_action`、定时 `execute_scheduled_action` |

## 示例演示了什么

| 能力 | 在哪里 | 说明 |
|---|---|---|
| 命令回复与配置 | `你好` / `hello` | 文本、下拉、标签、数字、开关等配置字段,读取时校验范围 |
| 显示群昵称 | `你好榜` | 状态里存稳定的用户 ID,显示时用 `member_names` 换成本群群昵称;消息自带的名字常常是 wxid,不要直接存 |
| 定时任务 | `[[schedule]]` + `execute_scheduled_action` | 每天定时给启用的群发问候;只在清单里声明,不改任何调度代码 |
| 状态存储 | `PluginStateStore` | 当前租户、当前绑定内的小型 JSON 状态 |

## 如何基于它写自己的插件

1. 复制整个 `hello_plugin` 目录,改成你的插件编码(小写字母开头,只含小写字母、数字、下划线)。
2. 修改 `config.toml` 里的 `code`(必须与目录名一致)、`display_name`、`description`、`version`、`author`。
3. 修改 `[matcher]` 的命令/关键词,以及 `[[config.fields]]` 声明的配置项。
4. 在 `plugin.py` 里实现你的逻辑。
5. 把目录压缩成 zip(`config.toml` 在 zip 根部,或在唯一一层插件目录里),交给平台管理员在「插件管理」页上传。

## 安装与升级

- 平台管理员在后台「插件管理」页上传 zip:编码不存在是安装,已存在是升级。
- 新安装的插件默认「仅平台可见」,确认无误后由平台设置对哪些租户开放。
- **每次发布新版本务必修改 `version`**。升级前旧版本会自动存档,出问题可以在后台一键回滚。

## 定时任务怎么写

1. 在 `config.toml` 写 `[[schedule]]`(`kind = "daily"` 每天一次,或 `"interval"` 按间隔),用 `enabled_key` 指向一个开关配置项;
2. 在 `plugin.py` 实现 `execute_scheduled_action(db, request)`,`request.action` 就是声明里的 `action`;
3. 平台只告诉你「到点了」和哪个绑定,**不带会话**,发给哪些群由插件决定;发消息用 `enqueue_outbound_job` 入队,不要直接调渠道;
4. 同一计划同一时刻平台只触发一次,错过时间窗口(服务没运行、重启晚了)就跳过当天,不补发,也不需要自己做去重。

## 注意

- 不要把密钥、真实地址、真实账号写进 `config.toml` 或代码;需要密钥时用 `kind = "secret"` 的配置字段。
- 外部 HTTP 调用用平台提供的 `PluginHttpClient`,不要自己发请求;不要直接读写文件系统。
- 压缩包不能包含符号链接、绝对路径或 `..` 路径,总大小不超过 20MB。
