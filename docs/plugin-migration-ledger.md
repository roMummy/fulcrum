# A-BOT 插件迁移台账

## 来源基线

- 来源仓库：`/data/abot`
- 来源分支：`feature-855`
- 来源提交：`9efa1739748e0e8fa2506b80b9670e77080f001a`
- 核对日期：`2026-08-31`
- 迁移方式：SaaS 原生重构，不加载 A-BOT 运行时，不保留兼容调用链

本次从 `ad5147b03b590bfad3a8932924b049f7c2198d2a` 核对到
`9efa1739748e0e8fa2506b80b9670e77080f001a`，新增内容仅为语义 Skill 的设计与实施计划，
未修改插件目录、命令、外部接口、数据表或定时动作。

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
| `game_task` | 无固定外部成本 | 无 | 待审核 | - | - |
| `global_news` | 租户承担 LLM 成本 | API Key | 已审核 | `88839bb` | `PYTHONPATH=. .venv/bin/pytest -q tests/test_existing_ai_content_plugins.py -k global_news`（3 passed）；插件相关组合验证 37 passed |
| `group_auto_invite` | 无固定外部成本 | 无 | 待审核 | - | - |
| `group_member_change` | 无固定外部成本 | 无 | 待审核 | - | - |
| `group_virtual` | 无固定外部成本 | 无 | 待审核 | - | - |
| `guess_song` | 无固定外部成本 | 无 | 待审核 | - | - |
| `inactive_rank` | 无固定外部成本 | 无 | 待审核 | - | - |
| `jd_sign_token` | 租户自行维护京东令牌 | Token | 待审核 | - | - |
| `lucky_pot` | 无固定外部成本 | 无 | 待审核 | - | - |
| `message_push_task` | 无固定外部成本 | 无 | 待审核 | - | - |
| `message_sign` | 无固定外部成本 | 无 | 待审核 | - | - |
| `message_summary` | 租户承担 LLM/Dify 成本 | API Key | 已审核 | `12bab98`、`89e9efb` | 群总结定向 4 passed；统一调度 6 passed；消息读取 7 passed |
| `music` | 租户承担音乐聚合服务成本 | 可选 API Key | 已审核 | `6bac843` | `PYTHONPATH=. .venv/bin/pytest -q tests/test_existing_query_plugins.py -k music`（2 passed）；插件相关组合验证 25 passed |
| `point_trade` | 无固定外部成本 | 无 | 待审核 | - | - |
| `weather` | 租户承担和风天气 API 成本 | API Key | 已审核 | `051575b` | `PYTHONPATH=. .venv/bin/pytest -q tests/test_existing_query_plugins.py -k weather`（3 passed）；统一调度 6 passed；插件相关组合验证 31 passed |

## 新插件迁移

| 插件代码 | 核心能力 | 外部成本 | 敏感配置 | 当前状态 | SaaS 提交 | 轻量验证 |
| --- | --- | --- | --- | --- | --- | --- |
| `epic_free` | 免费游戏查询与定时推送 | 无固定外部成本 | 无 | 待迁移 | - | - |
| `github_opengraph` | GitHub 链接图片预览 | 无固定外部成本 | 散列盐 | 待迁移 | - | - |
| `robot_menu` | 当前机器人功能菜单 | 无固定外部成本 | 无 | 待迁移 | - | - |
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
