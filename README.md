# Fulcrum

**简体中文** | [English](README.en.md)

[![Stars](https://img.shields.io/github/stars/liu-x-x-lab/fulcrum?style=flat&logo=github)](https://github.com/liu-x-x-lab/fulcrum/stargazers)
[![License](https://img.shields.io/github/license/liu-x-x-lab/fulcrum)](LICENSE)
[![CI](https://github.com/liu-x-x-lab/fulcrum/actions/workflows/ci.yml/badge.svg)](https://github.com/liu-x-x-lab/fulcrum/actions/workflows/ci.yml)
[![Contributors](https://img.shields.io/github/contributors/liu-x-x-lab/fulcrum)](https://github.com/liu-x-x-lab/fulcrum/graphs/contributors)

`Fulcrum` 是本项目的名称。部分历史部署标识（Docker 容器名、数据库名、数据卷名）沿用 `multi-bot-saas` / `multi_bot_saas`，不影响使用。产品面向企业提供全渠道智能机器人运营、租户隔离、团队协作、套餐计费和通用机器人额度；渠道执行通过抽象接口接入，不把商业模型绑定到具体 Provider。

## 功能特性

- 公开站点：Fulcrum 营销首页、套餐页、企业自助注册和 7 天试用
- 商业闭环：人工付款订单、私有付款凭证、平台审核和订阅自动履约
- 通用额度：按 `bot_limit` 管理机器人数量，以 `adapter_type` 和 `bot_account_id` 标识外部账号
- 租户管理：企业主手机号登录、员工独立登录、角色权限、会话和 CSRF 防护
- 平台管理：套餐、租户、订阅、订单审核、受控解绑和平台审计
- 渠道接入：统一管理企业微信、微信 iPad 协议、Windows 微信和 Telegram
- 插件生态：34 个 A-BOT 业务插件完成 SaaS 原生迁移，支持租户自备第三方服务地址和密钥
- 远端执行：企业微信执行端支持一次性激活码、设备凭证、心跳、长轮询、任务租约和多账号绑定
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
fulcrum/
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
| `/` | Fulcrum 营销首页 |
| `/pricing` | 公开套餐价格 |
| `/register` | 企业自助注册与 7 天试用 |
| `/login` | 企业所有者与员工统一使用手机号登录 |
| `/channels` | 企业账号清单与额度状态 |
| `/channels/connect` | 企业微信、个人微信和 Telegram 统一接入向导 |
| `/platform/login` | 平台管理员登录 |

企业账号接入使用单一主流程：企业管理员选择平台并填写最少连接信息，具体连接器从 Telegram、微信客户端或企业微信客户端取得可信账号身份；确认后系统在同一事务内启用机器人和渠道并写入额度绑定。验证前的草稿不占用额度，也不能通过独立开关进入调度。

旧的手工渠道创建和租户自助解绑入口已经移除。账号对应的机器人、可信账号 ID 和执行客户端不能在高级设置中更换；需要释放付费绑定时，由平台管理员在绑定详情中填写原因后受控处理。

### 一键更新并重启

Linux / macOS 可使用：

```bash
chmod +x scripts/deploy_restart.sh
./scripts/deploy_restart.sh
```

脚本会自动执行：

- 拉取 `MBS_GIT_BRANCH` 指定分支，默认使用 `origin/main`
- 安装或更新依赖
- 校验 36 个迁移插件、2 个 SaaS 自有插件和目录安全边界
- 校验独立插件主密钥，不生成或使用弱默认值
- 在旧进程仍运行时执行 `alembic upgrade head`
- 清除历史普通配置中的插件敏感明文字段
- 迁移成功后关闭旧的 `uvicorn` 进程
- 重新启动服务
- 检查 `/healthz` 和 `/readyz`

可选环境变量：

```bash
MBS_GIT_BRANCH=main \
MBS_PORT=8280 \
./scripts/deploy_restart.sh
```

脚本只做 Git 快进、数据库差异迁移和应用进程替换，不执行数据库备份或要求备份门禁。数据库迁移和敏感字段清理成功前不会停止旧进程。

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
ABOT_PLUGIN_SECRET_KEY=
AUTH_SEED_DEMO_USERS=false
AUTH_BOOTSTRAP_ADMIN_USERNAME=admin
AUTH_BOOTSTRAP_ADMIN_PASSWORD=change-this-password
BILLING_PAYMENT_TITLE=Fulcrum 人工付款
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
| `ABOT_PLUGIN_SECRET_KEY` | 插件敏感配置 AES-GCM 主密钥；必须使用 32 字节 URL-safe Base64，部署后保持稳定 |
| `DATABASE_URL` | 数据库连接串 |
| `LOG_LEVEL` | 日志级别 |
| `AUTH_SEED_DEMO_USERS` | 是否自动创建演示账号 |
| `AUTH_COOKIE_SECURE` | 会话 Cookie 是否仅允许 HTTPS；使用 `http://IP:8280` 时必须显式设为 `false` |
| `AUTH_BOOTSTRAP_ADMIN_USERNAME` | 初始化管理员用户名 |
| `AUTH_BOOTSTRAP_ADMIN_PASSWORD` | 初始化管理员密码 |
| `BILLING_PAYMENT_TITLE` | 租户订单页展示的人工付款标题 |
| `BILLING_PAYMENT_INSTRUCTIONS` | 人工付款说明，不应包含一次性敏感凭据 |
| `BILLING_PAYMENT_QR_IMAGE` | 可选收款二维码静态地址 |
| `BILLING_PAYMENT_BANK_DETAILS` | 可选银行转账说明 |
| `BILLING_PAYMENT_PROOF_MAX_BYTES` | 单个付款凭证最大字节数，默认 5 MiB |

示例见 [`.env.example`](.env.example)。

生成独立插件主密钥：

```bash
python3 -c "import base64,secrets; print(base64.urlsafe_b64encode(secrets.token_bytes(32)).decode())"
```

## 插件迁移交付

当前目录包含 34 个从 A-BOT 业务迁移的插件，以及 `customer_operations`、`supplier_order_flow` 两个 SaaS 自有插件。插件统一通过可信租户运行上下文、标准消息回复、通用 HTTP、状态存储、限流和任务调度能力执行，不导入 A-BOT 的数据库、Redis、Provider 或单机管理运行时。

租户承担的 LLM、Dify、解析服务、NewAPI、MaiBot 等第三方成本，均由租户在插件绑定中维护地址和凭证。敏感字段使用 `ABOT_PLUGIN_SECRET_KEY` 加密后按租户和绑定隔离，目录配置不提供共享密钥或环境回退。

交付前可执行静态完整性检查：

```bash
.venv/bin/python scripts/verify_plugin_migration.py
```

只在内网使用、不随本仓库发布的插件，把目录名写进环境变量 `FULCRUM_PRIVATE_PLUGINS`（逗号分隔），校验脚本会忽略它们。

详细范围、迁移边界和定向验证记录见 [插件迁移台账](docs/plugin-migration-ledger.md)。

想自己开发插件，请阅读 [插件开发规范](docs/plugin-development.md)：清单 `config.toml`、配置字段声明（自动生成配置页）、入口函数、安全规则和测试方法。

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

- [Dockerfile](Dockerfile)
- [docker-compose.yml](docker-compose.yml)
- [`.env.docker.example`](.env.docker.example)

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

- `ai_auto_response`
- `customer_operations`
- `daily_news`
- `dify`
- `douyin_parser`
- `douyu`
- `duanju_search`
- `game_task`
- `global_news`
- `group_auto_invite`
- `group_virtual`
- `guess_song`
- `jd_sign_token`
- `lucky_pot`
- `message_push_task`
- `message_sign`
- `message_summary`
- `music`
- `point_trade`
- `supplier_order_flow`
- `weather`

插件由后台启动时自动扫描并同步到数据库。

A-BOT 最新插件的审核、迁移、排除和平台原生替代范围记录在
[`docs/plugin-migration-ledger.md`](docs/plugin-migration-ledger.md)。迁移只复用业务规则，
不加载 A-BOT 的插件接口、数据库、全局 Redis 或微信客户端。

## 渠道与适配边界

套餐、订单、试用和机器人额度只依赖通用 `adapter_type`、`bot_account_id` 与执行能力接口，不依赖 Provider 数据模型。新增渠道时应实现适配接口并复用通用额度校验，不能把具体 Provider 客户端导入商业服务。

正式渠道由 `app/services/channel_catalog.py` 统一声明。页面、启用校验、适配器解析和后台调度都读取同一份目录；未知渠道类型直接拒绝，不降级到通用 HTTP 适配器。

| 渠道类型 | 展示名称 | 运行方式 | 启用前必要配置 |
| --- | --- | --- | --- |
| `workwechat` | 企业微信 | 客户端远端执行并主动领取任务 | 已连接企业微信客户端及在线账号 |
| `wechat_ipad` | 微信 iPad 协议 | 平台按租户执行消息同步、心跳和登录恢复 | API Base |
| `wechat_winauto` | 微信 Windows | HTTP 执行端与 Webhook | API Base |
| `wechat_gugatool` | 咕嘎助手 | 手机端主动连接平台，HTTP 长轮询或 WebSocket | 平台生成的 Agent 地址 |
| `telegram` | Telegram | 平台按租户调用 Bot API 拉取更新 | Bot Token |

接入向导可以保留尚未验证的停用草稿，但只有可信身份验证和额度绑定都完成后才能启用。渠道类型、关联机器人和可信账号创建后不可由租户修改；企业微信客户端关联也只在首次接入时确定。只有声明需要远端执行器的渠道会保存客户端和账号主键，其他渠道即使夹带这些字段也会被服务端清空。

后台协调器使用平台会话枚举有效租户主键，再为每个租户建立独立 `tenant_scope` 和租户数据库会话。Telegram 拉取、微信 iPad 消息同步、心跳和自动恢复均在对应租户上下文中执行；单个渠道失败不会中断其他渠道或租户。企业微信远端执行和 Windows 微信 Webhook 不进入平台主动轮询。

### 跨机器发送图片和文件

如果 `Fulcrum` 和 Windows provider 不在同一台机器，推荐使用“主服务中转文件”的方式：

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
  "download_url": "https://fulcrum.example.com/outbound-files/3/abc123/report.pdf",
  "payload": {
    "message_type": "file",
    "file_name": "report.pdf",
    "file_url": "https://fulcrum.example.com/outbound-files/3/abc123/report.pdf"
  }
}
```

随后可直接出站：

```json
{
  "external_id": "group:测试群",
  "message_type": "file",
  "file_name": "report.pdf",
  "file_url": "https://fulcrum.example.com/outbound-files/3/abc123/report.pdf"
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
curl -X POST "https://fulcrum.example.com/channels/3/outbound/upload-and-send" `
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

- [Tool API For OSM](docs/tool_api_for_osm.md)
- 登录企业工作台后，也可以从“开发文档”进入桥接接口中心，查看推荐的按开放应用签名的 `/api/open/*` 协议：
  [桥接接口开发文档](docs/bridge-api.md)

这组 API 使用 `X-API-Key` 鉴权，典型用途：

- 上传 Excel 并发送到指定群
- 查询发送任务状态
- 查询指定群的新消息

新业务系统优先使用 `/api/open/*`。它按开放应用隔离密钥和授权渠道，支持会话/群查询、文本和文件发送、任务查询以及 Webhook 事件；业务系统不需要实现微信扫码登录，只使用平台已经配置好的渠道。

### workwechat Provider

企业微信 Hook provider 是运行在企业本地 Windows 机器上的独立客户端，**不随本仓库发布**
（它依赖第三方 Hook 组件，需要使用者自行获取并遵守其许可）。本仓库只包含服务端对它的支持，
通信契约见 [WorkWechat Provider 接入说明](docs/workwechat_provider.md)。

推荐使用方式：

- 启动 `workwechat_provider`，它会自动拉起内置 Hook runtime
- 在企业后台连接企业微信客户端并生成一次性激活码
- 客户端激活并上报在线账号后，从 `/channels/connect` 选择企业微信完成身份验证和额度绑定
- 客户端通过长轮询领取绑定渠道的任务，不需要平台主动连接客户电脑

## 测试

部分测试会修改全局配置，放在同一个进程里会互相污染，请逐文件隔离运行（每个文件独立进程和独立测试数据库）：

```bash
scripts/run_tests_isolated.sh                              # 全部 tests/test_*.py
scripts/run_tests_isolated.sh .github/ci-test-files.txt    # CI 使用的稳定清单
```

单个文件必须指定独立的测试数据库，避免误连开发库：

```bash
APP_ENV=test DATA_DIR="$PWD/.run/t" DATABASE_URL="sqlite:///$PWD/.run/t/test.db" LOG_DIR="$PWD/.run/t/logs" \
.venv/bin/python -m pytest tests/test_xxx.py -q
```

## 开发说明

- 启动时会自动初始化数据库并启动后台 worker
- 后台 worker 会处理：
  - 插件派发
  - 出站消息发送
  - 租户隔离的 Telegram 拉取
  - 租户隔离的微信 iPad 消息同步、心跳和登录恢复
  - 企业微信远端执行端任务队列
- 插件配置与运行态信息会存入数据库
- 插件敏感配置使用独立 `ABOT_PLUGIN_SECRET_KEY` 和 AES-GCM 加密，并绑定租户与插件绑定主键

## 常见文件

- [main.py](app/main.py): FastAPI 入口
- [db.py](app/core/db.py): 数据库引擎与会话
- [settings.py](app/core/settings.py): 配置入口
- [entities.py](app/models/entities.py): 数据模型
- [plugin_directory.py](app/services/plugin_directory.py): 目录插件扫描与调度
- [async_workers.py](app/services/async_workers.py): 后台任务与轮询
- [channel_catalog.py](app/services/channel_catalog.py): 正式渠道、能力和表单字段目录
- [channel_runtime.py](app/services/channel_runtime.py): 租户隔离渠道任务协调器

## 生产建议

- 生产环境关闭 `AUTH_SEED_DEMO_USERS`
- HTTPS 部署保持 `AUTH_COOKIE_SECURE=true`；仅限受控内网 HTTP 验收时显式设为 `false`
- 使用高强度且长期稳定的 `SECRET_KEY`（至少 32 位随机字符串；非开发环境使用示例值会拒绝启动），轮换前必须设计设备摘要迁移方案
- 数据量大时切换到 MariaDB
- 为 `logs/`、数据库和 `data/tenant_billing/` 做好持久化与一致性备份
- 反向代理后再对外暴露服务
- 对 Telegram 等外部渠道单独准备稳定网络出口或代理

## 社区与交流

扫码添加企业微信，加入交流群（使用问题、功能建议、合作交流都欢迎）：

<img src="docs/images/community-qr.png" alt="交流群二维码" width="220" />

也可以直接在 [Issues](https://github.com/liu-x-x-lab/fulcrum/issues) 里提问或反馈。

## Star 趋势

[![Star History Chart](https://api.star-history.com/svg?repos=liu-x-x-lab/fulcrum&type=Date)](https://star-history.com/#liu-x-x-lab/fulcrum&Date)

## 贡献者

感谢所有为 Fulcrum 做出贡献的人！参与方式见 [CONTRIBUTING.md](CONTRIBUTING.md)。

<a href="https://github.com/liu-x-x-lab/fulcrum/graphs/contributors">
  <img src="https://contrib.rocks/image?repo=liu-x-x-lab/fulcrum" alt="Contributors" />
</a>

## License

本项目以 [Apache License 2.0](LICENSE) 发布。第三方依赖遵循各自的许可证；
可选依赖见 [requirements-optional.txt](requirements-optional.txt)（例如 `pysilk` 未声明许可证，使用前请自行确认）。

微信/企业微信 Hook、iPad 协议等 provider 客户端依赖第三方组件，**不随本仓库发布**；使用这类非官方接口可能违反
相关平台的服务条款，请自行评估风险并遵守适用的法律法规。

安全问题请参阅 [SECURITY.md](SECURITY.md)，参与贡献请参阅 [CONTRIBUTING.md](CONTRIBUTING.md)。
