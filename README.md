# Multi Bot Admin

`Multi Bot Admin` 是一个多渠道机器人后台，提供机器人、渠道、会话、插件、任务队列和运行状态管理能力。项目当前重点面向微信 iPad、Telegram 等渠道，并支持目录驱动插件体系，方便持续迁移和维护现有插件。

## 功能特性

- 机器人管理：创建、启停、分组、环境标记、命令前缀配置
- 渠道管理：支持 `wechat_ipad`、`wechat_winauto`、`workwechat`、`telegram`、`openclaw`、`simulator`
- 会话管理：查看消息、手动发送、出入站状态追踪
- 插件管理：目录扫描、运行态装载、配置编辑、启停与重载
- 异步任务：插件派发、消息出站、定时推送、后台轮询
- 微信群能力：群资料、成员同步、退群/回群状态、最后活跃时间
- `wechat_ipad` Provider：项目附带 `abot` 的 provider 目录，可单独部署后接入
- AI 插件能力：Dify 对话、图片引用、图片/视频结果回发
- 运行观测：健康检查、错误日志、任务状态、运行时详情

## 技术栈

- Python 3.11
- FastAPI
- SQLAlchemy 2.x
- Jinja2
- Uvicorn
- SQLite / MariaDB

## 目录结构

```text
multi-bot-admin/
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
pip install -r requirements.txt
```

Windows PowerShell:

```powershell
python -m venv .venv
.\.venv\Scripts\Activate.ps1
pip install -r requirements.txt
```

### 2. 启动服务

Linux / macOS:

```bash
uvicorn app.main:app --host 0.0.0.0 --port 8180
```

Windows:

```powershell
python -m uvicorn app.main:app --host 0.0.0.0 --port 8180
```

开发模式可加 `--reload`。

访问：

```text
http://127.0.0.1:8180
```

### 一键更新并重启

Linux / macOS 可使用：

```bash
chmod +x scripts/deploy_restart_mba.sh
./scripts/deploy_restart_mba.sh
```

脚本会自动执行：

- 拉取 `origin/main` 最新代码
- 安装或更新依赖
- 关闭旧的 `uvicorn` 进程
- 重新启动服务
- 检查 `/healthz`

可选环境变量：

```bash
MBA_GIT_BRANCH=main MBA_PORT=8180 ./scripts/deploy_restart_mba.sh
```

### 3. 首次安装引导

首次启动且系统中还没有管理员账号时，会自动跳转到 `/setup`：

- 选择数据库类型：默认使用内置 SQLite，也支持切换到 MariaDB
- 设置管理员用户名、显示名和密码
- 自动生成或手动填写 `SECRET_KEY`
- 提交后自动建表、初始化管理员，并写入项目根目录 `.env`

推荐规则：

- 首次体验、本地调试：直接选 SQLite
- 长期部署、大量消息入库：使用 MariaDB

说明：

- 使用 SQLite 时，初始化完成后可直接登录
- 使用 MariaDB 时，向导会先验证连接并完成初始化，然后提示你重启服务切换到新数据库

### 4. 手动配置环境变量（可选）

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
| `SECRET_KEY` | 用于敏感信息加解密与认证 |
| `DATABASE_URL` | 数据库连接串 |
| `LOG_LEVEL` | 日志级别 |
| `AUTH_SEED_DEMO_USERS` | 是否自动创建演示账号 |
| `AUTH_BOOTSTRAP_ADMIN_USERNAME` | 初始化管理员用户名 |
| `AUTH_BOOTSTRAP_ADMIN_PASSWORD` | 初始化管理员密码 |

示例见 [`.env.example`](/d:/learn/multi-bot-admin/.env.example)。

## 数据库

### SQLite

开发环境推荐继续使用 SQLite，配置简单、启动快：

```env
DATABASE_URL=sqlite:///./data/app.db
```

### MariaDB

如果消息规模会到几十万甚至更多，生产环境更建议使用 MariaDB：

```env
DATABASE_URL=mysql+pymysql://multi_bot_admin:multi_bot_admin_pass@127.0.0.1:3306/multi_bot_admin?charset=utf8mb4
DATABASE_POOL_SIZE=10
DATABASE_MAX_OVERFLOW=20
DATABASE_POOL_RECYCLE_SECONDS=1800
DATABASE_POOL_TIMEOUT_SECONDS=30
```

### 当前数据库初始化方式

当前项目还未引入正式 migration 工具，启动时采用：

- `Base.metadata.create_all()`
- 运行时 schema 补列

这套方式适合当前阶段快速迭代；如果后续进入长期生产维护，建议继续补 `Alembic`。

### 首次安装默认行为

如果数据库中还没有启用中的管理员账号，系统会强制进入 `/setup` 安装向导。

- 选择 SQLite 时，会直接初始化 `data/app.db`
- 选择 MariaDB 时，会先验证连接并完成建表
- 配置成功后会自动写入 `.env`
- MariaDB 模式需要重启进程后，运行中的应用才会切换到新连接

## Docker 部署

项目已提供：

- [Dockerfile](/d:/learn/multi-bot-admin/Dockerfile)
- [docker-compose.yml](/d:/learn/multi-bot-admin/docker-compose.yml)
- [`.env.docker.example`](/d:/learn/multi-bot-admin/.env.docker.example)

### 使用 Docker Compose

```bash
cp .env.docker.example .env.docker
docker compose up -d --build
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

## 渠道说明

当前内置渠道类型：

- `wechat_ipad`
- `wechat_winauto`
- `workwechat`
- `telegram`
- `openclaw`
- `simulator`

说明：

- `wechat_ipad` 当前能力最完整，支持图片、语音、视频、群资料和成员同步等
- `wechat_ipad` 建议先单独部署 provider，再把 `API Base` 配到渠道里
- `wechat_winauto` 适合 Windows 桌面微信场景，推荐单独部署 Windows provider，通过 HTTP 和主服务解耦
- `workwechat` 适合企业微信 Hook 协议，推荐单独启动 [WorkWechat Provider](/d:/learn/multi-bot-admin/docs/workwechat_provider.md)
- `telegram` 目前支持文本、图片、轮询拉取；视频发送仍建议按渠道能力做降级
- `openclaw` 依赖桥接端实现
- `simulator` 适合本地调试

### 跨机器发送图片和文件

如果 `Multi Bot Admin` 和 Windows provider 不在同一台机器，推荐使用“主服务中转文件”的方式：

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
  "download_url": "http://192.168.2.142:8180/outbound-files/3/abc123/report.pdf",
  "payload": {
    "message_type": "file",
    "file_name": "report.pdf",
    "file_url": "http://192.168.2.142:8180/outbound-files/3/abc123/report.pdf"
  }
}
```

随后可直接出站：

```json
{
  "external_id": "group:测试群",
  "message_type": "file",
  "file_name": "report.pdf",
  "file_url": "http://192.168.2.142:8180/outbound-files/3/abc123/report.pdf"
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
curl -X POST "http://192.168.2.142:8180/channels/3/outbound/upload-and-send" `
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

- [Tool API For OSM](/d:/learn/multi-bot-admin/docs/tool_api_for_osm.md)

这组 API 使用 `X-API-Key` 鉴权，典型用途：

- 上传 Excel 并发送到指定群
- 查询发送任务状态
- 查询指定群的新消息

### wechat_ipad Provider

项目里附带了一份来自 `abot` 的 provider 目录：

- [vendors/wechat_ipad_provider](/d:/learn/multi-bot-admin/vendors/wechat_ipad_provider)
- [Wechat iPad Provider 部署说明](/d:/learn/multi-bot-admin/docs/wechat_ipad_provider.md)

推荐使用方式：

- 由用户自己单独启动 provider
- 根据实际部署地址，在渠道管理里填写 `API Base`
- 主应用不默认托管这个 Go 服务，避免部署和运维强耦合

### wechat_winauto Provider

仓库里也附带了 Windows `wxauto` provider 骨架：

- [vendors/wechat_winauto_provider](/d:/learn/multi-bot-admin/vendors/wechat_winauto_provider)
- [WinAuto Provider 接入方案](/d:/learn/multi-bot-admin/docs/wechat_winauto_provider.md)

推荐使用方式：

- 在 Windows 机器单独启动 provider
- 保持桌面微信在线
- 在渠道里填写 `API Base`

### workwechat Provider

仓库里附带了企业微信 Hook 高层 provider：

- [vendors/workwechat_provider](/d:/learn/multi-bot-admin/vendors/workwechat_provider)
- [WorkWechat Provider 接入说明](/d:/learn/multi-bot-admin/docs/workwechat_provider.md)

推荐使用方式：

- 启动 `workwechat_provider`，它会自动拉起内置 Hook runtime
- 在后台新增 `channel_type=workwechat` 的渠道，并把 `API Base` 指向 `workwechat_provider`

## 测试

项目当前以单文件集成测试为主：

```bash
python -m unittest tests.test_multi_bot_admin
```

如果你在虚拟环境中运行：

```bash
.venv/bin/python -m unittest tests.test_multi_bot_admin
```

Windows:

```powershell
.\.venv\Scripts\python -m unittest tests.test_multi_bot_admin
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

- [main.py](/d:/learn/multi-bot-admin/app/main.py): FastAPI 入口
- [db.py](/d:/learn/multi-bot-admin/app/core/db.py): 数据库引擎与会话
- [settings.py](/d:/learn/multi-bot-admin/app/core/settings.py): 配置入口
- [entities.py](/d:/learn/multi-bot-admin/app/models/entities.py): 数据模型
- [plugin_directory.py](/d:/learn/multi-bot-admin/app/services/plugin_directory.py): 目录插件扫描与调度
- [async_workers.py](/d:/learn/multi-bot-admin/app/services/async_workers.py): 后台任务与轮询

## 生产建议

- 生产环境关闭 `AUTH_SEED_DEMO_USERS`
- 使用高强度 `SECRET_KEY`
- 数据量大时切换到 MariaDB
- 为 `logs/` 和数据库文件做好持久化
- 反向代理后再对外暴露服务
- 对 Telegram 等外部渠道单独准备稳定网络出口或代理

## License

当前仓库未单独声明开源许可证。如需对外发布，请补充正式 License 文件。
