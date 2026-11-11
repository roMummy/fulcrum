# A-BOT 插件迁移台账

## 来源基线

- 来源项目：A-BOT（作者自有项目）
- 来源分支：`feature-855`
- 来源提交：`a842271986b1e50ab62daa30b5be73a65d872d0d`
- 核对日期：`2026-09-01`
- 迁移方式：SaaS 原生重构，不加载 A-BOT 运行时，不保留兼容调用链

本次从 `9efa1739748e0e8fa2506b80b9670e77080f001a` 核对到
`a842271986b1e50ab62daa30b5be73a65d872d0d`，新增了语义 Skill 注册、路由、会话、执行和观测链路，
并修改了 `ai_auto_response`、`codex_quota_allocator`、`dify`、`dota2_stats`、`douyu`、
`message_summary`、`robot_menu`、`stats_collector`、`weather`。本轮 Task 10 的六个积分与任务插件
没有来源变更；受影响的后续插件仍按当前基线重新核对，不直接复用旧审核结论。

每次开始新的插件迁移前都要确认来源分支是否更新。来源提交发生变化时，先更新本台账，再核对新增提交影响的命令、外部接口、数据表和定时动作。

## 已有插件审核

下列插件已经存在于 SaaS，但仍需逐个对照来源基线完成最新业务审核。

| 插件代码 | 外部成本 | 敏感配置 | 当前状态 | SaaS 提交 | 轻量验证 |
| --- | --- | --- | --- | --- | --- |
| `ai_auto_response` | 租户承担 LLM/Dify 成本 | API Key | 已审核 | `b2a2a0e` | `PYTHONPATH=. .venv/bin/pytest -q tests/test_existing_ai_content_plugins.py -k ai_auto_response`（3 passed） |
| `daily_news` | 无固定外部成本 | 无 | 已审核 | `4a8ec15` | `pytest -q tests/test_existing_ai_content_plugins.py -k daily_news`（3 passed）；调度测试 6 passed |
| `dify` | 租户承担 Dify 成本 | API Key | 已审核 | `6b21339` | `PYTHONPATH=. .venv/bin/pytest -q tests/test_existing_ai_content_plugins.py -k dify`（3 passed） |
| `douyin_parser` | 租户承担抖音解析服务成本 | 可选 API Key | 已审核 | `3bbd653` | `PYTHONPATH=. .venv/bin/pytest -q tests/test_existing_query_plugins.py -k douyin_parser`（2 passed）；插件相关组合验证 27 passed |
| `douyu` | 无固定外部成本 | 无 | 已审核 | `227b646` | `PYTHONPATH=. .venv/bin/pytest -q tests/test_existing_query_plugins.py`（12 passed）；斗鱼统一调度 1 passed；调度、迁移和目录契约组合验证 10 passed |
| `duanju_search` | 视租户短剧搜索服务而定 | 可选 API Key | 已审核 | `fc02305` | `PYTHONPATH=. .venv/bin/pytest -q tests/test_existing_query_plugins.py -k duanju_search`（2 passed）；插件相关组合验证 29 passed |
| `game_task` | 无固定外部成本 | 无 | 已审核 | `d6beb3c` | 状态插件与目录组合验证 15 passed |
| `global_news` | 租户承担 LLM 成本 | API Key | 已审核 | `88839bb` | `PYTHONPATH=. .venv/bin/pytest -q tests/test_existing_ai_content_plugins.py -k global_news`（3 passed）；插件相关组合验证 37 passed |
| `group_auto_invite` | 无固定外部成本 | 无 | 已审核 | `c039963` | 群插件定向验证 6 passed |
| `group_virtual` | 无固定外部成本 | 无 | 已审核 | `7a04bb1` | 群插件定向验证 7 passed |
| `guess_song` | 租户承担音乐聚合服务成本 | 可选 API Key | 已审核 | `a751040` | 群插件定向验证 8 passed |
| `jd_sign_token` | 租户自行维护京东令牌 | Token | 已审核 | `2506b03` | 状态插件与目录组合验证 15 passed |
| `lucky_pot` | 无固定外部成本 | 无 | 已审核 | `d84a5cb` | 状态插件与目录组合验证 15 passed |
| `message_push_task` | 无固定外部成本 | 无 | 已审核 | `6ce75c4` | 状态插件与目录组合验证 15 passed |
| `message_sign` | 无固定外部成本 | 无 | 已审核 | `415f74e` | 状态插件与目录组合验证 15 passed |
| `message_summary` | 租户承担 LLM/Dify 成本 | API Key | 已审核 | `12bab98`、`89e9efb` | 群总结定向 4 passed；统一调度 6 passed；消息读取 7 passed |
| `music` | 租户承担音乐聚合服务成本 | 可选 API Key | 已审核 | `6bac843` | `PYTHONPATH=. .venv/bin/pytest -q tests/test_existing_query_plugins.py -k music`（2 passed）；插件相关组合验证 25 passed |
| `point_trade` | 无固定外部成本 | 无 | 已审核 | `cd282fc` | 状态插件与目录组合验证 15 passed |
| `weather` | 租户承担和风天气 API 成本 | API Key | 已审核 | `051575b` | `PYTHONPATH=. .venv/bin/pytest -q tests/test_existing_query_plugins.py -k weather`（3 passed）；统一调度 6 passed；插件相关组合验证 31 passed |

## 新插件迁移

| 插件代码 | 核心能力 | 外部成本 | 敏感配置 | 当前状态 | SaaS 提交 | 轻量验证 |
| --- | --- | --- | --- | --- | --- | --- |
| `epic_free` | 免费游戏查询与定时推送 | 无固定外部成本 | 无 | 已迁移 | `20d5cbd` | `PYTHONPATH=. .venv/bin/pytest -q tests/test_plugin_epic_free.py tests/test_plugin_scheduler.py tests/test_plugin_migration_catalog.py`（11 passed） |
| `github_opengraph` | GitHub 链接图片预览 | 无固定外部成本 | 散列盐 | 已迁移 | `84a255d` | `PYTHONPATH=. .venv/bin/pytest -q tests/test_plugin_github_opengraph.py tests/test_plugin_common_services.py tests/test_plugin_migration_catalog.py`（13 passed） |
| `robot_menu` | 当前机器人功能菜单 | 无固定外部成本 | 无 | 已迁移 | `e0c6535` | `PYTHONPATH=. .venv/bin/pytest -q tests/test_plugin_robot_menu.py tests/test_plugin_migration_catalog.py`（4 passed） |
| `gscore_adapter` | GScore 消息适配 | 租户承担外部服务成本 | API Key | 已迁移 | `88890a7` | `PYTHONPATH=. .venv/bin/pytest -q tests/test_plugin_gscore_adapter.py tests/test_plugin_common_services.py tests/test_plugin_migration_catalog.py`（14 passed） |
| `ai_gen_image` | 文生图与图片编辑 | 租户承担模型成本 | API Key | 已迁移 | `a8de7e4` | `PYTHONPATH=. .venv/bin/pytest -q tests/test_plugin_ai_gen_image.py tests/test_plugin_common_services.py tests/test_plugin_runtime_secrets.py tests/test_plugin_migration_catalog.py`（22 passed） |
| `maibot_adapter` | MaiBot 消息转发与回复 | 租户承担外部服务成本 | API Key | 已迁移 | `5fb4219` | `PYTHONPATH=. .venv/bin/pytest -q tests/test_plugin_maibot_adapter.py tests/test_plugin_runtime_secrets.py tests/test_plugin_secret_forms.py tests/test_plugin_migration_catalog.py tests/test_plugin_scheduler.py`（24 passed） |
| `codex_quota_allocator` | Codex 配额查询、调整与重置 | 租户承担上游账户成本 | 管理密钥、管理员 API Key | 已迁移 | `ba8806e` | `.venv/bin/python -m pytest -q tests/test_plugin_codex_quota_allocator.py tests/test_plugin_migration_catalog.py tests/test_plugin_runtime_secrets.py::test_declared_plugin_secret_defaults_are_physically_empty`（9 passed） |
| `newapi_coupon` | NewAPI 用户绑定与领券 | 租户承担上游套餐成本 | 管理员令牌、SMTP 密码 | 已迁移 | `eaf31db` | `.venv/bin/python -m pytest -q tests/test_plugin_newapi_coupon.py tests/test_plugin_migration_catalog.py tests/test_plugin_runtime_secrets.py::test_declared_plugin_secret_defaults_are_physically_empty`（8 passed）；临时 SQLite 空库升级到 `20260901_0017` |
| `daily_ranking` | 群聊日榜与积分奖励 | 无固定外部成本 | 无 | 已迁移 | `e543fc5` | `.venv/bin/python -m pytest -q tests/test_plugin_daily_ranking.py tests/test_plugin_scheduler.py tests/test_plugin_migration_catalog.py`（12 passed）；定向语法检查与 `git diff --check` 通过 |
| `dota2_stats` | Dota2 战绩与群排行 | 可选 OpenDota 成本 | API Key | 已迁移 | `d3ea0b2` | `.venv/bin/python -m pytest -q tests/test_plugin_dota2_stats.py tests/test_plugin_scheduler.py tests/test_plugin_migration_catalog.py tests/test_plugin_runtime_secrets.py::test_declared_plugin_secret_defaults_are_physically_empty tests/test_plugin_common_services.py`（20 passed）；定向语法检查与 `git diff --check` 通过 |
| `member_context` | 群成员画像与增量摘要 | 租户承担可选 Dify 成本 | API Key | 已迁移 | `b439d24` | `.venv/bin/python -m pytest -q tests/test_plugin_member_context.py tests/test_plugin_scheduler.py tests/test_plugin_migration_catalog.py`（13 passed）；定向语法检查与 `git diff --check` 通过 |
| `member_roast` | 基于画像的成员锐评 | 租户承担 LLM 成本 | API Key | 已迁移 | `880a314` | `.venv/bin/python -m pytest -q tests/test_plugin_member_roast.py tests/test_plugin_migration_catalog.py tests/test_plugin_runtime_secrets.py::test_declared_plugin_secret_defaults_are_physically_empty`（6 passed）；定向语法检查与 `git diff --check` 通过 |
| `value_rank` | 身价、关系和趋势排行 | 无固定外部成本 | 无 | 已迁移 | `283d2c7` | `.venv/bin/python -m pytest -q tests/test_plugin_value_rank.py tests/test_plugin_scheduler.py tests/test_plugin_migration_catalog.py`（21 passed）；定向语法检查与 `git diff --check` 通过 |
| `fun_command_play` | 自定义趣味命令与媒体回复 | 无固定外部成本 | 无 | 已迁移 | `9a03136`、`27e40d7`、`6831cd5` | `.venv/bin/python -m pytest -q tests/test_plugin_fun_command_play.py tests/test_plugin_migration_catalog.py`（8 passed）；通用链接回复 3 passed；Alembic 生命周期 2 passed；目录发现无错误或警告；定向语法检查与 `git diff --check` 通过 |
| `xiuxian` | 多租户修仙玩法 | 无固定外部成本 | 无 | 已迁移 | `917f383`、`057b4cb`、`8e4cd0c` | `PYTHONPATH=. .venv/bin/pytest -q tests/test_plugin_xiuxian.py tests/test_cultivation_service.py tests/test_plugin_migration_catalog.py tests/test_alembic_lifecycle.py`（13 passed）；目录发现和加载通过；定向语法检查与 `git diff --check` 通过 |

## 平台原生替代

| A-BOT 插件 | 当前 SaaS 能力 | 状态 |
| --- | --- | --- |
| `plugin_manager` | 插件清单、机器人绑定、群作用域和运行页 | 已覆盖，不创建兼容插件 |
| `stats_collector` | 插件执行日志、小时聚合、大盘和告警 | 已覆盖，不创建兼容插件 |
| `system_updater` | 平台员工运维流程 | 已划入平台边界，租户不可触发 |
| `trendradar_permission` | 机器人绑定与群插件作用域 | 已覆盖，不创建兼容插件 |

## 明确排除

| 插件代码 | 处理结果 |
| --- | --- |
| `pfe_hub` | 只有说明文档，不是可运行插件 |

## SaaS 自有插件

`customer_operations` 和 `supplier_order_flow` 是当前 SaaS 自有能力，不属于 A-BOT 迁移范围，继续遵守现有租户、渠道和 Open API 边界。

## 斗鱼审核边界

当前 SaaS 已实现斗鱼房间开播/下播提醒、鱼吧动态、成员提醒名单、官方高能增量推送和统一定时轮询。订阅定义保存在插件绑定普通配置中，房间状态、鱼吧最新动态、高能房间基线和会话投递基线保存在 `PluginStateRecord`，聊天命令只使用可信租户、机器人、会话和发送人上下文。

A-BOT 的斗鱼弹幕日报和粉丝日报依赖独立弹幕采集、直播会话、观众统计、图片渲染与模型生成链路。当前 SaaS 尚未具备这些真实数据源，因此本次不注册日报命令、不生成替代文本，也不把官方高能摘要伪装成弹幕日报。后续若建设跨渠道弹幕采集域，应以独立业务表和统一调度能力重新设计，不把大型日报素材塞入插件小型状态表。

## 群与互动插件审核边界

群成员变更由渠道适配器先转换为通用 `member_change` 事件，插件只使用可信会话更新当前群成员；微信系统 XML 只是当前微信渠道的事件解析，不承担群和渠道定位。自动拉群通过渠道适配器统一的群邀请动作执行，当前只有 iPad 874 实现该能力，其他渠道不展示不可用入口。

群桥配置只保存平台 `Chatroom.id`，管理员只能让当前群创建、加入或退出群桥。普通文本根据可信 `Message`、`Conversation` 和 `Chatroom` 定位来源，再通过统一出站任务队列发送，因此 Telegram、微信、企微等已接入渠道可以共用一条跨渠道转发路径。

猜歌名只保留 A-BOT 当前 `猜歌名` 命令，音乐服务地址和可选 API Key 由租户维护。游戏答案和时限状态保存在当前绑定的 `PluginStateRecord`，不再写入插件配置；歌曲通过标准文件回复发送，不生成微信音乐 XML 或协议专用语音载荷。

## 积分与任务插件审核边界

游戏任务、幸运池、签到补签和积分交易只从可信入站消息读取租户、机器人、渠道、群会话和发送人，不接受运维执行接口或消息载荷伪造身份、目标、金额和群编号。随机发题、开奖和打劫结果都由可信业务主键稳定派生，任务状态、奖池状态、打劫冷却和定时投递基线统一保存到当前插件绑定的 `PluginStateRecord`。

所有积分奖励、扣款、转账、开奖、失败罚款和保释都使用租户内唯一业务键；多方结算在一个数据库事务内提交，并发重复消息通过积分流水唯一键回滚后读取既有结果。京东令牌只写入租户敏感配置存储，聊天回复、普通插件配置和日志均不返回令牌原文。

定时推送只保存当前租户可访问的会话编号，由统一调度器创建标准出站任务。插件不持有 Provider 地址、凭证或协议载荷，也不直接调用企业微信、微信或 Telegram 发送接口。

## Epic 免费游戏迁移边界

Epic 免费游戏只访问固定的官方活动接口，地区、语言、是否包含即将免费内容和轮询频率由当前租户绑定配置维护，执行载荷不能覆盖查询地址或地区。官方响应只提取游戏、活动窗口、价格和产品链接，不保存完整第三方返回。

统一调度器按小时创建查询动作，插件通过标准出站队列发送文本。内容摘要按插件绑定和目标群分别保存到 `PluginStateRecord`，因此已收到当前内容的群不会重复推送，部分群入队失败也不会导致其他群重复，新启用的群仍能收到当前活动。微信系群级停用、绑定允许/拒绝范围和非群会话都会在入队前过滤。

## GitHub 链接预览迁移边界

GitHub 链接预览只接受 `github.com` 下至少包含 owner/repository 的公开地址，查询参数和片段不参与重复预览，中文标点、重复链接和单消息数量上限在下载前处理。执行载荷只能提供标准消息文本，不能覆盖 OpenGraph 域名、图片地址或散列盐。

散列盐保存在当前租户插件绑定的密文配置中。插件只通过平台受控 HTTP 客户端访问固定的 `opengraph.githubassets.com`，校验响应状态、图片类型、响应体上限和超时后在内存中生成标准图片回复；不下载仓库内容，不写本地图片文件，也不调用微信或 Telegram 专用发送接口。

## 机器人功能菜单迁移边界

功能菜单只响应群聊中的 `菜单` 和 `功能菜单`，并从可信入站消息确定当前租户、机器人、渠道和群会话。执行载荷不能伪造身份、机器人或目标群，运维执行入口也不能生成业务菜单。

菜单只展示当前机器人已经启用、当前渠道支持、当前群未停用且具有命令或关键词触发方式的目录插件。定时插件、系统插件、停用插件、群级关闭插件和无用户触发方式的插件不会展示；旧状态查询、群列表、插件启停、管理员管理、图片模板和自动撤回链路均已删除。菜单只生成标准文本回复，不依赖微信、企业微信或 Telegram Provider。

## GScore 适配迁移边界

GScore 只保留 `早柚` 业务查询命令，服务地址和 API Key 由当前租户插件绑定维护，平台不提供共享服务或密钥。旧全局 WebSocket 单例、自动重连、`重连早柚` 管理命令、微信联系人查询和管理员判断链路均已删除；插件禁用或租户配置不完整时不会创建外部 HTTP 客户端。

查询内容、发送人、机器人、渠道和会话只从可信入站消息及数据库归属链生成，执行载荷中的同名字段不能覆盖。GScore 响应只读取固定 `content` 数组并转换为标准文本、纯文本 Markdown 和 base64 图片；远程图片 URL、文件路径及不支持的媒体类型不会触发额外下载。响应中的 `target_id`、`bot_id`、`tenant_id` 和 `channel_id` 全部丢弃，结果始终由当前执行链路投递回原会话。

## AI 绘图迁移边界

AI 绘图保留 `AI绘图`、`绘图`、`画图` 和 `生成图片` 四个业务命令。API Base、模型和请求超时由当前租户普通配置维护，API Key 保存到当前插件绑定密文；平台不提供共享模型、共享密钥、全局 LLM 场景或旧图片接口回退。

文本生图固定调用 OpenAI 兼容 `/images/generations`，当前消息存在已下载的可信图片附件时固定调用 `/images/edits`。提示词只从落库入站消息解析，参考图只从当前租户、渠道、会话和消息关联的附件记录读取；执行载荷不能覆盖提示词、模型、地址或附件路径。请求在平台已有异步插件分发任务中执行，图片结果只接受受限 JSON 中的有效 `b64_json`，经内存校验后生成标准图片回复，不保存临时文件，也不调用微信、企业微信或 Telegram 专用发送接口。

## MaiBot 适配迁移边界

MaiBot 作为旁路观察者采集群聊和私聊消息，不注册聊天命令，也不阻断其他插件。WebSocket 地址、采集开关、回复开关和连接参数由当前租户普通配置维护，API Key 保存到当前绑定密文；每个 `(tenant_id, binding_id)` 使用独立线程、事件循环、发送队列和 WebSocket，不共享跨租户连接。绑定停用、配置保存、机器人删除和应用关闭都会停止对应旧连接。

入站消息维度只从可信绑定、渠道、会话和消息构造，群成员与群会话使用平台内部令牌，不向 MaiBot 暴露可直接改选的原始出站目标。客户端只接受当前连接最近 24 小时实际发送过的内部会话回复，并限制最近会话数量；回包进入平台后再次验证租户、绑定、机器人、渠道、会话、群级停用和回复开关，只提取文本或文本片段列表并写入标准出站任务。MaiBot 回包中的其他路由、媒体和任意对象不能指定租户、渠道或目标会话。

## Codex 额度管理迁移边界

Codex 额度管理只保留 `额度查询`、`额度调整` 和 `周额重置` 三个业务命令。CPA 服务根地址、认证文件名、Sub2API 服务根地址、目标分组和额度策略由当前租户普通配置维护，CPA Management Key 与 Sub2API Admin API Key 保存到当前绑定密文；平台不提供共享账户、共享凭证或环境变量回退。

CPA 只调用已经验证的 `v0/management/auth-files` 和 `v0/management/api-call`，由 CPA 代理读取 Codex 七天主额度窗口，平台不接触 OAuth Token。Sub2API 只调用已经验证的 v1 分组、有效订阅、分组更新和订阅周额重置接口；目标必须是租户配置的 OpenAI 订阅分组，响应混入其他分组、非有效订阅、未知信封或未知接口版本时直接拒绝，不探测备用路由，也不解析私有替代字段。

查询按真实发送人限流，订阅邮箱只返回脱敏标识。调整和重置还要求租户配置管理员用户 ID、用户级写限流以及同一管理员在同一会话中发送第二条新消息确认；同一消息的任务重试不能充当确认。最终快照与首次预览不一致时旧确认失效，确认状态在外部写入前持久化为已消费，避免外部成功后任务重试造成重复写入。额度调整按有效订阅原生周用量和 CPA 已用比例反推统一周限额并向下截断；周额重置只清零 `weekly`，明确保留 `daily` 和 `monthly`。

## NewAPI 领券迁移边界

NewAPI 领券只保留群聊中的 `领券 邮箱` 单一入口。插件从可信绑定、渠道、群会话、落库消息、当前群记录和活跃群成员记录确认领取人，不读取消息载荷中的用户、邮箱、地址或套餐字段，也不通过渠道 API 临时补查成员。NewAPI 地址、管理员用户 ID、明确套餐 ID、开户策略、限流和邮件配置由当前租户维护，管理员访问令牌与 SMTP 密码保存到当前插件绑定密文。

`coupon_account_bindings` 使用租户、插件绑定、渠道用户、邮箱和 NewAPI 用户唯一约束保存不可自行更换的领取关系。首次请求在访问 NewAPI 前先提交 `pending` 预留，确定性用户名使任务重试复用同一账号；同一用户换邮箱、同一邮箱换用户、同一上游账号重复发放都在外部写入前拒绝。已有 `active` 记录直接返回幂等结果，不再次查询或绑定套餐。NewAPI 只使用当前确认的用户查询、用户创建、套餐列表、用户订阅、套餐绑定、登录和订阅扣费偏好接口，响应必须符合固定 `success/message/data` 信封，不解析旧分页或嵌套替代结构。

自动开户只有在租户配置完整 SMTP 且通知模式为邮件时才允许执行。初始密码使用安全随机数在内存生成，只进入 NewAPI 创建请求和租户邮件发送器，不写数据库、不进标准回复、不进执行输出，也不会在邮件失败时回退到群聊；邮件失败后领取仍保持已发放状态，并提示用户联系管理员重置密码。数据库迁移使用 `20260901_0017`，已从临时空库完整升级验证。

## 群成员画像迁移边界

群成员画像是纯后台定时插件，不注册聊天命令。租户必须明确选择需要画像的群会话，统一调度器才会按会话创建日、周、月刷新任务；任务载荷只保存插件绑定和会话主键，租户、机器人、渠道、群成员、消息和密钥均在执行时通过可信数据库归属链重建。插件只读取平台标准 `Message`、`Conversation`、`Chatroom` 和 `ChatroomMember`，不访问微信、企业微信、Telegram 或其他 Provider 的联系人和消息接口。

每个成员的画像使用可信渠道、群主键和成员标识哈希生成状态键，保存到当前绑定的 `PluginStateRecord`。首次建档读取有限历史样本，后续只处理 `last_message_id` 水位之后的新入站文本；重复调度在没有新消息时直接跳过。状态只保存结构化画像、聚合统计和增量水位，不保存完整聊天输入、Dify 请求正文、API Key 或第三方原始响应。

Dify 地址由租户普通配置维护，API Key 保存到当前绑定密文，平台不提供共享模型或共享密钥。每个群每次刷新最多批量调用一次工作流，输入限制成员数量、群消息数量和单条文本长度；外部服务未配置、超时、失败或返回无效 JSON 时仍保存本地基础画像。Dify 返回结果只能覆盖本次候选成员白名单内的画像字段，返回其他群或未知成员会被丢弃，成员身份、水位和来源统计始终由平台保留。

## 群成员锐评迁移边界

群成员锐评只接受真实群入站文本中的“锐评/吐槽”命令。被锐评目标只能是消息发送人本人，或当前群成员表中按外部账号、昵称、展示名唯一匹配的成员；执行载荷不能指定目标，重名时要求使用唯一账号 ID，不读取微信 XML、Provider 联系人缓存或临时成员查询接口。

锐评插件只复用同一机器人已启用 `member_context` 绑定中的结构化画像，不在命令链路复制画像生成逻辑。目标没有画像或近期有效发言不足时直接给出业务提示。最近消息按当前群、目标成员、时间窗口和数量上限读取，再压缩为消息数、平均长度、问句比例、高频词、重复短语和有限代表句；完整聊天历史、数据库对象和模型请求正文均不写入日志、插件状态或执行结果。

模型 API 地址、模型名称、温度、超时和采样上限由租户普通配置维护，API Key 保存到当前绑定密文，调用使用平台通用 OpenAI-compatible 文本生成能力。用户和群分别使用跨进程固定窗口限流控制模型成本。模型提示禁止编造职业、家庭、健康、政治、宗教等敏感信息，返回文本会强制替换为当前可信目标标题并限制长度；模型失败只返回有限业务错误，不泄露画像、提示词、上游响应或密钥。

## 群成员身价迁移边界

群成员身价只使用当前租户群的 `PointsAccount`、标准入站 `Message`、`Conversation`、`Chatroom` 和 `ChatroomMember`。每日快照按积分、统计窗口发言、活跃天数和社交得分四个维度计算，指标先按群内 95 分位缩放，再应用潜水惩罚；权重会归一化，避免租户错误配置让总权重失真。候选成员来自当前群积分和真实发言并集，不建立跨租户、跨群或平台全局排行。

社交关系只识别标准文本中对当前活跃群成员唯一账号、昵称或展示名的明确 `@`，重名不会建立关系边，也不解析微信 XML、Provider 提及字段或联系人缓存。社交热度、搭子、桥梁和关系摘要与身价排行共享同一每日结构化快照，快照和周报保存到当前绑定的 `PluginStateRecord`；趋势只扫描当前绑定、当前会话和有限日期窗口内的快照。

“我的身价”和“我的趋势”固定查询真实消息发送人，执行载荷不能替换用户或会话。“重算身价”只允许租户普通配置中的渠道成员账号执行。统一调度器只为租户明确选择的群会话创建每日重算、周一周报预生成和周报推送任务；周报推送通过标准出站队列并使用绑定、会话和周日期稳定幂等，不调用任何具体渠道 Provider。

## 趣味命令迁移边界

趣味命令作为 `observer` 只观察标准群聊或私聊文本，具体规则必须通过可信租户、机器人绑定、渠道、会话和落库入站消息查询。规则独立保存在 `fun_command_rules`，同一机器人插件绑定内触发文本唯一；当前只支持 `exact`、`prefix` 和 `contains` 三种明确匹配方式，不执行租户可编辑正则，也不保留 A-BOT 全局作用域、事件触发或拍一拍解析。规则按优先级执行首条命中结果，冷却使用当前绑定的跨进程状态限流，不维护 Redis 或进程内规则缓存。

规则回复只支持文本、图片、视频和链接卡片。文本模板只开放发送人、发送人名称、会话名称和原消息四个固定占位符；图片和视频必须使用 `DATA_DIR/tenants/<tenant_id>/fun_command` 下的相对路径，并在读取前完成目录归属和文件体积校验。链接先经过 HTTP/HTTPS URL 校验，再进入平台通用回复链；Telegram 当前降级为标题、描述和链接文本，微信 iPad 由渠道适配器转换为 874 `SendApp(Type=5)`，其他渠道接收同一通用链接载荷。插件不拼微信 XML，不直接调用企业微信、微信、Telegram 或其他 Provider，也不迁移旧语音、App 消息、延时连发和媒体 LRU 缓存。

## 文字修仙迁移边界

文字修仙只接受真实群入站文本中的正式命令。租户、机器人绑定、渠道、会话、消息和发送人必须通过数据库归属链交叉验证，执行载荷不能覆盖命令、道号、目标、数量或身份。目标玩家只能在当前租户、当前插件绑定和当前群中按渠道账号 ID、道号或唯一显示名解析；显示名重合时拒绝猜测，要求使用唯一账号 ID。插件输出统一使用标准文本回复，不读取或生成微信 XML，也不调用企业微信、微信、Telegram 或其他 Provider。

玩家、背包、门派、资产流水和命令冷却分别保存在 `cultivation_players`、`cultivation_inventory`、`cultivation_clans`、`cultivation_transactions` 和 `cultivation_cooldowns`。所有数据同时受租户、插件绑定和群会话边界约束；写命令使用消息级唯一业务键，多个 Worker 重复消费同一消息时整体回滚失败事务并读取已完成结果。物品和灵石赠送、劫掠、购买、突破、炼丹与历练在数据库行锁内结算，稳定随机数由可信消息身份派生，任务重试不会改变结果。

积分兑换灵石复用当前积分账本，并让积分扣减、灵石入账、两类流水、命令结果和冷却共享同一次数据库提交，不能出现部分到账。签到使用玩家日期字段，闭关、受伤、气息不稳、历练和炼丹状态使用上海时区时间。排行榜直接查询当前群玩家，不迁移 Redis 排行榜、玩家缓存、旧修仙表或单机初始化 SQL；旧帮助别名、进程内管理入口、微信提及解析和非正式 `出售` 命令均不保留。

## 最终交付校验

`scripts/verify_plugin_migration.py` 固定校验 36 个 A-BOT 目标插件和 2 个 SaaS 自有插件。脚本使用 TOML 与 Python AST 静态检查目录数量、manifest、真实执行入口、敏感字段空默认值、废弃双入口和 A-BOT `base/db/utils/plugins` 运行时导入，只输出数量、插件代码和错误，不读取数据库或输出凭证。

部署必须配置独立 `ABOT_PLUGIN_SECRET_KEY`，执行完整性校验、`alembic upgrade head` 和 `scripts/scrub_plugin_plaintext_secrets.py` 后再替换旧进程。清理脚本只输出租户、绑定和删除字段数量。升级前仍把 API Key 等凭证保存在普通配置中的绑定会删除对应明文，租户需要在新插件配置表单中重新录入；已写入新密文列的凭证不会被重复清理。

需要租户自行维护外部凭证的插件包括：`ai_auto_response`、`ai_gen_image`、`codex_quota_allocator`、`dify`、`dota2_stats`、`douyin_parser`、`duanju_search`、`github_opengraph`、`global_news`、`gscore_adapter`、`guess_song`、`jd_sign_token`、`maibot_adapter`、`member_context`、`member_roast`、`message_summary`、`music`、`newapi_coupon` 和 `weather`。平台不提供共享凭证或旧环境变量回退。
