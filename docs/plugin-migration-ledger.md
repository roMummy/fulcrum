# A-BOT 插件迁移台账

## 来源基线

- 来源仓库：`/data/abot`
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
| `group_member_change` | 无固定外部成本 | 无 | 已审核 | `6c730dd` | 群插件定向验证 3 passed |
| `group_virtual` | 无固定外部成本 | 无 | 已审核 | `7a04bb1` | 群插件定向验证 7 passed |
| `guess_song` | 租户承担音乐聚合服务成本 | 可选 API Key | 已审核 | `a751040` | 群插件定向验证 8 passed |
| `inactive_rank` | 无固定外部成本 | 无 | 已审核 | `f7e2b83` | `PYTHONPATH=. .venv/bin/pytest -q tests/test_existing_group_plugins.py -k inactive_rank`（1 passed） |
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
| `gscore_adapter` | GScore 消息适配 | 租户承担外部服务成本 | API Key | 待迁移 | - | - |
| `ai_gen_image` | 文生图与图片编辑 | 租户承担模型成本 | API Key | 待迁移 | - | - |
| `maibot_adapter` | MaiBot 消息转发与回复 | 租户承担外部服务成本 | API Key | 待迁移 | - | - |
| `codex_quota_allocator` | Codex 配额查询、调整与重置 | 租户承担上游账户成本 | 管理密钥、管理员 API Key | 待迁移 | - | - |
| `newapi_coupon` | NewAPI 用户绑定与领券 | 租户承担上游套餐成本 | 管理员令牌 | 待迁移 | - | - |
| `daily_ranking` | 群聊日榜与积分奖励 | 无固定外部成本 | 无 | 待迁移 | - | - |
| `dota2_stats` | Dota2 战绩与群排行 | 可选 OpenDota 成本 | API Key | 待迁移 | - | - |
| `member_context` | 群成员画像与增量摘要 | 视画像服务而定 | 可选 API Key | 待迁移 | - | - |
| `member_roast` | 基于画像的成员锐评 | 租户承担 LLM 成本 | API Key | 待迁移 | - | - |
| `value_rank` | 身价、关系和趋势排行 | 无固定外部成本 | 无 | 待迁移 | - | - |
| `fun_command_play` | 自定义趣味命令与媒体回复 | 无固定外部成本 | 无 | 待迁移 | - | - |
| `xiuxian` | 多租户修仙玩法 | 无固定外部成本 | 无 | 待迁移 | - | - |

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
| `beautyleg` | 成人或擦边内容，不迁移 |
| `fanhao_search` | 成人或擦边内容，不迁移 |
| `sehuatang_push` | 成人或擦边内容，不迁移 |
| `video` | 成人或擦边内容，不迁移 |
| `video_man` | 成人或擦边内容，不迁移 |
| `xiuren_image` | 成人或擦边内容，不迁移 |
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
