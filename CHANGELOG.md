# Changelog

All notable changes to this project should be documented in this file.

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
- 营销首页强化曜枢 AI 品牌标题、真实产品背景、区块留白和价格方案层级
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
- standalone deployment guide at [`docs/deployment.md`](/d:/learn/multi-bot-admin/docs/deployment.md)

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
