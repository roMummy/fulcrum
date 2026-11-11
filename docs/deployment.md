# Fulcrum 部署指南

## 发布流程

推荐发布顺序：在目标主机的部署目录执行 `git pull --ff-only`，完成数据库迁移、依赖检查、应用重启和健康探针验证。
发布前先备份数据库；回滚应用前先判断迁移是否向后兼容。

## Overview

Fulcrum 对外提供营销、注册、试用、人工付款和通用机器人额度，部分历史部署标识（Docker 容器名、数据库名、数据卷名）沿用 `multi-bot-saas` / `multi_bot_saas`。支持两种部署模式：

- Local process deployment with Python virtualenv
- Container deployment with Docker Compose

The application can run on either:

- SQLite
- MariaDB

商业和额度服务不依赖具体 Provider。当前随仓库交付的成熟 Windows 执行端是企业微信 Provider，客户 PC 通过 HTTPS 主动连接云端，不要求客户开放入站端口；后续渠道应通过通用适配接口接入。

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
ABOT_PLUGIN_SECRET_KEY=
LOG_LEVEL=INFO
AUTH_SEED_DEMO_USERS=false
AUTH_COOKIE_SECURE=true
AUTH_BOOTSTRAP_ADMIN_USERNAME=admin
AUTH_BOOTSTRAP_ADMIN_PASSWORD=change-this-password
BILLING_PAYMENT_TITLE=Fulcrum 人工付款
BILLING_PAYMENT_INSTRUCTIONS=请完成转账后填写交易流水号并上传付款凭证。
BILLING_PAYMENT_PROOF_MAX_BYTES=5242880
# 第三方支付自动回调使用；未配置时 /api/payment/webhook 会返回 503。
PAYMENT_WEBHOOK_SECRET=change-this-payment-webhook-secret
```

`SECRET_KEY` 同时参与会话安全和设备指纹 HMAC。已经产生机器人绑定后不得直接更换该值，否则同一设备会计算出不同摘要，破坏设备归属和试用防重复判断。若必须轮换，应先实现双密钥过渡或摘要迁移。

`ABOT_PLUGIN_SECRET_KEY` 专门用于插件敏感配置 AES-GCM 加密，不能复用 `SECRET_KEY`。它必须是 32 字节 URL-safe Base64，部署后保持稳定；可使用以下命令生成：

```bash
python3 -c "import base64,secrets; print(base64.urlsafe_b64encode(secrets.token_bytes(32)).decode())"
```

`AUTH_COOKIE_SECURE` 必须与实际访问协议一致。通过正式 HTTPS 域名访问时保持 `true`；当前仅通过受控内网 `http://IP:8280` 验收时必须显式设为 `false`，否则浏览器收到登录 Cookie 后不会在后续 HTTP 请求中回传。切换到 HTTPS 后应立即恢复为 `true`。

人工付款可选配置：

```env
# BILLING_PAYMENT_QR_IMAGE=/static/images/payment-qr.png
# BILLING_PAYMENT_BANK_DETAILS=开户行及账户信息
```

二维码若使用静态路径，部署前必须确认文件进入镜像或持久化目录；银行信息和二维码不得包含短期口令或仅供内部使用的凭据。

## 首次数据库和平台管理员初始化

生产启动不会自动建表或补列。首次部署必须先执行：

```bash
.venv/bin/alembic upgrade head
```

数据库达到 Alembic head 后会存在试用、入门、标准和专业套餐。若没有启用的平台管理员，访问后台会进入 `/setup`；该页面只创建平台管理员，不修改 `DATABASE_URL`、不生成 `SECRET_KEY`、不写 `.env`。

也可以在受限环境中配置 `AUTH_BOOTSTRAP_ADMIN_USERNAME`、`AUTH_BOOTSTRAP_ADMIN_PASSWORD` 和 `AUTH_BOOTSTRAP_ADMIN_DISPLAY_NAME`。密码只参与哈希，不能写入 Git、命令输出或日志。

- SQLite 和 MariaDB 都必须先通过 Alembic 升级到唯一 head；应用运行时不会代替迁移创建或修改业务表
- 数据库迁移完成后，平台管理员可通过 `/setup` 或受限引导配置创建；初始化管理员不会切换数据库连接，也不要求通过重启完成所谓的数据库模式切换

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

启动后先检查 `/healthz` 和 `/readyz`，再验证 `/`、`/pricing`、`/register`、`/login` 和 `/platform/login`。

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

容器启动后必须确认 `/healthz` 和 `/readyz` 都返回成功，再初始化平台管理员并验证企业自助注册。

### 已有环境升级顺序

已有 MariaDB 环境必须按以下顺序执行，迁移失败时不能重建正在服务的应用容器：

1. 记录当前 Git HEAD、应用镜像和 Alembic revision；
2. 确认 `.env.docker` 已配置稳定的 `ABOT_PLUGIN_SECRET_KEY`；
3. `git pull --ff-only`，禁止覆盖远程或本地未确认改动；
4. `docker compose build app`；
5. `docker compose run --rm app python scripts/verify_plugin_migration.py`；
6. `docker compose run --rm app alembic upgrade head`；
7. `docker compose run --rm app python scripts/scrub_plugin_plaintext_secrets.py`；
8. 迁移和清理成功后执行 `docker compose up -d --no-deps app`，只重建 SaaS 应用容器；
9. 验证健康探针、公开入口、注册试用、订单审核、机器人额度和插件绑定，再清理旧镜像。

本轮开放接口扩展对应 Alembic 迁移 `20261108_0021`，包含套餐开放应用、渠道、调用、消息和媒体额度字段，以及 `open_api_usage_records` 月度账本。迁移前先确认当前 revision，再执行 `alembic upgrade head`；迁移成功后才替换应用进程。该迁移不会删除历史订单、订阅、渠道或消息记录。

部署主机如果还运行旧 `8180` 服务，不得执行整组 Compose 重启或端口清理；上线后必须单独确认旧服务仍然可用。

### 基础冒烟验证

应用就绪后运行：

```bash
./scripts/saas_smoke_test.sh http://127.0.0.1:8280
```

脚本检查存活/就绪探针、营销与套餐页、注册入口、企业主/员工/平台三类登录入口，以及 Provider 无效凭据的拒绝边界；随后使用临时数据库自动验证既有 Provider 租户隔离流程。业务冒烟不写入部署数据库。需要同时扫描应用日志时设置 `MBS_SMOKE_LOG_FILE`，脚本只报告是否出现疑似凭据，不输出匹配原文。

本地发布前还应单独运行 `tests/test_public_commercial_flow.py`。该测试使用临时 SQLite 和临时私有目录验证注册、试用、订单、付款凭证、平台审核、订阅生效和机器人额度限制，不接触部署数据库。

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

## 发布命令

非容器部署使用仓库脚本，在目标主机的部署目录执行：

```bash
cd <部署目录>
git pull --ff-only origin main
MBS_GIT_BRANCH=main MBS_PORT=8280 ./scripts/deploy_restart.sh
```

脚本会依次安装依赖、执行插件完整性探针、运行 `alembic upgrade head`、清理历史插件明文配置、重启 8280 端口的应用进程，并检查本机 `/healthz` 和 `/readyz`。迁移或密钥探针失败时旧进程不会被停止。

容器部署则在同一目录执行：

```bash
git pull --ff-only origin main
docker compose --env-file .env.docker build app
docker compose --env-file .env.docker run --rm app alembic upgrade head
docker compose --env-file .env.docker up -d --no-deps app
docker compose --env-file .env.docker ps
curl -fsS http://127.0.0.1:8280/healthz
curl -fsS http://127.0.0.1:8280/readyz
```

MariaDB 容器不需要因为本轮应用代码更新而重启。若目标主机无法访问 Git 服务，应从已有认证工作区生成增量 Git bundle 传到目标主机，再在目标目录快进到 `main`；不得把新凭据写入仓库、命令行记录或日志。

发布完成后至少验证：

- `http://127.0.0.1:8280/healthz` 返回成功；
- `http://127.0.0.1:8280/readyz` 返回成功；
- 未登录访问 `/developer/docs` 重定向 `/login`；
- 登录后文档页面包含 `/api/open/accounts`、`/api/open/messages/sync`、额度和 Webhook 章节；
- `/api/payment/webhook` 在未配置密钥时不误报成功，配置密钥后能按签名校验回调。

## Persistent Data

Make sure the following are persisted:

- `logs/`
- `data/` when using SQLite
- `data/tenant_billing/` 中的私有付款凭证（MariaDB 模式也必须持久化）
- MariaDB volume when using containerized MariaDB
- `plugins/` if you maintain plugins outside the image build pipeline

## Startup Behavior

On startup the application will:

1. 校验数据库 revision 等于 Alembic head；
2. 执行受控平台管理员种子、插件目录同步和必要数据回填；
3. 数据库就绪后启动后台 Worker；
4. revision 落后时保留 `/healthz`，让 `/readyz` 返回 503，并拒绝业务流量。

## 订单凭证持久化与恢复

企业主从 `/account/billing` 创建订单，在订单详情提交交易流水号和付款凭证。平台管理员从 `/platform/billing/orders` 检查租户、套餐、金额和凭证后批准或驳回。批准操作具备幂等保护，重复提交不能重复延长订阅。

付款凭证保存于 `DATA_DIR/tenant_billing/<tenant_id>/<order_no>/`，不通过静态目录公开。自动更新脚本不会创建数据库或文件备份；如部署环境另行实施灾难恢复，数据库与私有目录必须使用同一恢复时间点，并在启动后抽查订单凭证下载权限。

回滚应用镜像前先判断数据库迁移是否向后兼容。不能只切回旧镜像后继续写入，也不得在生产库上直接尝试未经验证的 `alembic downgrade`。

## Notes

- `scripts/deploy_restart.sh` 只做差异更新，不执行备份，也不设置备份门禁。
- 插件目录、独立主密钥、数据库迁移和敏感明文清理成功前不得停止旧进程。
- 平台可修改公开套餐价格和额度，订单创建后使用金额和额度快照，不受后续套餐修改影响。
- 一个手机号首版只拥有一个企业；试用机器人绑定不可由租户自行更换，付费绑定释放也必须由平台管理员带原因执行。
- 旧系统数据导入见 [legacy-import.md](../docs/legacy-import.md)，必须先 dry-run，再备份目标库并正式导入。
- 下载分享码是 128 位随机值，服务端同时校验固定格式、租户目录、渠道目录和解析后的真实路径，不能用可预测 ID 替代。
