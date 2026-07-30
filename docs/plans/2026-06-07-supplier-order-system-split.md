# Supplier Order System Split Implementation Plan

> **For Claude:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task.

> 当前状态：2026-06-10 已确认收发单系统完整运行在独立 `supplier-order-admin`，MBA 内旧 `supplier_order_flow` 业务实现已经退出。后续不要继续修改 `app/services/supplier_order_flow`、`app/api/supplier_order_flow.py`、`app/templates/supplier_order_flow` 或 `tests/test_supplier_order_flow.py`；`plugins/supplier_order_flow` 只保留轻量桥接和单群停用入口。

**Goal:** 拆出独立 `supplier-order-admin` 收发单系统，并让它通过 MBA 消息网关完成企业微信发单和回传接收。

**Architecture:** MBA 保留机器人、渠道、消息、文件、任务和 Provider 能力；新系统承载供应商订单业务。两边通过 MBA Open API 和 Webhook 事件通信，收发单系统不直接依赖企业微信 Provider、授权码、WS 或 Hook。

**Tech Stack:** FastAPI, SQLAlchemy, Jinja2, SQLite/MariaDB, existing MBA message/task/file APIs, unittest.

---

## Phase 0: 冻结边界

### Task 0.1: 确认拆分清单

**Files:**
- Read: `app/models/entities.py`
- Read: `app/services/supplier_order_flow/`
- Read: `app/api/supplier_order_flow.py`
- Read: `app/templates/supplier_order_flow/`
- Read: `plugins/supplier_order_flow/plugin.py`
- Read: `tests/test_supplier_order_flow.py`

**Step 1: 列出迁移对象**

确认以下对象需要进入新系统：

```text
SupplierOrderSetting
SupplierOrderSupplier
SupplierOrderGroupBinding
SupplierOrderBatch
SupplierOrderItem
SupplierOrderReturn
SupplierOrderReturnItem
app/services/supplier_order_flow/*
app/templates/supplier_order_flow/*
app/api/supplier_order_flow.py 中的业务页面和业务动作
```

**Step 2: 列出 MBA 保留对象**

确认以下对象留在 MBA：

```text
Bot / Channel / Conversation / Message / MessageAttachment / TaskJob
channel_outbound / channel_inbound / tool_api
workwechat_provider_cloud
企业微信 Provider 和云端任务队列
```

**Step 3: Commit**

本任务只确认边界，不提交代码。

---

## Phase 1: 新建收发单系统骨架

### Task 1.1: 创建新项目目录

**Files:**
- Create: sibling repo/directory `../supplier-order-admin`
- Create: `../supplier-order-admin/app/main.py`
- Create: `../supplier-order-admin/app/core/settings.py`
- Create: `../supplier-order-admin/app/core/db.py`
- Create: `../supplier-order-admin/app/models/entities.py`
- Create: `../supplier-order-admin/requirements.txt`
- Create: `../supplier-order-admin/README.md`

**Step 1: 搭建最小 FastAPI 应用**

包含：

```text
GET /healthz
GET /
```

**Step 2: 搭建数据库初始化**

复用 SQLAlchemy `Base.metadata.create_all`，首版不引入 Alembic。

**Step 3: 运行验证**

Run:

```bash
python -m py_compile app/main.py app/core/settings.py app/core/db.py app/models/entities.py
```

**Step 4: Commit**

```bash
git add .
git commit -m "feat: 初始化收发单独立系统骨架"
```

---

## Phase 2: 迁移收发单数据模型和服务

### Task 2.1: 迁移数据表

**Files:**
- Modify: `../supplier-order-admin/app/models/entities.py`
- Source: `app/models/entities.py`

**Step 1: 复制业务表**

迁移：

```text
SupplierOrderSetting
SupplierOrderSupplier
SupplierOrderGroupBinding
SupplierOrderBatch
SupplierOrderItem
SupplierOrderReturn
SupplierOrderReturnItem
```

**Step 2: 去掉 MBA 外键依赖**

`channel_id`、`chatroom_id` 先保留为普通整数和字符串字段，不再外键到 MBA 表。

建议新增：

```text
mba_bot_id
mba_channel_id
conversation_external_id
conversation_title_snapshot
```

**Step 3: 写模型创建测试**

Create: `../supplier-order-admin/tests/test_models.py`

验证 `create_all` 和基础插入。

**Step 4: Commit**

```bash
git add app/models/entities.py tests/test_models.py
git commit -m "feat: 迁移收发单业务数据模型"
```

### Task 2.2: 迁移业务服务

**Files:**
- Create: `../supplier-order-admin/app/services/supplier_order_flow/`
- Source: `app/services/supplier_order_flow/`

**Step 1: 复制服务模块**

迁移：

```text
batch_service.py
excel_service.py
express.py
order_api.py
return_service.py
supplier_sync.py
```

**Step 2: 替换 MBA 发送依赖**

把 `enqueue_job`、`store_outbound_upload`、`Channel`、`Conversation` 依赖替换为 `MbaClient`。

先创建接口占位：

```python
class MbaClient:
    def send_file(self, *, request_id: str, conversation_external_id: str, file_path: str, file_name: str, content: str) -> dict:
        ...
```

**Step 3: 写服务测试**

迁移 `tests/test_supplier_order_flow.py` 中不依赖 MBA TestClient 的服务测试。

**Step 4: Commit**

```bash
git add app/services tests
git commit -m "feat: 迁移收发单业务服务"
```

---

## Phase 3: MBA 增加通用消息 Open API

### Task 3.1: 新增 Open API 鉴权

**Files:**
- Modify: `/data/multi-bot-admin/app/models/entities.py`
- Create: `/data/multi-bot-admin/app/services/open_api_auth.py`
- Create: `/data/multi-bot-admin/tests/test_open_api.py`

**Step 1: 新增接入应用模型**

新增：

```text
OpenApiApp
- id
- name
- app_key
- app_secret_hash
- bot_id
- default_channel_id
- status
- allowed_plugin_codes_json
- webhook_url
- webhook_secret
```

**Step 2: 新增鉴权服务**

支持：

```text
X-App-Key
X-Timestamp
X-Signature
```

签名内容：

```text
timestamp + "\n" + raw_body
```

**Step 3: 测试鉴权**

覆盖签名正确、错误、禁用应用。

**Step 4: Commit**

```bash
git add app/models/entities.py app/services/open_api_auth.py tests/test_open_api.py
git commit -m "feat: 增加开放接口应用鉴权"
```

### Task 3.2: 新增统一发送文件 API

**Files:**
- Create: `/data/multi-bot-admin/app/api/open_api.py`
- Modify: `/data/multi-bot-admin/app/api/__init__.py`
- Modify: `/data/multi-bot-admin/app/main.py`
- Test: `/data/multi-bot-admin/tests/test_open_api.py`

**Step 1: 实现 API**

```text
POST /api/open/files
```

Body:

```json
{
  "request_id": "supplier-order-batch-001",
  "conversation_external_id": "R:room-001",
  "conversation_title": "供应商群",
  "chat_type": "group",
  "file_name": "100单-供应商-06071030.xls",
  "file_url": "https://supplier.example.com/files/xxx",
  "content": "供应商发单批次..."
}
```

MBA 内部：

```text
app_key -> OpenApiApp -> default_channel_id
ensure Conversation
enqueue outbound job
notify cloud WS if workwechat cloud mode
return job_id
```

**Step 2: 支持幂等**

用 `request_id` 防重复提交。可以先复用 `TaskJob.related_type=open_api_request`，后续再加独立请求表。

**Step 3: 测试**

覆盖云端企微渠道会创建 `workwechat_cloud_outbound` 任务。

**Step 4: Commit**

```bash
git add app/api/open_api.py app/api/__init__.py app/main.py tests/test_open_api.py
git commit -m "feat: 增加开放接口文件发送能力"
```

### Task 3.3: 新增统一消息 API 和任务查询

**Files:**
- Modify: `/data/multi-bot-admin/app/api/open_api.py`
- Test: `/data/multi-bot-admin/tests/test_open_api.py`

**Step 1: 实现 API**

```text
POST /api/open/messages
GET  /api/open/jobs/{request_id}
```

**Step 2: 支持 message 类型**

首版支持：

```text
text
image
file
link
mini_program
```

**Step 3: Commit**

```bash
git add app/api/open_api.py tests/test_open_api.py
git commit -m "feat: 增加开放接口消息发送和任务查询"
```

---

## Phase 4: MBA 增加事件转发到收发单系统

### Task 4.1: 新增 Webhook 投递服务

**Files:**
- Create: `/data/multi-bot-admin/app/services/open_webhooks.py`
- Test: `/data/multi-bot-admin/tests/test_open_webhooks.py`

**Step 1: 构建事件 envelope**

事件类型：

```text
message.received
file.received
message.sent
message.failed
```

**Step 2: 签名**

Header:

```text
X-MBA-Event-ID
X-MBA-Timestamp
X-MBA-Signature
```

**Step 3: Commit**

```bash
git add app/services/open_webhooks.py tests/test_open_webhooks.py
git commit -m "feat: 增加开放事件回调服务"
```

### Task 4.2: 入站消息触发 Webhook

**Files:**
- Modify: `/data/multi-bot-admin/app/services/channel_events.py`
- Modify: `/data/multi-bot-admin/app/services/inbound_media.py`
- Test: `/data/multi-bot-admin/tests/test_open_webhooks.py`

**Step 1: 消息落库后触发**

对绑定了 webhook 的 OpenApiApp 推送事件。

**Step 2: 文件下载完成后触发**

入站附件有可下载地址后推送 `file.received`。

**Step 3: Commit**

```bash
git add app/services/channel_events.py app/services/inbound_media.py tests/test_open_webhooks.py
git commit -m "feat: 入站消息和文件触发开放回调"
```

---

## Phase 5: 新系统接入 MBA

### Task 5.1: 新系统实现 MbaClient

**Files:**
- Create: `../supplier-order-admin/app/services/mba_client.py`
- Modify: `../supplier-order-admin/app/core/settings.py`
- Test: `../supplier-order-admin/tests/test_mba_client.py`

**Step 1: 配置项**

```env
MBA_BASE_URL=
MBA_APP_KEY=
MBA_APP_SECRET=
MBA_DEFAULT_CONVERSATION_CHAT_TYPE=group
```

**Step 2: 实现签名请求**

调用：

```text
POST /api/open/files
POST /api/open/messages
GET /api/open/jobs/{request_id}
```

**Step 3: Commit**

```bash
git add app/services/mba_client.py app/core/settings.py tests/test_mba_client.py
git commit -m "feat: 接入MBA开放消息接口"
```

### Task 5.2: 发单流程改用 MbaClient

**Files:**
- Modify: `../supplier-order-admin/app/services/supplier_order_flow/batch_service.py`
- Test: `../supplier-order-admin/tests/test_supplier_order_flow.py`

**Step 1: 替换发送逻辑**

原 MBA 内部队列发送改为：

```text
MbaClient.send_file(...)
```

**Step 2: 保存 MBA 返回任务**

批次记录保存：

```text
mba_request_id
mba_job_id
send_status
```

**Step 3: Commit**

```bash
git add app/services/supplier_order_flow/batch_service.py tests/test_supplier_order_flow.py
git commit -m "feat: 发单流程改用MBA发送文件"
```

### Task 5.3: 新系统接收 MBA Webhook

**Files:**
- Create: `../supplier-order-admin/app/api/mba_webhooks.py`
- Modify: `../supplier-order-admin/app/main.py`
- Test: `../supplier-order-admin/tests/test_mba_webhooks.py`

**Step 1: 实现入口**

```text
POST /api/mba/webhooks
```

**Step 2: 处理事件**

```text
file.received -> 下载附件 -> process_return_file
message.received -> 文本回传 -> process_return_text
message.sent/message.failed -> 更新批次发送状态
```

**Step 3: Commit**

```bash
git add app/api/mba_webhooks.py app/main.py tests/test_mba_webhooks.py
git commit -m "feat: 接收MBA事件回调处理回传"
```

---

## Phase 6: 新系统后台页面迁移

### Task 6.1: 迁移模板和路由

**Files:**
- Create: `../supplier-order-admin/app/api/admin.py`
- Create: `../supplier-order-admin/app/templates/supplier_order_flow/*`
- Source: `/data/multi-bot-admin/app/templates/supplier_order_flow/*`

**Step 1: 迁移页面**

迁移：

```text
dashboard
suppliers
settings
batches
returns
exceptions
```

**Step 2: 调整导航**

移除 MBA 插件后台壳依赖，改成新系统自己的后台布局。

**Step 3: Commit**

```bash
git add app/api/admin.py app/templates
git commit -m "feat: 迁移收发单后台页面"
```

---

## Phase 7: 瘦身 MBA

### Task 7.1: 禁用旧收发单后台并保留轻量桥接

**Files:**
- Modify: `/data/multi-bot-admin/app/api/__init__.py`
- Modify: `/data/multi-bot-admin/app/main.py`
- Create/Modify: `/data/multi-bot-admin/plugins/supplier_order_flow/`

**Step 1: 移除旧后台路由注册**

不再 include `supplier_order_flow_router`。

**Step 2: 保留轻量桥接插件**

当前新系统已有完整收发单逻辑，MBA 旧重业务插件不再恢复。目录插件只做识别、帮助提示和单群停用入口，不调用 `app/services/supplier_order_flow`。

**Step 3: Commit**

```bash
git add -A app/api/__init__.py app/main.py plugins/supplier_order_flow
git commit -m "refactor: 将MBA收发单插件改为轻量桥接"
```

### Task 7.2: 删除 MBA 旧业务表和服务

**Files:**
- Modify: `/data/multi-bot-admin/app/models/entities.py`
- Delete: `/data/multi-bot-admin/app/services/supplier_order_flow/`
- Delete: `/data/multi-bot-admin/app/templates/supplier_order_flow/`
- Delete: `/data/multi-bot-admin/app/api/supplier_order_flow.py`
- Modify/Delete: `/data/multi-bot-admin/tests/test_supplier_order_flow.py`

**Step 1: 删除旧业务依赖**

确认新系统闭环跑通后再执行。

**Step 2: 测试 MBA**

Run:

```bash
.venv311/bin/python -m unittest tests.test_local_timezone tests.test_workwechat_cloud_poll tests.test_workwechat_provider tests.test_customer_operations -v
.venv311/bin/python -m compileall -q app plugins
```

**Step 3: Commit**

```bash
git add -A
git commit -m "refactor: 从MBA移除收发单业务子系统"
```

---

## Phase 8: 端到端验收

### Task 8.1: 发单闭环

**Scenario:**

```text
新系统创建批次
-> 新系统调用 MBA /api/open/files
-> MBA 生成任务
-> 企业微信 Provider 发送文件
-> Provider 回传 complete
-> MBA Webhook 通知新系统
-> 新系统批次状态变为 sent
```

### Task 8.2: 回传闭环

**Scenario:**

```text
供应商群上传 Excel
-> MBA 入站文件落库/下载
-> MBA Webhook file.received
-> 新系统下载文件
-> 新系统解析并导入业务系统
-> 新系统记录回传结果
```

### Task 8.3: 客户交付包

**Deliverables:**

```text
supplier-order-admin README
MBA API 接入说明
Webhook 签名说明
部署脚本
客户配置清单
```
