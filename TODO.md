# TODO - Multi Bot Admin

> Last reviewed: 2026-03-24

## 2026-03-24 plugin auto-discovery wording follow-up
- [x] plugin runtime fallback metadata no longer uses `unregistered`; it now uses `undiscovered`
- [x] plugin list/detail/runtime pages now use auto-discovery wording instead of manual registration wording
- [x] execution status wording is now `可执行 / 不可执行`
- [x] schema fallback wording is now `仅 JSON 配置`, avoiding confusion with manual registration
- [x] plugin list page now supports keyword search and multi-condition filtering for large plugin sets
- [x] directory sync now prunes removed plugins from the mirrored `plugins` table

## 2026-03-24 auto outbound follow-up
- [x] inbound dispatch now auto-converts plugin execution results into outbound payloads
- [x] current auto outbound bridge supports text replies and xml/file replies (`music_xml / video_xml / douyu_xml / previous_music_xml`)
- [x] auto outbound is wired in the unified dispatch path, so both `handle_event` and runtime `execute_action` plugins can reply
- [x] dispatch report now records `auto_outbound` details for each executed plugin
- [x] local image reply payloads such as `image_path` can now be auto-translated into adapter-usable base64 payloads
- [x] auto outbound reply normalization now supports explicit `voice` and `video` reply payloads
- [x] core migrated plugins now emit explicit `output["replies"]` contracts while retaining legacy fields for compatibility
- [ ] outbound reply contract still needs a stricter first-class schema and broader plugin-wide normalization rules

## 2026-03-24 wechat_ipad multimedia send follow-up
- [x] `wechat_ipad` adapter now supports real protocol outbound for `voice -> /Msg/SendVoice`
- [x] `wechat_ipad` adapter now supports real protocol outbound for `video -> /Msg/SendVideo`
- [x] `wechat_ipad` adapter now supports CDN video forward payloads via `video + xml -> /Msg/SendCDNVideo`
- [x] `wechat_ipad` voice send now aligns with `abot`: `wav/mp3` inputs are transcoded to `silk(Type=4)` before send
- [x] outbound regression tests now cover voice/video request mapping and persisted raw request bodies
- [ ] current environment still lacks `pysilk`; install runtime deps before using non-silk voice transcoding in production
- [ ] emoji / CDN file / richer app-card media sends are still not fully aligned with `abot`

## 2026-03-24 abot 插件迁移盘点
- [x] 已盘点 `D:\learn\abot\plugins` 目录，作为本轮插件迁移候选源
- [x] 已确认本轮目标应聚焦 `abot` 的“非框架类插件”，不把 `plugin_manager / stats_collector / system_updater` 这类框架或运维插件混入首批业务迁移
- [x] 已确认迁移范围：排除框架/运维插件与高风险内容插件，其余 `abot` 非框架插件全部纳入本项目迁移范围
- [x] 已把确认范围内的插件先接入当前项目 `plugins/` 目录与 runtime registry，形成可见、可绑定、可继续补逻辑的迁移骨架
- [ ] 继续按“先已有能力补齐、再低耦合、再高依赖状态机”的顺序补完整业务执行逻辑

### 第一批建议优先迁移：低耦合业务插件
- [x] `weather`：已存在目录插件，后续继续对齐 `abot` 实现
- [x] `ai_gen_image`：已从迁移骨架升级为可执行 runtime，补齐绘图 schema、真实图片生成链路和接口测试
- [x] `duanju_search`：已从迁移骨架升级为可执行 runtime，补齐短剧搜索 schema、结果解析链路和接口测试
- [x] `douyin_parser`：已迁回目录插件实现，补齐抖音链接解析、卡片 XML 生成与接口测试
- [x] `global_news`：已迁回目录插件实现，补齐 RSS 抓取、可选 Dify 摘要与接口测试
- [x] `music`：已迁回目录插件实现，补齐搜歌、详情拉取、音乐卡片 XML 与接口测试

### 第二批建议迁移：中等耦合业务插件
- [x] `guess_song`：已创建迁移骨架
- [x] `message_sign`：已创建迁移骨架
- [x] `point_trade`：已创建迁移骨架
- [x] `group_auto_invite`：已创建迁移骨架
- [x] `group_virtual`：已创建迁移骨架
- [x] `douyu`：已创建迁移骨架
- [x] `message_push_task`：已创建迁移骨架
- [x] `message_summary`：已存在目录插件，后续继续对齐 `abot` 实现

### 第三批建议迁移：强依赖现有 abot 运行时的业务插件
- [x] `ai_auto_response`：已创建迁移骨架
- [x] `dify`：已存在目录插件，后续继续对齐 `abot` 实现
- [x] `game_task`：已创建迁移骨架
- [x] `xiuxian`：已创建迁移骨架
- [x] `lucky_pot`：已创建迁移骨架
- [x] `jd_sign_token`：已创建迁移骨架

### 暂不建议纳入默认迁移批次
- [x] `plugin_manager`：已从业务插件目录中移除，统一由本项目后台插件管理能力替代
- [ ] `robot_menu`：强依赖旧权限与功能开关实现，需重写，不建议按“迁移插件”处理
- [ ] `stats_collector`：属于框架统计能力，应并入本项目运行时与审计体系
- [ ] `system_updater`：属于系统运维能力，且涉及 shell 执行，不纳入普通业务插件迁移
- [ ] `gscore_adapter`：更像外部桥接/基础设施插件，需单独评估
- [ ] `pfe_hub`：当前目录下未见完整插件主入口，先不列入迁移批次
- [ ] `wx_red_pack`：当前目录下未见完整插件主入口，先不列入迁移批次

### 需你单独确认是否迁移：内容/合规高风险插件
- [ ] `beautyleg`
- [ ] `xiuren_image`
- [ ] `video`
- [ ] `video_man`
- [ ] `fanhao_search`

## 2026-03-24 Wechat iPad 收口补记
- [x] 已扩 `wechat_ipad` client API 覆盖面，对齐 `abot` 的联系人、群资料、好友处理、附件下载等常用动作
- [x] 已把返回值成功判定收口到 `Success` 优先、`Ret/Code == 0` 兼容兜底，并补统一错误码描述
- [x] 已把 `pull-messages -> 群资料自动补同步` 串起来，遇到新群或群成员变更信号会继续补群事实
- [x] 已新增通用动作入口：`POST /channels/{channel_id}/wechat-ipad/actions/{action}`
- [x] 已在渠道详情页补 `Wechat iPad 运维动作台`，可直接执行动作并查看最近一次结果摘要/参数/预览
- [ ] 下一步把动作台继续分层成更贴业务语义的操作面板，减少直接手填 JSON 的成本

## 2026-03-24 插件迁移验证补记
- [x] `ai_gen_image` runtime 已补齐到可执行状态，并完成 `py_compile` 级别校验
- [x] `duanju_search` runtime 已补齐到可执行状态，并完成 `py_compile` 级别校验
- [x] 已把 `dify / message_summary / weather / ai_gen_image / duanju_search / global_news / music / douyin_parser` 的后台执行逻辑迁回各自 `plugins/<code>/plugin.py`
- [x] 已把 runtime registry 调整为“schema + 通用目录桥接执行器”，不再把具体插件业务逻辑继续堆进 `executors.py`
- [x] 已清理 `app/services/plugin_runtime/executors.py` 中遗留的插件业务死代码，仅保留 `PlaceholderExecutor` 与 `DirectoryRuntimeExecutor`
- [x] 已打通目录插件 `config.toml` 的 `runtime` 默认配置注入链路，后台执行会先吃目录默认值，再叠加 bot 绑定覆盖配置
- [x] 已把 `abot` 现有可复用的接口地址与 key 回收到 `dify / weather / global_news / music / douyin_parser / ai_gen_image / duanju_search` 的 `config.toml`
- [ ] 本地缺少 `sqlalchemy`，当前无法执行 `tests/test_multi_bot_admin.py` 的接口级单测；待补依赖后复跑插件执行链路测试

## 2026-03-24 Demo Data Follow-up
- [x] 确认 `docs/data_demo.sql` 已补充群、群成员、联系人、群消息样本，可继续作为 `wechat_ipad` 群治理页面的结构参考
- [x] 发现 demo SQL 仍包含真实微信 ID `wxid_72ow1edm3kea22`，后续禁止原样导入
- [x] 新增 `scripts/sanitize_data_demo.py`，先提供脱敏导出入口，避免用 demo 数据推进页面时误带入正式号
- [x] 单群成员列表已补充 demo 字段展示：`member_flag / inviter_user_name / alias / remark / province / city / small_head_img_url`
- [x] 新增单群“生成 200 人演示成员”入口，使用当前群成员样本派生假数据，并通过内嵌 SVG 头像保证本地可直接显示
- [ ] 下一步继续把 demo 消息样本接成“群详情最近消息预览”，但必须基于脱敏后的输入
 
## 2026-03-24 群管理推进

- [x] 已补 `wechat_ipad` 完整群列表页：`/channels/{channel_id}/chatrooms`
- [x] 已补单群详情页：`/channels/{channel_id}/chatrooms/{chatroom_id}`
- [x] 单群详情页已可查看群成员列表：`member_external_id / nickname / display_name / role / last_synced_at`
- [x] 单群详情页已可查看每个群的插件权限清单：基于 `channel -> chatroom -> bot_plugin -> GroupPluginScope`
- [x] 渠道详情页已增加“查看全部群”入口，可从渠道页进入完整群管理链路
- [ ] 群级 allow/deny、命令/能力边界、审计留痕仍是后续阶段；当前完成的是“群列表 + 群成员列表 + 群插件权限清单 + 最小启停草稿控制”

> 口径：按新的终极目标重写，优先服务“多机器人统一管理 + 插件自动装载 + 消息可靠入库 + 响应及时 + 可扩展 channel + 微信群治理”。

## 终极目标拆解

项目后续围绕 6 条主线持续推进：

1. **多机器人统一管理**
2. **插件自动装载（目录驱动）**
3. **消息记录可靠存档入库**
4. **响应及时（高效筛选 + 动态匹配 + 执行调度）**
5. **可扩展更多 channel（含飞书）**
6. **微信 channel 的群组管理与群组级插件权限管理**

---

## 已完成（与当前代码状态一致）

### 平台底座
- [x] FastAPI + Jinja2 + SQLAlchemy 项目骨架已可运行
- [x] bots / channels / plugins / bot_plugins / conversations / messages / error_logs 等核心模型已建立
- [x] 机器人、渠道、插件、会话、消息、错误、监控等后台页面已有最小闭环
- [x] Webhook 入站、消息归档、手工出站、基础 smoke test 已打通
- [x] 登录、角色控制、后台账号管理、自助改密、`auth_version` 驱动旧登录态失效链路已落地
- [x] 旧 SQLite 库缺列时，运行时 schema 修补链路可用

### 多机器人能力
- [x] 分组、标签、配置模板、复制、环境字段与总览卡片已进入后台主流程
- [x] bot 维度的启停与部分筛选能力已落地

### 插件目录驱动（已启动并落代码）
- [x] 已建立 `plugins/` 目录驱动主链路的初版骨架
- [x] 已明确插件最小结构：`config.toml` + 单逻辑入口文件
- [x] 逻辑入口兼容：`plugin.py` / `main.py` / `plugin_main.py`
- [x] 已实现目录扫描器与最小刷新路径
- [x] `plugins` 表已开始转向扫描缓存 / 展示镜像
- [x] 后台已开始展示扫描结果与入口元信息

### 消息消费调度（已启动并落代码）
- [x] 已把入站消息链路拆成：标准化 -> 静态候选筛选 -> 动态匹配判定 -> 执行调度 的工程骨架
- [x] 静态候选筛选已开始生效，避免“每条消息全量跑所有插件”
- [x] 动态 matcher 已开始支持：commands / require_mention / keywords / plugin_type 差异化判定
- [x] 执行调度层骨架已具备：统一执行入口、结果结构、异常隔离、timeout 参数位、deferred 语义
- [x] 目录插件统一 Python 接口已开始落地：支持 `plugin` 实例或 `handle_event()` / `matches_event()` 模块函数

### `wechat_ipad` 骨架
- [x] 登录初始化、二维码刷新、状态同步、重连、拉取消息、去重统计、入站/出站入口已在后台显式呈现
- [x] 二维码图片展示链路已具备，不再只停留在 base64 原文
- [x] 健康检查、部分运行态摘要、北京时间/有效期提示已在继续收口
- [x] 渠道详情页已补最近同步诊断：`sync_key`、`continue_flag`、拉取协议摘要、同步新增/去重计数与最近异常提示可直接查看

---

## P0：当前最高优先推进

> 当前排序调整：**优先推进 `wechat_ipad` 相关功能**，再继续其余 P0 主线。只要不越过明显风险边界，后续小闭环优先从 P0.1 开始选。

### P0.1 `wechat_ipad` 能力继续收口（当前第一优先级）
- [ ] 为 `wechat_ipad` 继续对齐真实样本，收紧 endpoint / payload / 状态枚举 / 错误处理
- [ ] 把群资料接入链路正式拆成“两段式”：消息驱动骨架落库 + 群接口补全落库，避免继续把 chatroom/member 只当页面摘要来源
- [ ] 明确 `message_observed -> profile_synced -> members_synced` 的群资料状态推进口径，并补测试覆盖状态跃迁
- [x] 已补最小 `chatrooms` / `chatroom_members` 本地实体骨架与渠道详情页空态扩展位，后续可在其上继续接群资料/群成员同步
- [x] 已补最小群资料/同步可见性摘要：渠道详情页可查看本地群数、成员数、最近同步时间、最近观测来源与最近落库群资料，便于核对是否仍停留在 `message_observed` 骨架
- [x] 已补群资料同步覆盖度摘要：渠道详情页可区分仅消息骨架、已补资料群、已有成员记录群与最近来源，便于运维判断本地落库深度
- [x] 已补群资料同步深度状态标签：渠道详情页可直接区分“尚未形成样本 / 仅消息骨架 / 已有群资料无成员快照 / 已有成员记录”，减少翻 JSON 或逐项比数字
- [x] 已补群资料同步档位运维卡片：渠道详情页直接展示当前档位、短摘要与骨架提示，减少在 `empty_state_message` / 覆盖度说明之间来回切换
- [x] 已补群资料“最近同步摘要”区块：渠道详情页可直接查看群资料覆盖、成员记录覆盖、消息骨架占比，以及最近群资料同步/成员记录同步/观测来源
- [x] 已补群资料“同步判断”运维摘要卡：渠道详情页直接展示当前档位、关键证据、下一步动作与最近来源，便于运维快速判断当前群资料同步深度
- [x] 已补群资料“同步一眼判断”摘要行：渠道详情页基础信息表可直接显示“当前档位 / 关键证据 / 下一步动作”，减少运维继续往下翻多张卡片
- [x] 已补最小错误上下文可见性：失败调用会记录阶段 / endpoint / 时间，渠道详情页可直接定位最近失败点
- [x] 已补最近错误运维摘要：渠道详情页可直接查看阶段 / 接口 / 时间 / 错误消息的汇总结果，减少翻 runtime JSON
- [x] 已补最近错误分类与运维建议：渠道详情页可直接区分登录态异常 / 运行态异常 / 同步异常 / 待归类异常，并给出下一步排查提示
- [x] 已补最近错误分类阶段扩展：覆盖重连唤醒、自动登录、登录确认、二维码刷新与 `/api/Msg/Sync` 等变体，尽量减少落到“待归类异常”
- [x] 已补最近错误分类别名继续收口：覆盖重连恢复、状态同步、等待确认、联系人/群资料同步等阶段别名，并细化二维码准备、扫码确认、重连唤醒、自动登录、资料同步建议
- [x] 已补最近错误单一卡片摘要：渠道详情页会先展示“当前错误态 / 建议 / 关键信息”，减少在错误摘要、分类、阶段和接口行之间来回切换
- [x] 已补最近错误时效分档：可直接区分新鲜 / 偏旧 / 过旧 / 尚无时间样本，并进入结构化字段、运维摘要与详情页展示
- [x] 已补最近错误处置优先级与下一步动作：可直接区分“优先处理 / 先刷新再判断 / 历史样本 / 样本不足”，减少把偏旧错误直接当成当前故障
- [x] 已补最近同步诊断最小空态/异常态提示：可区分尚未形成同步样本、最近同步异常与 continue_flag=1 的待续拉取状态
- [x] 已补最近同步结构化运维摘要：可直接查看分类 / 最近拉取时间 / sync_key / continue_flag / 拉取新增去重计数 / 异常阶段，并区分最近正常但无新增
- [x] 已补最近同步单一卡片摘要：渠道详情页会先展示当前同步档位、状态标签、短摘要与关键 detail，减少在 `sync_key / continue_flag / 最近新增 / 去重 / 异常阶段` 多行之间来回拼读
- [x] 已补最近收发摘要单一卡片：渠道详情页可直接查看最近接收/发送是否已有样本、最近时间、时效分档、下一步动作、累计接收/去重与最近去重事件，减少在接收摘要、发送摘要和协议摘要之间来回切换
- [x] 已把最近收发摘要进一步收口成“当前判断 / 关键证据 / 下一步动作”单视图，并按接收/发送分别展示样本状态与时效，减少人工拼读多行摘要
- [x] 已补最近收发“接收 / 发送分侧判断”与优先处理侧：渠道详情页可直接区分接收正常、发送正常、接收缺样本、发送缺样本及单侧样本过旧/偏旧，并在摘要卡片与基础信息区优先暴露拖后腿的一侧
- [x] 已补最近收发“接收 / 发送一眼判断”短句：摘要卡片直接展示分侧判断，基础信息区固定展示接收/发送各自短句与拖后腿侧，减少运维从长摘要里自己拆状态
- [x] 已补“当前首要处理项”前置卡片：渠道详情页会在“当前运维一眼判断”之前先收口当前最该处理的模块、关键证据与下一步动作，减少运维自行比较多张卡片
- [x] 已补“当前首要处理项”判定依据短摘要：渠道详情页会再固定展示 1 到 3 条触发信号短句，帮助运维理解“为什么现在先处理它”，并区分高优先异常与继续观察/补样本场景
- [x] 已补“当前首要处理项”当前把握短摘要：渠道详情页会固定区分“高置信度当前故障 / 缺样本待确认 / 历史旧样本先刷新 / 继续观察”，避免把谨慎推断误读成已确认故障
- [x] 已补“当前首要处理项”最小测试：覆盖存在高优先问题时能正确指向最近错误/关键模块，以及整体稳定时不会误报高风险
- [x] 已补群级插件权限“仍缺模型摘要”前置短句：渠道详情页顶部基础信息表可直接看到当前主要还缺启停、allow/deny、命令/能力边界、审计留痕中的哪几类核心模型，不必先读长段落
- [x] 已补群级治理“建模判断档位 / 主 blocker / 下一步”稳定摘要：渠道详情页可直接区分“仍不可建模 / 可进入建模但样本偏旧 / 已具备建模前提”，并优先暴露最影响落地的样本或模型缺口
- [x] 已补“登录 / 二维码一眼判断”单一卡片：渠道详情页可直接查看当前登录态、二维码状态、是否过期、最近登录 / CheckQR、当前先看什么与下一步动作，减少在登录表格与协议摘要之间来回切换
- [x] 已补 heartbeat / cache_info / check_qr 最近调用结果的最小结构化字段，渠道详情页可直接查看消息、HTTP 状态和缓存摘要
- [x] 已补 heartbeat / cache_info / check_qr 最近调用结果的状态标签与空态/失败提示，可直接区分尚未调用、等待结果、调用成功与调用失败
- [x] 已补 heartbeat / cache_info / check_qr 最近调用结果的运维摘要与状态 chip，可直接区分尚未调用、等待结果、调用成功、调用失败与返回空摘要
- [x] 已补最近调用结果摘要分档的最小测试，覆盖成功 / 失败 / 尚未调用 / 返回空摘要
- [x] 已补 check_qr / heartbeat / heartbeat_long / cache_info 最近调用时间结构化字段与时间入摘要，便于直接判断结果是否过旧
- [x] 已补最近调用结果时效分档：可直接区分新鲜 / 偏旧 / 过旧 / 尚无时间样本，并写入结构化字段、运维摘要与提示
- [x] 已把 check_qr / heartbeat / heartbeat_long / cache_info 时效分档独立展示到渠道详情页，运维可直接看到中文时效 chip，不再只依赖运维摘要文案
- [x] 已补“最近协议调用总览”单一卡片：渠道详情页可直接查看四类探针的失败/偏旧/过旧/缺样本收口结果、总运维摘要与下一步动作，减少逐行扫协议表格
- [ ] 规划群组级插件权限管理：
  - 群级启停
  - 群级 allow/deny
  - 群级命令/能力边界
  - 群级审计与运营动作
- [ ] 继续补强 `wechat_ipad` 后台运行态、登录态、二维码、同步/收发摘要的可见性与可运维性

### P0.2 多机器人统一管理
- [ ] 把 bot 维度的环境、分组、模板、健康状态进一步统一到更多后台页面
- [ ] 继续补强 bot 级总览、筛选、批量操作和异常定位
- [ ] 为未来多 bot 运维补明确的状态面板和告警入口

### P0.3 插件自动装载（目录驱动继续收口）
- [ ] 把目录扫描结果进一步收敛成“目录事实源优先，registry 兼容，plugins 表镜像”
- [ ] 让目录插件入口真正 load 并参与统一执行路径
- [ ] 把现有硬编码 runtime/executor 逐步桥接到目录插件优先的体系
- [ ] 继续弱化后台手工创建插件定义的中心流程
- [x] 已补最小 manifest 校验与错误收集：非法目录不会打崩扫描，并会以 `invalid` 镜像保留错误信息
- [ ] 继续完善 `config.toml` 字段约定与 manifest 校验

### P0.3 消息记录可靠入库
- [x] 已把入站 dispatch 执行摘要最小持久化到 `raw_webhook_events.dispatch_summary_json`
- [ ] 继续完善原始事件、消息、会话、去重、插件消费计划、执行摘要的入库结构（消息相关字段优先参考/复用 170 `abot` 的消息结构）
- [ ] 明确哪些记录用于审计、哪些用于检索、哪些用于执行上下文
- [ ] 为后续消息检索、消息回放、插件上下文复用预留字段

### P0.4 响应及时（调度链继续收口）
- [ ] 把 dispatch plan 从“候选筛选 + 动态 matcher 骨架”推进到更稳定的执行调度主链
- [x] 已在目录插件 dispatch / runtime executor 主链补最小真实 timeout 保护：timeout 命中会返回可审计的 `timeout` 结果，并写入 dispatch summary
- [ ] 为执行补并发控制
- [x] 已让 `priority` 影响执行顺序，并让 `continue` / `stop` / `consume`（兼容 `exclusive`）在调度层生效
- [ ] 明确 observer / command / rule / ai / system / scheduled 的冲突与调度策略
- [ ] 为异常隔离、重试边界、执行摘要和审计记录补足工程闭环

### P0.5 可扩展更多 channel（含飞书）
- [ ] 抽清 channel 接入统一模型，避免每个新 channel 重写一套消息主链
- [ ] 为飞书预留接入所需的标准化字段、鉴权入口、消息类型映射与出站接口位置
- [ ] 明确 channel adapter 与统一调度层的边界

### P0.6 微信 Channel 的群组管理与群组级插件权限管理
- [ ] 为 `wechat_ipad` 继续对齐真实样本，收紧 endpoint / payload / 状态枚举 / 错误处理
- [ ] 先把群级治理控制链固定为 `channel -> chatroom -> bot_plugin`，不要直接设计成 `chatroom -> plugin`
- [ ] 为群级启停预留最小模型草案：`channel_id + chatroom_id + bot_plugin_id + enabled`
- [ ] 在群级启停模型稳定后，再补 `policy_effect(action_scope)` 口径，明确 allow/deny 只作用于 bot_plugin 绑定层
- [ ] 群级能力边界按顺序推进：群级启停 -> allow/deny -> 命令/能力边界 -> 审计与运营动作
- [ ] 为后续群级治理预留单独读模型或表草案：`config_override_json / last_operator_id / last_operated_at / audit_note`
- [x] 已补最小 `chatrooms` / `chatroom_members` 本地实体骨架与渠道详情页空态扩展位，当前仍只是扩展位，不等于已完成真实群同步
- [x] 已补后台最小群资料可见视图，但当前仍不是完整群管理，不代表真实群资料/群成员同步接口已经完全对齐
- [x] 已补后台群资料同步覆盖度摘要，但当前仍只用于验证本地落库和同步进度，不代表完整群治理能力已上线
- [x] 已补后台群资料最近同步摘要，但当前仍只用于核对本地覆盖比例与最近来源，不代表真实群同步协议已经完全对齐
- [x] 已补后台群资料最近同步时效分档，可直接区分新鲜 / 偏旧 / 过旧 / 尚无时间样本；当前仍只是运维可见性增强，不代表真实群同步协议已经完全对齐
- [x] 已补后台群资料“同步一眼判断”摘要行，可在基础信息表直接看到当前档位、最近同步时效与下一步动作；当前仍只是运维可见性增强，不代表真实群同步协议已经完全对齐
- [x] 已补后台群级插件权限“运维摘要短句”：基础信息表与群资料卡片会直接显示“当前阶段 / 关键证据 / 下一步”，便于先判断本地样本是否足够进入下一轮群级权限建模
- [x] 已把 `plugin_scope_overview` 收口到 `wechat_ipad` 渠道详情页顶部“群级插件权限单一总览”，固定显示 headline / 当前档位 / 优先级 / 主 blocker / 下一步 / 边界提示，仍明确这只是准备度扩展位
- [x] 已补后台群级插件权限“主 blocker 前置短句 / 类型家族”：基础信息区与群资料卡片会固定展示“样本前置 / 模型前置 + 当前最卡 blocker + 补完后才可进入的下一步”
- [x] 已补后台群级插件权限“样本门槛前置卡片”：群资料卡片会直接显示本地 chatroom/member 样本是否足以进入下一步、关键证据与下一步动作，并明确这仍只是治理准备度扩展位
- [x] 已补后台“群级 allow/deny 口径”只读草案摘要：会固定展示是否已定义最小口径、建议判定维度、当前状态、边界说明与下一步，但仍明确这只是口径/扩展位，不代表真实群级 allow/deny 控制已上线
- [ ] 新增群详情视图而不只停留在渠道详情汇总：至少能进入单群查看基础资料、成员快照、绑定插件状态与最近同步时间
- [ ] 把群成员视图从“计数/摘要”提升到“可列表查看”，字段至少包含 `member_external_id / nickname / display_name / role / last_synced_at`
- [ ] 把群级治理页设计成“继承 bot_plugin 状态 + 群级覆盖结果”的只读视图，再逐步进入可写操作

---

## P1：下一阶段推进

### P1.1 后台适配新插件体系
- [x] 已把 `/plugins` 页面收口为“扫描结果 + 快速绑定 + 导航入口”，绑定运行态与编辑职责转交 `/plugins/runtime` 和插件详情页
- [x] 已把 manifest 校验状态、错误、告警同步展示到 `/plugins`、插件详情页和轻量 `/plugins/runtime`
- [ ] 支持 bot 维度启停、覆盖配置、重载、最近执行摘要

### P1.2 执行保护与稳定性
- [ ] 为插件执行补统一超时控制
- [ ] 为插件执行补异常隔离与错误分类
- [ ] 为重复消息 / 重复事件补稳定去重策略
- [ ] 明确单消息内多插件的串行 / 并行 / 独占策略
- [ ] 为 AI / rule / command 插件定义冲突处理策略

### P1.3 认证 / 调度 / 运维能力
- [ ] 补登录成功/失败审计、管理员强制退出指定账号、最近安全事件视图
- [ ] 补最小调度数据模型和后台入口，承载 cron/job 状态与最近执行结果
- [x] 已补最小自动恢复闭环：probe 会对假活跃/僵尸 session 输出 `recovery_action=recreate_session`，并可通过 `scripts/mba_recover_codex_session.sh` 保守执行 cancel/close/recreate/re-dispatch
- [ ] 评估 Alembic 引入时机，逐步替代 runtime schema 修补

---

## 主要风险 / 差距

- [ ] 当前插件 registry / schema / executor 仍带有硬编码首批插件色彩，离“目录插件优先”还有收口工作
- [ ] `plugins` 表虽已开始转为扫描镜像，但仍带一定定义层历史包袱
- [ ] 若消息调度层不持续收紧，插件一多会出现性能、冲突、稳定性问题
- [ ] 缺 170 `abot` 更多真实样本，会限制 `wechat_ipad`、插件组织方式和协议字段对齐速度
- [ ] 若过早把群级控制直接挂到 `plugin` 而不是 `bot_plugin`，后续会出现 bot 绑定、群级启停、群级覆盖配置三层语义打架
- [ ] 若 chatroom/member 继续只停留在可见性摘要而不进入稳定状态机，后续群治理模型会缺少可靠前置条件
- [ ] 随着消息归档、调度和群治理能力增强，正式 migration 的需求会越来越强

---

## 当前设计原则（必须遵守）

- [ ] 后台 UI 不照搬 170 `abot`，但插件组织方式与接入逻辑持续参考真实项目
- [ ] 插件是否存在由目录决定，不再把后台登记当唯一事实源
- [ ] 每个插件最小结构固定为：`config.toml` + 一个逻辑入口文件
- [ ] 逻辑入口需兼容：`plugin.py` / `main.py` / `plugin_main.py`
- [ ] channel 收到消息后，必须先做高效候选筛选，再做动态匹配判定
- [ ] 平台层负责调度和保护，插件层负责声明自己消费什么、如何匹配
- [ ] 不让每条消息都全量跑所有插件
- [ ] 消息记录要为审计、检索、上下文和执行摘要服务，不只是“存一下文本”
- [ ] 设计上预留飞书等新 channel 的统一扩展位
- [ ] 为微信 channel 的群管理与群级插件权限预留明确模型
- [ ] 群级治理始终建立在 `bot_plugin` 绑定层之上，不直接把目录插件当成群级控制对象
- [ ] 群资料链路遵循“消息发现骨架，群接口补全资料，成员接口补全成员快照”的三段式原则
- [ ] 涉及 170 协议层、字段名、回调结构的结论，未拿到真实样本前不写成“已确认”
- [ ] 每轮优先做 1 到 3 个最小闭环，并同步更新 README / TODO / 必要设计文档
## 2026-03-24 runtime migration notes
- [x] `guess_song` upgraded from scaffold to directory runtime plugin
- [x] `guess_song` now supports `start / answer / next` runtime flow and returns round metadata instead of old Redis session state
- [x] `guess_song` runtime defaults moved into `plugins/guess_song/config.toml`
- [x] `douyu` upgraded from scaffold to directory runtime plugin
- [x] `douyu` now supports `room_status / yuba_feed / subscribe_plan` runtime flow
- [x] `douyu` runtime defaults moved into `plugins/douyu/config.toml`
- [x] runtime registry now marks `guess_song / douyu` as `migrated` and uses `DirectoryRuntimeExecutor`
- [x] added direct runtime tests for `guess_song / douyu` directory plugin implementations with mocked provider responses
- [ ] `guess_song` still does not migrate old Redis session persistence, audio clipping, or voice sending
- [ ] `douyu` still does not migrate old Redis subscription storage, scheduled polling, danmu recording, or proactive group push
