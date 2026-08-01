# Multi Bot SaaS 部署指南

## Overview

`Multi Bot SaaS` 支持两种部署模式：

- Local process deployment with Python virtualenv
- Container deployment with Docker Compose

The application can run on either:

- SQLite
- MariaDB

SaaS 首期只开放企业微信。云端应用统一部署，客户 Windows PC 部署企业微信 Provider，并通过 HTTPS 主动连接云端，不要求客户开放入站端口。

For development, SQLite is sufficient.  
For production, especially when message volume is expected to grow to hundreds of thousands of rows or more, MariaDB is recommended.

## Environment Variables

The application reads configuration from:

1. OS environment variables
2. `.env`
3. default values in code

Typical production settings:

```env
APP_ENV=prod
SECRET_KEY=change-this-secret-key
LOG_LEVEL=INFO
AUTH_SEED_DEMO_USERS=false
AUTH_BOOTSTRAP_ADMIN_USERNAME=admin
AUTH_BOOTSTRAP_ADMIN_PASSWORD=change-this-password
```

## 首次数据库和平台管理员初始化

生产启动不会自动建表或补列。首次部署必须先执行：

```bash
.venv/bin/alembic upgrade head
```

数据库达到 Alembic head 后，若没有启用的平台管理员，访问后台会进入 `/setup`。该页面只创建平台管理员，不修改 `DATABASE_URL`、不生成 `SECRET_KEY`、不写 `.env`。

也可以在受限环境中配置 `AUTH_BOOTSTRAP_ADMIN_USERNAME`、`AUTH_BOOTSTRAP_ADMIN_PASSWORD` 和 `AUTH_BOOTSTRAP_ADMIN_DISPLAY_NAME`。密码只参与哈希，不能写入 Git、命令输出或日志。

- SQLite mode can be used immediately after setup
- MariaDB mode validates the connection, creates tables, initializes the admin, and then requires an app restart to switch the running process to the new database

## Database Configuration

### SQLite

Example:

```env
DATABASE_URL=sqlite:///./data/app.db
```

Recommended for:

- local development
- functional verification
- single-node lightweight deployment

Not recommended for:

- sustained high write throughput
- large message/event history with multiple background workers

### MariaDB

Example:

```env
DATABASE_URL=mysql+pymysql://multi_bot_admin:multi_bot_admin_pass@127.0.0.1:3306/multi_bot_admin?charset=utf8mb4
DATABASE_POOL_SIZE=10
DATABASE_MAX_OVERFLOW=20
DATABASE_POOL_RECYCLE_SECONDS=1800
DATABASE_POOL_TIMEOUT_SECONDS=30
```

Recommended for:

- production deployment
- larger message history
- better concurrency and maintenance

## Local Deployment

### Linux / macOS

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env
./.venv/bin/alembic upgrade head
uvicorn app.main:app --host 0.0.0.0 --port 8280
```

### Windows PowerShell

```powershell
python -m venv .venv
.\.venv\Scripts\Activate.ps1
pip install -r requirements.txt
Copy-Item .env.example .env
./.venv/Scripts/alembic.exe upgrade head
python -m uvicorn app.main:app --host 0.0.0.0 --port 8280
```

启动后先检查 `/healthz` 和 `/readyz`，再进入 `/setup` 或平台登录页。

## Docker Deployment

The repository already includes:

- `Dockerfile`
- `docker-compose.yml`
- `.env.docker.example`

### Start with Docker Compose

```bash
cp .env.docker.example .env.docker
docker compose build
docker compose up -d mariadb
docker compose run --rm app alembic upgrade head
docker compose up -d app
```

The default compose stack starts:

- `app`
- `mariadb`

容器启动后必须确认 `/healthz` 和 `/readyz` 都返回成功，再创建平台管理员和租户。

### Use SQLite in Docker

If you want SQLite instead of MariaDB, update `.env.docker`:

```env
DATABASE_URL=sqlite:////app/data/app.db
```

And keep the `data` directory mounted.

### Use External MariaDB

If you already have an external MariaDB instance:

1. Set `DATABASE_URL` to the external database
2. Remove or ignore the bundled `mariadb` service

## Reverse Proxy

Recommended production layout:

- app behind Nginx / Caddy / Traefik
- TLS terminated at proxy layer
- persistent storage mounted for `logs/` and database volume

## Persistent Data

Make sure the following are persisted:

- `logs/`
- `data/` when using SQLite
- MariaDB volume when using containerized MariaDB
- `plugins/` if you maintain plugins outside the image build pipeline

## Startup Behavior

On startup the application will:

1. 校验数据库 revision 等于 Alembic head；
2. 执行受控平台管理员种子、插件目录同步和必要数据回填；
3. 数据库就绪后启动后台 Worker；
4. revision 落后时保留 `/healthz`，让 `/readyz` 返回 503，并拒绝业务流量。

## Notes

- 已有数据库部署前必须备份，迁移失败时不得停止旧进程。
- `scripts/deploy_restart_mba.sh` 要求已有数据库提供 `MBS_DB_BACKUP_COMMAND`；首次独立空库部署可显式设置 `MBS_INITIAL_DEPLOY=1`。
- 旧系统数据导入见 [legacy-import.md](/data/multi-bot-saas/docs/legacy-import.md)，必须先 dry-run，再备份目标库并正式导入。
- 下载分享码是 128 位随机值，服务端同时校验固定格式、租户目录、渠道目录和解析后的真实路径，不能用可预测 ID 替代。
