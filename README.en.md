# Fulcrum

[简体中文](README.md) | **English**

Fulcrum is a multi-tenant SaaS for operating chat bots across channels from a single back office:
WeChat, WeCom (WeChat Work), Telegram and more. It handles tenants, teams, plans and quotas, a plugin runtime,
group management, a message center and an Open API/Webhook gateway for your own business systems.

> The detailed documentation is written in Chinese (see [README.md](README.md) and [docs/](docs/)).
> This page is a short English overview.

## Features

- **Multi-tenant by design** — tenants, roles, per-tenant data isolation, CSRF and session protection.
- **Channels** — a unified adapter layer for WeChat (iPad protocol, phone bridge, desktop automation), WeCom and Telegram.
- **Plugin runtime** — directory-based plugins (`config.toml` + `plugin.py`) with per-bot and per-group scopes,
  encrypted per-binding secrets and scheduled actions.
- **Group features** — AI chat with long-term memory and web search, red-packet/transfer handling, points, and more.
- **Message center & Moments** — a WeChat-style message center and a Moments viewer with on-demand media loading.
- **Commercial loop** — plans, quotas, manual payment orders, review and subscription fulfilment.
- **Open API / Webhook** — send messages and files, query conversations and groups, receive signed webhooks.
- **Operations** — database lifecycle checks (`/readyz` stays 503 until migrations match), alerts and dashboards.

## Tech stack

Python 3.11, FastAPI, SQLAlchemy 2, Alembic, Jinja2, Uvicorn, SQLite or MariaDB.

## Quick start

```bash
python3.11 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env
alembic upgrade head
uvicorn app.main:app --host 127.0.0.1 --port 8280
```

Then open <http://127.0.0.1:8280>. For production use Docker Compose (see [docs/deployment.md](docs/deployment.md))
behind a TLS reverse proxy.

## Security

- `SECRET_KEY` must be a random string of at least 32 characters. Outside development
  (`APP_ENV` not in `dev/local/demo/test`) the app **refuses to start** with a placeholder or short key.
- Plugin secrets use a separate AES-GCM master key (`ABOT_PLUGIN_SECRET_KEY`).
- Report vulnerabilities privately — see [SECURITY.md](SECURITY.md).

## Not included

Client-side providers for WeChat/WeCom (hook, iPad protocol, desktop automation) are separate programs that depend on
third-party components and are **not distributed in this repository**. Using unofficial protocols may violate the
terms of the platforms involved; evaluate the risk and comply with applicable laws.

## Contributing & license

See [CONTRIBUTING.md](CONTRIBUTING.md). Licensed under the [Apache License 2.0](LICENSE).
