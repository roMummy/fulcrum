# 曜枢 AI

`曜枢 AI` 是本项目的对外品牌，内部仓库、Python 包、数据库和部署标识继续使用 `multi-bot-saas`。产品面向企业提供全渠道智能机器人运营、租户隔离、团队协作、套餐计费和通用机器人额度；渠道执行通过抽象接口接入，不把商业模型绑定到具体 Provider。

## 功能特性

- 公开站点：曜枢 AI 营销首页、套餐页、企业自助注册和 7 天试用
- 商业闭环：人工付款订单、私有付款凭证、平台审核和订阅自动履约
- 通用额度：按 `bot_limit` 管理机器人数量，以 `adapter_type` 和 `bot_account_id` 标识外部账号
- 租户管理：企业主手机号登录、员工独立登录、角色权限、会话和 CSRF 防护
- 平台管理：套餐、租户、订阅、订单审核、受控解绑和平台审计
- 执行接入：通过通用适配能力扩展渠道；当前随仓库交付的成熟 Windows 执行端是企业微信 Provider
- Provider 节点：一次性激活码、设备凭证、心跳、长轮询、任务租约和多账号绑定
- 业务数据：机器人、渠道、联系人、群聊、会话、消息、插件绑定和任务按租户强制隔离
- 数据库生命周期：生产只接受 Alembic head，数据库未就绪时 `/readyz` 返回 503 且不启动 Worker
- 旧系统迁移：只读来源、dry-run、外键重映射、批次审计和重复导入防护

## 技术栈

- Python 3.11
- FastAPI
- SQLAlchemy 2.x
- Jinja2
- Uvicorn
- SQLite / MariaDB

## 目录结构

```text
multi-bot-saas/
├── app/                  # 应用主代码
│   ├── core/             # 配置、日志、数据库
│   ├── models/           # SQLAlchemy 模型
│   ├── schemas/          # 表单/请求结构
│   ├── services/         # 核心业务逻辑
│   ├── static/           # 静态资源
│   ├── templates/        # 页面模板
│   └── main.py           # FastAPI 入口
├── plugins/              # 目录驱动插件
├── tests/                # 测试
├── data/                 # SQLite 数据文件等本地数据
├── logs/                 # 日志目录
├── requirements.txt
├── Dockerfile
├── docker-compose.yml
└── README.md
```

## 环境要求

- Python 3.11+
- Windows / Linux 均可
- 如果需要 Dify 视频回发封面提取，建议确保系统可用 `ffmpeg`

## 快速开始

### 1. 创建虚拟环境

Linux / macOS:

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements-dev.txt
```

Windows PowerShell:

```powershell
python -m venv .venv
.\.venv\Scripts\Activate.ps1
pip install -r requirements-dev.txt
```

### 2. 启动服务

Linux / macOS:

```bash
uvicorn app.main:app --host 0.0.0.0 --port 8280
```

Windows:

```powershell
python -m uvicorn app.main:app --host 0.0.0.0 --port 8280
```

开发模式可加 `--reload`。

访问：

```text
http://127.0.0.1:8280
```

主要入口：

| 路径 | 用途 |
| --- | --- |
| `/` | 曜枢 AI 营销首页 |
| `/pricing` | 公开套餐价格 |
| `/register` | 企业自助注册与 7 天试用 |
| `/login` | 企业主手机号登录 |
| `/employee/login` | 员工使用企业编码和用户名登录 |
| `/platform/login` | 平台管理员登录 |

### 一键更新并重启

Linux / macOS 可使用：

```bash
chmod +x scripts/deploy_restart_mba.sh
./scripts/deploy_restart_mba.sh
```

脚本会自动执行：

- 拉取 `origin/main` 最新代码
- 安装或更新依赖
- 检查数据库备份门禁
- 在旧进程仍运行时执行 `alembic upgrade head`
- 迁移成功后关闭旧的 `uvicorn` 进程
- 重新启动服务
- 检查 `/healthz` 和 `/readyz`

可选环境变量：

```bash
MBS_GIT_BRANCH=main \
MBS_PORT=8280 \
MBS_DB_BACKUP_COMMAND='/opt/backup/multi-bot-saas-db.sh' \
./scripts/deploy_restart_mba.sh
```

首次部署到确认过的独立空库时可显式设置 `MBS_INITIAL_DEPLOY=1`。已有数据库升级不得用该变量跳过备份门禁。

### 3. 初始化数据库和平台管理员

首次启动前必须先升级数据库：

```bash
.venv/bin/alembic upgrade head
```

数据库达到 head 后启动服务。系统中没有平台管理员时会进入 `/setup`：

- 页面只创建 `platform_users` 平台管理员；
- 不切换数据库、不建表、不写 `.env`；
- 也可以通过 `AUTH_BOOTSTRAP_ADMIN_*` 受限环境变量自动创建首个平台管理员；
- 迁移会初始化试用和三档公开套餐；平台管理员可继续调整价格、额度和公开状态；
- 企业客户可从 `/register` 自助创建租户、企业所有者和试用订阅，平台管理员仍可在后台管理租户。

### 4. 配置环境变量

如果你不想使用引导页，也可以手动复制示例文件：

```bash
cp .env.example .env
```

Windows:

```powershell
Copy-Item .env.example .env
```

常见配置示例：

```env
APP_ENV=prod
SECRET_KEY=change-this-secret-key
AUTH_SEED_DEMO_USERS=false
AUTH_BOOTSTRAP_ADMIN_USERNAME=admin
AUTH_BOOTSTRAP_ADMIN_PASSWORD=change-this-password
BILLING_PAYMENT_TITLE=曜枢 AI 人工付款
BILLING_PAYMENT_INSTRUCTIONS=请完成转账后填写交易流水号并上传付款凭证。
```

## 配置说明

项目使用 `pydantic-settings` 读取配置，来源优先级通常为：

1. 系统环境变量
2. 项目根目录 `.env`
3. 代码默认值

主要配置项：

| 配置项 | 说明 |
| --- | --- |
| `APP_ENV` | 运行环境，常用 `dev` / `prod` |
| `SECRET_KEY` | 用于认证、敏感摘要和设备指纹 HMAC；部署后必须保持稳定 |
| `DATABASE_URL` | 数据库连接串 |
| `LOG_LEVEL` | 日志级别 |
| `AUTH_SEED_DEMO_USERS` | 是否自动创建演示账号 |
| `AUTH_BOOTSTRAP_ADMIN_USERNAME` | 初始化管理员用户名 |
| `AUTH_BOOTSTRAP_ADMIN_PASSWORD` | 初始化管理员密码 |
| `BILLING_PAYMENT_TITLE` | 租户订单页展示的人工付款标题 |
| `BILLING_PAYMENT_INSTRUCTIONS` | 人工付款说明，不应包含一次性敏感凭据 |
| `BILLING_PAYMENT_QR_IMAGE` | 可选收款二维码静态地址 |
| `BILLING_PAYMENT_BANK_DETAILS` | 可选银行转账说明 |
| `BILLING_PAYMENT_PROOF_MAX_BYTES` | 单个付款凭证最大字节数，默认 5 MiB |

示例见 [`.env.example`](/data/multi-bot-saas/.env.example)。

## 套餐、试用与付款

迁移默认创建以下公开套餐，真实页面和订单金额始终读取数据库，平台管理员可以调整：

| 套餐 | 机器人额度 | 月付 | 年付 |
| --- | ---: | ---: | ---: |
| 入门版 | 1 | ¥99 | ¥990 |
| 标准版 | 3 | ¥259 | ¥2590 |
| 专业版 | 10 | ¥699 | ¥6990 |

注册后自动开通 7 天、1 个机器人额度的试用。首版付款采用人工审核：企业主创建订单并上传流水号和凭证，平台管理员在 `/platform/billing/orders` 审核；审核通过后系统在同一事务内结束旧订阅、创建新订阅并固化订单额度。

付款凭证不会进入公开静态目录，文件保存于 `DATA_DIR/tenant_billing/<tenant_id>/<order_no>/`，文件名由服务端随机生成，只能通过租户或平台鉴权路由下载。备份数据库时必须同时备份该目录，否则订单记录与付款凭证会失去对应关系。

设备原始指纹不会落库，只保存基于 `SECRET_KEY` 的 HMAC-SHA256 摘要。同一设备只能领取一次试用，一个机器人账号只能归属一个企业；试用绑定不能由租户自行更换，付费绑定也只能由平台管理员带原因释放。

## 数据库

### SQLite

开发环境推荐继续使用 SQLite，配置简单、启动快：

```env
DATABASE_URL=sqlite:///./data/app.db
```

### MariaDB

如果消息规模会到几十万甚至更多，生产环境更建议使用 MariaDB：

```env
DATABASE_URL=mysql+pymysql://multi_bot_saas:change-this-password@127.0.0.1:3306/multi_bot_saas?charset=utf8mb4
DATABASE_POOL_SIZE=10
DATABASE_MAX_OVERFLOW=20
DATABASE_POOL_RECYCLE_SECONDS=1800
DATABASE_POOL_TIMEOUT_SECONDS=30
```

### 当前数据库初始化方式

生产数据库结构全部由 Alembic 管理。应用启动只校验当前 revision 是否等于唯一 head；不再执行 `Base.metadata.create_all()` 或运行时 `ALTER TABLE`。开发和测试临时库可以使用显式辅助函数，但该能力在生产环境禁用。

### 首次安装默认行为

如果数据库 revision 未到 head，`/healthz` 仍用于存活检查，`/readyz` 返回 503，业务请求被拒绝且 Worker 不启动。数据库已迁移但没有平台管理员时，系统才允许进入 `/setup` 创建首个平台身份。

## Docker 部署

项目已提供：

- [Dockerfile](/data/multi-bot-saas/Dockerfile)
- [docker-compose.yml](/data/multi-bot-saas/docker-compose.yml)
- [`.env.docker.example`](/data/multi-bot-saas/.env.docker.example)

### 使用 Docker Compose

```bash
cp .env.docker.example .env.docker
docker compose --env-file .env.docker up -d --build
```

默认会启动：

- `app`
- `mariadb`

如果你想改用 SQLite，只需要把 `.env.docker` 里的 `DATABASE_URL` 改成：

```env
DATABASE_URL=sqlite:////app/data/app.db
```

## 插件系统

插件位于 `plugins/<plugin_code>/` 目录下，当前项目已接入的目录插件包括：

- `dify`
- `message_summary`
- `daily_news`
- `douyu`
- `weather`
- `music`
- `guess_song`
- `group_member_change`
- `inactive_rank`
- `message_push_task`
- `ai_auto_response`
- `group_auto_invite`
- `group_virtual`
- `game_task`
- `jd_sign_token`
- `lucky_pot`
- `message_sign`
- `point_trade`
- `douyin_parser`
- `duanju_search`
- `global_news`

插件由后台启动时自动扫描并同步到数据库。

## 渠道与适配边界

套餐、订单、试用和机器人额度只依赖通用 `adapter_type`、`bot_account_id` 与执行能力接口，不依赖 Provider 数据模型。新增渠道时应实现适配接口并复用通用额度校验，不能把具体 Provider 客户端导入商业服务。

当前仓库已完成并可实际运行的 Windows 执行端仍是企业微信 Provider。节点通过一次性激活码换取设备凭证，随后主动上报心跳、发现账号并长轮询领取绑定渠道的任务；其他渠道应沿相同抽象边界扩展，而不是复制计费和租户逻辑。

### 跨机器发送图片和文件

如果 `Multi Bot SaaS` 和 Windows provider 不在同一台机器，推荐使用“主服务中转文件”的方式：

1. 把文件上传到主服务 `POST /channels/{channel_id}/outbound/upload`
2. 主服务返回一个公开下载地址 `download_url`
3. 再调用 `POST /channels/{channel_id}/outbound`，把这个 `download_url` 作为 `file_url` 或 `image_url` 发给 provider

上传接口使用 `multipart/form-data`，字段：

- `file`: 实际文件
- `message_type`: `file` 或 `image`

返回示例：

```json
{
  "ok": true,
  "download_url": "http://192.168.2.142:8280/outbound-files/3/abc123/report.pdf",
  "payload": {
    "message_type": "file",
    "file_name": "report.pdf",
    "file_url": "http://192.168.2.142:8280/outbound-files/3/abc123/report.pdf"
  }
}
```

随后可直接出站：

```json
{
  "external_id": "group:测试群",
  "message_type": "file",
  "file_name": "report.pdf",
  "file_url": "http://192.168.2.142:8280/outbound-files/3/abc123/report.pdf"
}
```

如果你想一步完成“上传并发送”，可以直接调用：

`POST /channels/{channel_id}/outbound/upload-and-send`

使用 `multipart/form-data`：

- `file`: 实际文件，例如 `report.xlsx`
- `external_id`: 目标群，例如 `group:测试群`
- `conversation_title`: 可选，例如 `测试群`
- `chat_type`: 一般填 `group`
- `message_type`: `file` 或 `image`

Excel 发群示例：

```powershell
curl -X POST "http://192.168.2.142:8280/channels/3/outbound/upload-and-send" `
  -H "Cookie: 你的后台登录cookie" `
  -F "external_id=group:测试群" `
  -F "conversation_title=测试群" `
  -F "chat_type=group" `
  -F "message_type=file" `
  -F "file=@D:\tmp\report.xlsx"
```

返回里会包含：

- `job_id`
- `conversation_id`
- `download_url`
- 最终入队的 `payload`

### 第三方工具 API

如果你希望让 OSM 之类的第三方系统直接接入，而不依赖后台登录 Cookie，可以使用工具 API：

- [Tool API For OSM](/data/multi-bot-saas/docs/tool_api_for_osm.md)

这组 API 使用 `X-API-Key` 鉴权，典型用途：

- 上传 Excel 并发送到指定群
- 查询发送任务状态
- 查询指定群的新消息

### workwechat Provider

仓库里附带了企业微信 Hook 高层 provider：

- [vendors/workwechat_provider](/data/multi-bot-saas/vendors/workwechat_provider)
- [WorkWechat Provider 接入说明](/data/multi-bot-saas/docs/workwechat_provider.md)

推荐使用方式：

- 启动 `workwechat_provider`，它会自动拉起内置 Hook runtime
- 在后台新增 `channel_type=workwechat` 的渠道，并把 `API Base` 指向 `workwechat_provider`

## 测试

测试前必须从进程环境指定 `.run/test-runtime` 数据库，避免测试模块收集顺序让全局 Settings 误连开发库：

```bash
APP_ENV=test \
DATA_DIR="$PWD/.run/test-runtime/full" \
DATABASE_URL="sqlite:///$PWD/.run/test-runtime/full/test.db" \
LOG_DIR="$PWD/.run/test-runtime/full/logs" \
.venv/bin/python -m pytest -q
```

## 开发说明

- 启动时会自动初始化数据库并启动后台 worker
- 后台 worker 会处理：
  - 插件派发
  - 出站消息发送
  - Telegram 轮询
  - 微信心跳与消息同步
  - 定时新闻、天气、订阅类任务
- 插件配置与运行态信息会存入数据库
- 敏感信息如 bot token 会基于 `SECRET_KEY` 做简单加密存储

## 常见文件

- [main.py](/data/multi-bot-saas/app/main.py): FastAPI 入口
- [db.py](/data/multi-bot-saas/app/core/db.py): 数据库引擎与会话
- [settings.py](/data/multi-bot-saas/app/core/settings.py): 配置入口
- [entities.py](/data/multi-bot-saas/app/models/entities.py): 数据模型
- [plugin_directory.py](/data/multi-bot-saas/app/services/plugin_directory.py): 目录插件扫描与调度
- [async_workers.py](/data/multi-bot-saas/app/services/async_workers.py): 后台任务与轮询

## 生产建议

- 生产环境关闭 `AUTH_SEED_DEMO_USERS`
- 使用高强度且长期稳定的 `SECRET_KEY`，轮换前必须设计设备摘要迁移方案
- 数据量大时切换到 MariaDB
- 为 `logs/`、数据库和 `data/tenant_billing/` 做好持久化与一致性备份
- 反向代理后再对外暴露服务
- 对 Telegram 等外部渠道单独准备稳定网络出口或代理

## License

当前仓库未单独声明开源许可证。如需对外发布，请补充正式 License 文件。
