# Changelog

All notable changes to this project should be documented in this file.

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
