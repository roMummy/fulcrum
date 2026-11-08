# 开放 API 按 874 能力补全 Implementation Plan

> **For Claude:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task.

**Goal:** 在现有 `/api/open/*` 上补齐 874 非登录业务能力，让有效订阅租户可以自助管理开放应用、授权渠道、Webhook 和套餐额度，并通过稳定业务 API 开发自己的系统。

**Architecture:** 保留现有 HMAC 开放应用鉴权和渠道授权边界，在服务层增加统一能力矩阵、额度账本和异步任务封装。对外提供账号、联系人、群、消息、媒体、任务和 Webhook 资源，874 原始端点只存在于内部适配器；企业自助配置由租户路由完成，订阅和支付回调负责能力门禁。

**Tech Stack:** FastAPI、SQLAlchemy、Alembic、Jinja2、现有任务队列、现有微信 iPad 874 客户端、pytest。

---

### Task 1: 建立 874 能力矩阵和开放 API 契约测试

**Files:**
- Create: `app/services/open_api_capabilities.py`
- Create: `tests/test_open_api_capabilities.py`
- Modify: `app/services/wechat_ipad/contract.py`
- Modify: `docs/bridge-api.md`

**Step 1: Write the failing test**

增加测试，验证每个业务能力包含稳定编码、支持的渠道类型、读写方向和内部 874 方法映射；登录、代理、心跳、`setproxy` 必须不出现在开放能力集合中。

**Step 2: Run test to verify it fails**

Run: `pytest tests/test_open_api_capabilities.py -q`

Expected: FAIL，因为能力矩阵模块尚不存在。

**Step 3: Write minimal implementation**

新增只读能力矩阵，将 `Ipad874Endpoints` 的非登录方法映射为 `account.read`、`contact.read`、`group.members.read`、`message.*`、`media.*` 等业务能力。矩阵只描述能力，不执行请求；为未实现的外部接口保留明确的 `planned` 状态，不能伪装成可用。

**Step 4: Run test to verify it passes**

Run: `pytest tests/test_open_api_capabilities.py -q`

Expected: PASS。

**Step 5: Commit**

```bash
git add app/services/open_api_capabilities.py app/services/wechat_ipad/contract.py tests/test_open_api_capabilities.py docs/bridge-api.md
git commit -m "契约：建立 874 开放能力矩阵"
```

### Task 2: 增加开放应用自助管理和作用域模型

**Files:**
- Create: `app/web/open_api_apps.py`
- Create: `app/templates/open_api_apps.html`
- Create: `app/templates/open_api_app_detail.html`
- Create: `tests/test_open_api_app_pages.py`
- Modify: `app/services/open_api_apps.py`
- Modify: `app/web/tenant_navigation.py`
- Modify: `app/main.py`

**Step 1: Write the failing test**

覆盖企业主/企业管理员创建、查看、停用、轮换密钥、配置 Webhook、绑定授权渠道；普通 operator 和 readonly 必须被拒绝；没有有效订阅时创建操作返回订阅错误。

**Step 2: Run test to verify it fails**

Run: `pytest tests/test_open_api_app_pages.py -q`

Expected: FAIL，因为租户侧没有完整的开放应用管理入口和订阅门禁。

**Step 3: Write minimal implementation**

增加租户路由和模板，复用 `OpenApiApp`、`OpenApiAppChannel` 与 `open_api_apps.py` 的密钥处理；新密钥只在创建或轮换成功时展示一次，页面不得回显明文。应用数量、渠道数量和 Webhook 配置先调用统一额度服务检查。

**Step 4: Run test to verify it passes**

Run: `pytest tests/test_open_api_app_pages.py -q`

Expected: PASS。

**Step 5: Commit**

```bash
git add app/web/open_api_apps.py app/templates/open_api_apps.html app/templates/open_api_app_detail.html app/services/open_api_apps.py app/web/tenant_navigation.py app/main.py tests/test_open_api_app_pages.py
git commit -m "功能：增加租户自助开放应用管理"
```

### Task 3: 增加套餐额度字段和使用账本

**Files:**
- Create: `migrations/versions/20261108_00xx_open_api_usage_quotas.py`
- Create: `app/services/open_api_quotas.py`
- Create: `tests/test_open_api_quotas.py`
- Modify: `app/models/saas.py`
- Modify: `app/models/entities.py`
- Modify: `app/services/billing.py`

**Step 1: Write the failing test**

覆盖开放应用数、授权渠道数、月 API 调用数、月消息数和月媒体字节数的计算；相同 `request_id` 只能预扣一次；明确失败任务按规则返还；订阅过期和额度不足返回稳定错误码。

**Step 2: Run test to verify it fails**

Run: `pytest tests/test_open_api_quotas.py -q`

Expected: FAIL，因为套餐没有开放 API 额度字段和月度账本。

**Step 3: Write minimal implementation**

为 `SaasPlan` 增加开放应用和开放接口额度快照，为租户增加按月份、能力和幂等键索引的使用记录。实现事务内的预扣、确认和返还；不修改现有 `bot_limit` 语义，也不让旧 Provider 节点额度参与开放 API 计算。迁移必须支持空库和已有 MariaDB。

**Step 4: Run test to verify it passes**

Run: `pytest tests/test_open_api_quotas.py -q`

Expected: PASS，并通过 `alembic check`。

**Step 5: Commit**

```bash
git add migrations/versions/20261108_00xx_open_api_usage_quotas.py app/services/open_api_quotas.py tests/test_open_api_quotas.py app/models/saas.py app/models/entities.py app/services/billing.py
git commit -m "计费：增加开放接口套餐额度和使用账本"
```

### Task 4: 接入开放 API 鉴权、订阅和额度门禁

**Files:**
- Create: `app/services/open_api_guard.py`
- Create: `tests/test_open_api_guard.py`
- Modify: `app/services/open_api_auth.py`
- Modify: `app/api/open_api.py`

**Step 1: Write the failing test**

验证无效签名、停用应用、未授权渠道、无有效订阅、额度耗尽和频率超限分别映射为 `401`、`403`、`402` 或 `429`，并且错误响应不包含 874 原始错误和敏感信息。

**Step 2: Run test to verify it fails**

Run: `pytest tests/test_open_api_guard.py -q`

Expected: FAIL，因为现有开放 API 只做签名和渠道授权，没有统一订阅/额度门禁。

**Step 3: Write minimal implementation**

增加统一依赖函数，在路由进入业务会话前校验租户、开放应用、订阅、能力、授权渠道和请求频率。保留现有 HMAC 规则；错误统一为 `{ok:false,error:{code,message,request_id}}`。

**Step 4: Run test to verify it passes**

Run: `pytest tests/test_open_api_guard.py tests/test_open_api.py -q`

Expected: PASS。

**Step 5: Commit**

```bash
git add app/services/open_api_guard.py app/services/open_api_auth.py app/api/open_api.py tests/test_open_api_guard.py
git commit -m "安全：接入开放接口订阅和额度门禁"
```

### Task 5: 实现账号、能力、联系人和群资源 API

**Files:**
- Create: `app/api/open_accounts.py`
- Create: `app/api/open_contacts.py`
- Create: `app/api/open_groups.py`
- Create: `tests/test_open_api_resources.py`
- Modify: `app/api/__init__.py`
- Modify: `app/main.py`
- Modify: `app/services/wechat_ipad/service.py`

**Step 1: Write the failing test**

覆盖账号状态/资料/能力、联系人列表/详情、好友申请处理、群列表/详情/成员/二维码和成员新增/邀请；验证游标分页、渠道授权和不支持能力的错误码。

**Step 2: Run test to verify it fails**

Run: `pytest tests/test_open_api_resources.py -q`

Expected: FAIL，因为对应开放路由不存在。

**Step 3: Write minimal implementation**

新增资源路由和序列化器，调用已有 `wechat_ipad` 服务与数据库快照；查询使用游标和 `external_id`，写操作创建标准任务。路由只返回业务字段，不透传 874 原始响应。

**Step 4: Run test to verify it passes**

Run: `pytest tests/test_open_api_resources.py tests/test_open_api_capabilities.py -q`

Expected: PASS。

**Step 5: Commit**

```bash
git add app/api/open_accounts.py app/api/open_contacts.py app/api/open_groups.py app/api/__init__.py app/main.py app/services/wechat_ipad/service.py tests/test_open_api_resources.py
git commit -m "功能：补齐账号联系人和群资源开放接口"
```

### Task 6: 扩展消息、撤回、同步和媒体 API

**Files:**
- Create: `app/api/open_messages.py`
- Create: `app/api/open_media.py`
- Create: `tests/test_open_api_messages_media.py`
- Modify: `app/api/open_api.py`
- Modify: `app/services/task_queue.py`
- Modify: `app/services/wechat_ipad/client.py`

**Step 1: Write the failing test**

覆盖文本、图片、语音、视频、表情、链接、名片、小程序、应用消息，撤回、同步、媒体上传和受控下载；验证 `request_id` 幂等和额度预扣只发生一次。

**Step 2: Run test to verify it fails**

Run: `pytest tests/test_open_api_messages_media.py -q`

Expected: FAIL，因为现有 `/api/open/messages` 只支持少量轻量消息，媒体接口没有统一资源层。

**Step 3: Write minimal implementation**

按现有 874 client 方法构建统一 payload，增加媒体存储引用和异步任务类型；对不支持的渠道返回 `capability_not_supported`，不回退到旧 `/api/tool/*`。

**Step 4: Run test to verify it passes**

Run: `pytest tests/test_open_api_messages_media.py tests/test_open_api.py -q`

Expected: PASS。

**Step 5: Commit**

```bash
git add app/api/open_messages.py app/api/open_media.py app/api/open_api.py app/services/task_queue.py app/services/wechat_ipad/client.py tests/test_open_api_messages_media.py
git commit -m "功能：扩展开放消息撤回同步和媒体接口"
```

### Task 7: 统一 Webhook 事件和投递状态

**Files:**
- Create: `app/services/open_event_catalog.py`
- Create: `tests/test_open_api_events.py`
- Modify: `app/services/open_webhooks.py`
- Modify: `app/services/async_workers.py`
- Modify: `docs/bridge-api.md`

**Step 1: Write the failing test**

覆盖 `message.received`、`message.sent`、`message.failed`、账号在线状态和任务完成/失败事件的信封、签名、事件 ID 幂等、重试和失败记录。

**Step 2: Run test to verify it fails**

Run: `pytest tests/test_open_api_events.py -q`

Expected: FAIL，因为现有 Webhook 事件集合和状态字段还没有统一目录。

**Step 3: Write minimal implementation**

抽取事件目录和统一 envelope；复用现有异步投递 worker，增加事件类型过滤、签名和投递结果记录。外部 Webhook 失败不阻塞渠道入站处理。

**Step 4: Run test to verify it passes**

Run: `pytest tests/test_open_api_events.py tests/test_open_webhooks.py -q`

Expected: PASS。

**Step 5: Commit**

```bash
git add app/services/open_event_catalog.py app/services/open_webhooks.py app/services/async_workers.py tests/test_open_api_events.py docs/bridge-api.md
git commit -m "事件：统一开放接口 Webhook 事件协议"
```

### Task 8: 增加支付成功自动履约

**Files:**
- Create: `app/api/payment_webhooks.py`
- Create: `tests/test_payment_webhooks.py`
- Modify: `app/services/payment_gateways/base.py`
- Modify: `app/services/billing.py`
- Modify: `app/main.py`
- Modify: `docs/deployment.md`

**Step 1: Write the failing test**

覆盖支付回调签名、订单金额和订单号校验、重复回调幂等、自动创建/续期订阅、失败回调不改变订阅，以及审计记录不泄露支付凭证。

**Step 2: Run test to verify it fails**

Run: `pytest tests/test_payment_webhooks.py -q`

Expected: FAIL，因为当前只有人工提交付款凭证和平台审核流程。

**Step 3: Write minimal implementation**

按现有 `payment_gateways` 抽象增加一个明确的回调适配器；回调成功后在单事务内履约订单和订阅，重复通知直接返回原履约结果。手工审核作为已存在的运营路径保留，但不参与新自动支付路径的开放 API 门禁。

**Step 4: Run test to verify it passes**

Run: `pytest tests/test_payment_webhooks.py tests/test_public_billing_models.py -q`

Expected: PASS。

**Step 5: Commit**

```bash
git add app/api/payment_webhooks.py tests/test_payment_webhooks.py app/services/payment_gateways/base.py app/services/billing.py app/main.py docs/deployment.md
git commit -m "计费：增加支付回调自动履约订阅"
```

### Task 9: 更新开发文档和租户用量页面

**Files:**
- Modify: `docs/bridge-api.md`
- Modify: `app/templates/developer_docs.html`
- Modify: `app/templates/tenant_subscription.html`
- Modify: `app/web/billing.py`
- Create: `tests/test_open_api_docs_contract.py`

**Step 1: Write the failing test**

验证文档列出所有已实现接口、能力发现、签名示例、Webhook 信封、错误码、额度说明和自动支付状态；账户页显示订阅、开放应用数、API/消息/媒体用量。

**Step 2: Run test to verify it fails**

Run: `pytest tests/test_open_api_docs_contract.py -q`

Expected: FAIL，因为现有文档只覆盖基础开放接口。

**Step 3: Write minimal implementation**

按真实路由和字段更新 Markdown 与页面，文档中的接口清单必须从测试断言覆盖；用量页面只展示当前租户数据，不显示密钥明文和其他租户资源。

**Step 4: Run test to verify it passes**

Run: `pytest tests/test_open_api_docs_contract.py tests/test_developer_docs.py -q`

Expected: PASS。

**Step 5: Commit**

```bash
git add docs/bridge-api.md app/templates/developer_docs.html app/templates/tenant_subscription.html app/web/billing.py tests/test_open_api_docs_contract.py
git commit -m "文档：补全 874 开放接口和租户用量说明"
```

### Task 10: 集成验证和 201 发布

**Files:**
- Modify: `docs/plans/2026-11-08-open-api-874-expansion.md`
- Modify: `CHANGELOG.md`

**Step 1: Run focused validation**

Run:

```bash
pytest tests/test_open_api_capabilities.py tests/test_open_api_app_pages.py tests/test_open_api_quotas.py tests/test_open_api_guard.py tests/test_open_api_resources.py tests/test_open_api_messages_media.py tests/test_open_api_events.py tests/test_payment_webhooks.py tests/test_open_api_docs_contract.py -q
python3 -m compileall -q app
git diff --check
```

Expected: 定向测试通过、语法检查通过、差异检查无输出。未运行全量回归时，提交说明必须如实记录。

**Step 2: Run database validation**

Run: `alembic check`，再用隔离数据库执行 `alembic upgrade head`；确认现有 201 数据库升级不会修改或删除历史订阅、订单、渠道和消息数据。

**Step 3: Publish to 201**

推送 `main` 后，在 `192.168.2.201:/data/multi-bot-saas` 同步发布分支，保留当前版本备份；只重建 app 容器，不重启 MariaDB。由于 201 的 Git HTTP 凭据限制，沿用已验证的 Git bundle 同步方式，不能把新凭据写入仓库或日志。

**Step 4: Verify live behavior**

验证 201 的 `/healthz`、`/readyz`、未登录 `/developer/docs` 重定向、登录后的文档页面、开放应用管理入口、签名鉴权、额度错误和一个真实已登录账号的消息任务。真实第三方业务系统联调和完整微信能力覆盖另行记录，不以源码或 HTTP 200 代替。

**Step 5: Commit**

```bash
git add CHANGELOG.md docs/plans/2026-11-08-open-api-874-expansion.md
git commit -m "发布：完成 874 开放能力补全验证"
```
