# 外部 MariaDB 切换实施计划

> **For Claude:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task.

**Goal:** 将线上 `multi-bot-saas` 从 Compose 内置 MariaDB 切换到外部 MariaDB，同时完整保留现有平台账号、演示租户和业务数据，并保留原数据库作为回滚源。

**Architecture:** 应用仍通过统一的 `DATABASE_URL` 使用 SQLAlchemy 和 PyMySQL 访问数据库，不新增对具体业务 Provider 的依赖。切换采用“旧库一致性备份 -> 目标库建库 -> 全量导入 -> 修改未提交运行配置 -> 仅重建应用容器”的顺序，内置 MariaDB 容器暂不删除，便于快速回滚。

**Tech Stack:** Docker Compose、MariaDB 10.11/11.4、SQLAlchemy、Alembic、FastAPI。

---

### Task 1: 执行只读前置检查

**Files:**
- Inspect: `.run/production.env`
- Inspect: `docker-compose.yml`
- Inspect: `alembic.ini`

**Step 1: 检查网络和认证**

从部署主机和应用容器检查目标 MariaDB 的 `3306` 端口，并使用临时环境变量验证数据库账号可以执行只读查询。输出只能包含版本、连通状态和数据库是否存在，禁止输出密码或完整连接串。

**Step 2: 检查当前运行状态**

记录应用容器 ID、镜像 ID、当前数据库主机和库名、Alembic revision、Git HEAD，以及 `/healthz`、`/readyz` 的结果。

### Task 2: 创建可回滚备份

**Files:**
- Create: `/data/backups/multi-bot-saas/<timestamp>/production.env`
- Create: `/data/backups/multi-bot-saas/<timestamp>/multi_bot_saas.sql.gz`
- Create: `/data/backups/multi-bot-saas/<timestamp>/metadata.txt`

**Step 1: 备份未提交配置**

复制当前生产环境文件并设置目录与文件权限，避免其他本机用户读取数据库连接和认证配置。

**Step 2: 导出当前数据库**

使用 `mariadb-dump --single-transaction --routines --triggers` 导出当前 `multi_bot_saas`，压缩后校验文件非空，并记录源库 Alembic revision。

### Task 3: 创建并导入目标数据库

**Files:**
- Modify: external MariaDB database `db_multi_bot_saas`

**Step 1: 创建数据库**

在目标 MariaDB 上以 `utf8mb4` 字符集和 `utf8mb4_unicode_ci` 排序规则创建 `db_multi_bot_saas`。创建前再次确认目标库不存在，避免覆盖未知数据。

**Step 2: 导入备份**

将源库 SQL 导入目标库，随后核对 Alembic revision、关键表数量以及平台账号、租户、订阅和机器人绑定记录数量。

### Task 4: 切换应用连接

**Files:**
- Modify: `.run/production.env`

**Step 1: 更新运行配置**

将 `DATABASE_URL` 更新为外部 MariaDB 连接。真实密码只写入权限为 `600` 的未提交环境文件，不写入 Git、日志、实施文档或命令输出。

**Step 2: 重建应用容器**

保持内置 MariaDB 容器运行，仅通过 `docker compose up -d --no-deps --force-recreate app` 重新创建应用容器，减少停机范围并保留旧库回滚能力。

### Task 5: 执行轻量验证

**Files:**
- Test: `scripts/saas_smoke_test.sh`

**Step 1: 校验迁移状态**

在新应用容器中执行 `alembic current`，确认目标数据库位于代码仓库唯一的 Alembic head。

**Step 2: 校验核心入口**

验证 `/healthz`、`/readyz`、`/`、`/pricing`、`/register`、`/login` 和 `/platform/login`。使用现有演示账号执行一次真实登录，确认 Cookie 会话和控制台访问正常。

**Step 3: 检查日志和旧服务**

检查本次重建后的应用日志是否包含 ERROR、CRITICAL、异常堆栈、5xx 或疑似凭据；同时确认旧 `8180` 服务仍然健康且未重启。

### Task 6: 回滚步骤

若新数据库连接、迁移 revision 或登录验证失败，立即恢复备份的 `.run/production.env`，只重建应用容器并重新验证 `/healthz`、`/readyz` 和登录。不得删除目标数据库或旧 MariaDB 数据卷，后续清理由独立审批处理。
