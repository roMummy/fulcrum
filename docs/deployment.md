# Deployment Guide

## Overview

`Multi Bot Admin` supports two common deployment modes:

- Local process deployment with Python virtualenv
- Container deployment with Docker Compose

The application can run on either:

- SQLite
- MariaDB

For `wechat_ipad`, the repository also includes a vendored provider directory copied from the `abot` project. It is recommended to deploy this provider separately and then configure its `API Base` in the admin system.

Dedicated guide:

- [Wechat iPad Provider 部署说明](/d:/learn/multi-bot-admin/docs/wechat_ipad_provider.md)

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

## First-Time Setup Wizard

If the system starts without any enabled admin account, it will automatically redirect to `/setup`.

The wizard supports:

- built-in SQLite for quick start
- MariaDB for production deployment
- admin account initialization
- automatic `.env` generation

Behavior notes:

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
uvicorn app.main:app --host 0.0.0.0 --port 8180
```

### Windows PowerShell

```powershell
python -m venv .venv
.\.venv\Scripts\Activate.ps1
pip install -r requirements.txt
Copy-Item .env.example .env
python -m uvicorn app.main:app --host 0.0.0.0 --port 8180
```

After the first startup, open the web page and complete the `/setup` wizard.

## Docker Deployment

The repository already includes:

- `Dockerfile`
- `docker-compose.yml`
- `.env.docker.example`

### Start with Docker Compose

```bash
cp .env.docker.example .env.docker
docker compose up -d --build
```

The default compose stack starts:

- `app`
- `mariadb`

After the containers are ready, open the admin page and finish the `/setup` wizard to initialize the database and administrator account.

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

1. initialize database schema
2. run runtime schema patching
3. seed auth users if configured
4. sync plugins from `plugins/`
5. start background workers

## Notes

- Current schema evolution is based on `create_all + runtime patching`
- Formal database migrations are not yet introduced
- For long-term production maintenance, adding `Alembic` is strongly recommended
