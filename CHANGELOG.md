# Changelog

All notable changes to this project should be documented in this file.

## 2026-11-08

### Added

- 新增企业登录后的“开发文档”入口和桥接接口文档中心
- 新增 `/api/open/*` 推荐协议说明，覆盖 HMAC 鉴权、群/会话查询、消息和文件发送、任务状态及 Webhook
- 明确插件只用于功能演示，外部业务系统可以自行维护订单、客服和自动化流程

### Changed

- 将 `/api/tool/*` 定位为已有内部工具的兼容接口，新业务优先使用按开放应用隔离的 `/api/open/*`

### Validation

- 已执行桥接文档页面定向测试 2 项、Markdown 解析检查、Python 语法检查和 `git diff --check`
- 未执行完整历史回归或真实第三方业务系统联调

## 2026-09-01

### Added

- 新增插件通用 HTTP、状态存储、限流、标准回复、统一调度和可信租户运行上下文
- 完成 36 个 A-BOT 业务插件的 SaaS 原生迁移，并保留 2 个 SaaS 自有业务插件
- 新增插件敏感配置 AES-GCM 密文、表单分离、明文清理和交付完整性校验脚本

### Changed

- LLM、Dify、解析服务、NewAPI、MaiBot 等第三方地址和凭证改由租户插件绑定维护
- 插件只通过通用渠道与标准出站能力执行，不直接调用企业微信、微信或 Telegram Provider
- 部署脚本改为插件与主密钥预检、Alembic 差异迁移、敏感明文清理、进程替换和双健康探针

### Removed

- 删除 A-BOT 数据库、Redis、Provider、微信 XML、单机管理入口和占位执行器兼容链路
- 删除 `plugin_manager`、`stats_collector`、`system_updater`、`trendradar_permission` 等平台原生插件目录
- 删除部署前数据库备份命令和首次部署跳过门禁变量

### Security

- 插件敏感配置使用独立 `ABOT_PLUGIN_SECRET_KEY`，密文同时绑定租户和插件绑定主键
- 目录配置敏感默认值保持为空，运行日志、任务载荷和页面不回显租户凭证
- 配额调整、领券、积分、排行和修仙等高副作用操作使用可信身份、限流、幂等和事务边界

### Validation

- 插件交付校验确认 38 个目录、36 个目标插件、2 个 SaaS 自有插件和 0 个静态错误
- 已执行插件目录、运行密钥、平台原生边界、运维和大盘相关轻量组合验证
- 已执行 Python 语法检查、Jinja 模板解析和差异检查
- 未执行完整历史回归、性能测试或真实第三方账号联调

## 2026-08-29

### Added

- 新增“系统运维 > 代理池”，支持平台代理新增、编辑、CSV 导入、启停和端口检查
- 新增单次扫码登录代理会话，支持智能选择、自有 SOCKS 代理和直接登录
- 新增平台代理独占租约、同地区最多三次取码轮换、二维码就绪锁定和终态释放

### Changed

- 微信 iPad 首次接入和账号重新登录统一使用“选择网络、扫码、手机确认、登录成功”业务流程
- 扫码接口只返回业务状态、二维码地址、轮询标记和脱敏网络标签
- 微信 iPad 账号详情只保留账号状态、消息、联系人和群聊，不再展示协议诊断信息
- 协议服务地址继续由平台在“系统运维 > 协议服务”统一维护，租户不填写地址或凭证
- 消息、会话、插件、平台日志和企微联系人筛选区统一改为紧凑工具栏，筛选条件并入结果区域
- 820px 以下视口默认折叠未生效的筛选条件，存在有效条件时保持展开，优先展示列表和日志内容

### Removed

- 删除渠道长期代理字段、代理密码保留逻辑和 `adapter_config` 代理读取路径
- 删除租户侧登录初始化、状态同步、二维码刷新、退出和重连技术路由
- 删除依赖旧二维码诊断面板和旧登录动作的历史页面测试

### Security

- 平台代理密码和自有代理密码均加密保存，页面、接口、审计和异常不返回原文
- 同一个平台代理同一时间只分配给一个有效扫码会话
- 代理只绑定当前扫码会话，下次登录可重新选择；自有代理密码在终态立即清除
- 二维码生成后固定当前代理，更换网络必须重新开始并生成新二维码

### Validation

- 已执行代理模型、代理池服务、平台代理页面、渠道页面和 874 登录编排定向测试，共 60 项
- 已执行 Swiss UI 紧凑筛选结构定向测试，共 16 项，并解析 5 个改造模板
- 已执行 Python 语法检查、旧路径检索和差异检查
- 未执行完整历史回归、性能测试或真实微信账号扫码与消息收发验收

## 2026-08-27

### Added

- 新增本地托管的 Albert Sans 与 Noto Sans SC 可变字体，按 Unicode 分片加载完整中文字符
- 新增字体来源和 SIL Open Font License 许可说明
- 新增页面区、编辑表面和独立工具面板三类明确视觉语义

### Changed

- 全站正文统一为 `15px` 轻字重排版，重建暖灰白、松绿、峡湾蓝和木色视觉 token
- 企业后台与平台后台统一降低顶栏、导航激活态、页头和二级导航的视觉重量
- 企业总览、平台总览、账号接入、员工管理和套餐管理改为开放页面布局，减少连续白色卡片和纵向网格线
- 指标、表格、记录列表、状态徽标和表单统一使用更疏的间距与更低透明度边界
- 营销首页强化 Fulcrum AI 品牌标题、真实产品背景、区块留白和价格方案层级
- 移动端保持 `15px` 正文和稳定触控尺寸，表格只在自身容器滚动，公开导航隐藏滚动条

### Validation

- 已执行斯堪的纳维亚界面定向检查，共 11 项
- 已执行平台与租户管理路由定向测试，共 20 项
- 已使用真实 Chromium 检查营销首页、企业总览、平台总览、账号接入和员工管理的桌面与移动视图，共 9 个视图
- 已确认所有检查视图无整页横向溢出，本地 Noto Sans SC 字体实际加载，桌面与移动正文均为 `15px`
- 未执行完整历史回归、性能测试或真实第三方账号联调

## 2026-08-22

### Added

- 新增企业微信、微信 iPad、Windows 微信和 Telegram 的统一三步账号接入向导
- 新增账号草稿验证、可信身份确认以及机器人、渠道、额度绑定原子启用流程
- 新增桌面和移动端账号接入页面的布局边界检查

### Changed

- 企业总览五组双列业务卡片改由专用网格统一间距和同排高度，移除相邻卡片规则造成的右侧下移
- 平台概览指标统一标签、数值和说明的纵向排列，说明文案长度不同时保持一致阅读基线
- 企业账号清单收敛为平台、状态、机器人、最近在线和额度状态等业务字段
- 企业微信在普通租户页面统一使用业务名称，不再展示 `Hook` 等内部实现术语
- 账号高级设置只保留名称和运行配置，不能更换机器人、可信账号、执行客户端或启用状态
- 企业微信客户端页面只展示已关联账号，商业绑定释放统一由平台管理员处理

### Removed

- 删除旧 `POST /channels` 手工渠道创建入口和独立提交链路
- 删除租户侧企业微信客户端解绑路由与页面按钮
- 删除高级设置中的机器人、执行账号和启用状态编辑控件

### Security

- 草稿启用必须存在当前租户、机器人、渠道和适配类型完全匹配的有效 `BotBinding`
- 账号启停只允许企业管理员操作，并强制校验租户 CSRF
- 页面不回显代理密码、稳定账号键和原始适配配置，保存时保留未展示的服务端配置

### Validation

- 已执行斯堪的纳维亚界面定向检查，共 11 项
- 已使用真实 Chromium 检查企业总览和平台概览的 `1536x864`、`390x844` 视口，卡片对齐、移动单列和页面溢出检查均通过
- 已执行账号接入、渠道页面、企业微信客户端、套餐页面、通用首页和渠道目录定向测试，共 43 项
- 已执行关键 Python 文件语法检查、差异检查，以及 `1536x864`、`390x844` 两种视口下 7 个账号接入页面检查
- 额外抽查的 2 项历史群聊测试在夹具创建缺少 `tenant_id` 时失败，未进入本次文案逻辑，按历史测试债务记录
- 未执行完整历史回归、完整端到端测试、性能测试或真实第三方账号联调

## 2026-08-21

### Added

- 新增企业微信、微信 iPad 协议、Windows 微信和 Telegram 的统一渠道目录
- 新增目录驱动的租户渠道创建、编辑和启用校验
- 新增租户隔离渠道运行协调器，恢复 Telegram 拉取及微信 iPad 消息同步、心跳和登录恢复

### Changed

- 渠道适配器未知类型不再降级到通用 HTTP 实现
- 企业微信执行端绑定收口为渠道专项能力，非远端执行渠道不保存节点或账号主键
- 渠道类型创建后不可修改，敏感凭证编辑留空时保留已保存密文
- 企业首页、机器人页面和公开宣传页改为全渠道统计与描述
- Worker 删除使用全局业务会话扫描 Telegram 和微信 iPad 渠道的旧路径

### Security

- 后台主动渠道任务先校验租户和订阅，再在独立 `tenant_scope` 与租户会话中执行
- 单项协议任务异常只回滚当前任务，不影响其他租户和渠道

### Validation

- 已执行渠道目录、页面、Provider 绑定、通用首页、公开营销页、租户运行协调器和机器接口定向测试
- 未执行完整历史回归、完整端到端测试或真实第三方账号联调

## 2026-03-31

### Added

- WeChat Windows UIA provider debug probes for message page, message rows, chat history, and avatar click inspection
- new debug endpoints for inspecting UIA message page descendants and message contexts
- `uiautomation` dependency for standalone UIA probing scripts

### Changed

- UIA current chat collector now exposes reusable control payload, parent-chain, and sibling context helpers for debugging
- UIA provider README expanded with probe usage and output file guidance

## 2026-03-26

### Added

- `.env.example` for common local configuration
- Docker deployment support with `Dockerfile`
- `docker-compose.yml` with bundled MariaDB service
- `.env.docker.example` for container deployment
- MariaDB connection support through `DATABASE_URL`
- database pool configuration for non-SQLite backends
- standalone deployment guide at [`docs/deployment.md`](docs/deployment.md)

### Changed

- README rewritten into a standard project-oriented document
- Dify plugin now supports media reply normalization for image/video outputs
- Telegram image sending now supports `base64` payload upload
- initialization flow no longer auto-seeds demo bot/channel/chatroom data

### Notes

- SQLite remains available for development
- MariaDB is recommended for larger production workloads

## 2026-03-25

### Added

- `daily_news` plugin with scheduled push support
- message summary AI-only rewrite with image rendering flow
- weather scheduled push support
- group member leave/rejoin state handling
- chatroom member last active time support

### Changed

- plugin config merge behavior improved with deep merge support
- Telegram outbound image compatibility improved
- multiple plugin command texts normalized to regular Chinese literals

## Earlier

Earlier exploratory and migration-stage notes remain in the `docs/` directory.  
This changelog is intended to track stable project-facing changes going forward.
