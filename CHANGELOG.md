# Changelog

All notable changes to this project should be documented in this file.

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
