# 曜枢 AI 营销注册和计费 MVP Implementation Plan

> **For Claude:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task.

**Goal:** 在现有 FastAPI 单体内增加曜枢 AI 营销首页、企业自助注册、7 天试用、人工付款订单、平台审核和通用机器人额度绑定闭环。

**Architecture:** 继续复用现有租户、会话、订阅和平台后台，在 `app/web` 与 `app/services` 下增加独立公开站点、计费和额度模块。商业化模型只使用通用 `bot_limit`、`adapter_type` 和 `bot_account_id`，旧 Provider 模型和 `provider_node_limit` 暂时保留兼容，不进入新订单与额度服务。

**Tech Stack:** Python 3.11、FastAPI、SQLAlchemy 2.x、Alembic、Jinja2、MariaDB/SQLite、Pytest、Playwright、Docker Compose。

---

## 执行约束

- 每次产品代码修改必须写详细中文注释，说明安全边界、事务边界和兼容原因；
- 每个任务结束后执行与本次修改直接相关的轻量测试、`git diff --check` 并中文提交；
- 不把全量历史测试作为本阶段门禁；
- 不在日志、审计或页面源码中输出密码、设备原始指纹、付款凭证正文或内部存储路径；
- 不重命名仓库、Python 包、数据库或旧接口；
- 不主动重构旧 Provider 业务，只通过通用模型和服务承接新商业化能力。

### Task 1: 增加商业化数据模型和 Alembic 迁移

**Files:**
- Create: `migrations/versions/20260821_0006_public_billing.py`
- Modify: `app/models/saas.py`
- Modify: `app/models/__init__.py`
- Create: `tests/test_public_billing_models.py`

**Step 1: 写模型注册和约束失败测试**

在 `tests/test_public_billing_models.py` 中覆盖：

```python
def test_public_billing_models_are_registered() -> None:
    assert {
        "billing_orders",
        "bot_bindings",
        "device_trial_claims",
    }.issubset(Base.metadata.tables)


def test_plan_exposes_generic_bot_quota_and_prices(db: Session) -> None:
    plan = SaasPlan(
        code="starter",
        name="入门版",
        provider_node_limit=1,
        bot_limit=1,
        monthly_price_cents=9900,
        yearly_price_cents=99000,
        is_public=True,
    )
    db.add(plan)
    db.commit()
    assert plan.bot_limit == 1
```

增加手机号唯一、订单号唯一、机器人账号全局唯一和设备试用摘要唯一测试。

**Step 2: 运行测试确认失败**

Run:

```bash
.venv/bin/python -m pytest -q tests/test_public_billing_models.py
```

Expected: FAIL，提示新模型或字段不存在。

**Step 3: 实现模型**

在 `app/models/saas.py` 中：

- `TenantUser` 增加 `mobile` 和 `is_owner`；
- `SaasPlan` 增加 `bot_limit`、价格、试用、公开展示字段；
- 新增 `BillingOrder`；
- 新增 `BotBinding`；
- 新增 `DeviceTrialClaim`；
- 为状态字段增加数据库 CheckConstraint；
- 为关系字段增加明确外键和索引；
- 为设备摘要和付款凭证字段增加中文安全注释。

`BillingOrder` 至少包含：

```python
order_no
tenant_id
plan_id
billing_cycle
bot_limit_snapshot
amount_cents
status
payment_reference
payment_proof_path
payment_submitted_at
reviewed_by_user_id
reviewed_at
rejection_reason
fulfilled_subscription_id
```

`BotBinding` 使用 `(adapter_type, bot_account_id)` 唯一约束，首版通过软释放保留历史记录；服务层在释放后仍不得让试用设备再次领取试用。

**Step 4: 编写迁移**

`20260821_0006_public_billing.py`：

- `down_revision = "20260801_0005"`；
- 新增字段和三张表；
- 将现有 `provider_node_limit` 回填到 `bot_limit`；
- 插入或补齐 `trial`、`starter`、`standard`、`professional` 四个套餐；
- 迁移不能覆盖平台管理员已经修改的同编码套餐；
- downgrade 只删除本迁移新增结构，不删除旧 Provider 字段。

**Step 5: 运行定向测试和迁移检查**

```bash
.venv/bin/python -m pytest -q tests/test_public_billing_models.py
.venv/bin/alembic upgrade head
.venv/bin/alembic heads
git diff --check
```

Expected: 模型测试通过，Alembic 只有 `20260821_0006` 一个 head。

**Step 6: 提交**

```bash
git add app/models/saas.py app/models/__init__.py migrations/versions/20260821_0006_public_billing.py tests/test_public_billing_models.py
git commit -m "数据库：增加套餐计费和机器人绑定模型"
```

### Task 2: 扩展平台套餐管理

**Files:**
- Modify: `app/services/platform_admin.py`
- Modify: `app/web/platform_admin.py`
- Modify: `app/templates/platform_plans.html`
- Modify: `tests/test_platform_admin.py`

**Step 1: 写套餐价格服务失败测试**

增加 `save_plan()` 保存通用机器人额度、月价、年价、公开状态、推荐状态和展示顺序的测试，并断言负数价格、负额度和非法试用天数被拒绝。

**Step 2: 运行测试确认失败**

```bash
.venv/bin/python -m pytest -q tests/test_platform_admin.py
```

Expected: FAIL，`save_plan()` 不接受新字段。

**Step 3: 实现服务和页面**

- `save_plan()` 增加新字段，所有金额统一接收整数分；
- 平台页面表单以元为单位输入，Router 使用 `Decimal` 精确转换为分，禁止浮点金额；
- 公开套餐必须有正数 `bot_limit` 和非负价格；
- 保留 `provider_node_limit` 表单兼容旧功能，但新增页面明确区分“机器人额度”和“旧节点额度”；
- 套餐表格增加月价、年价、公开和推荐状态。

**Step 4: 运行轻量测试**

```bash
.venv/bin/python -m pytest -q tests/test_platform_admin.py
git diff --check
```

**Step 5: 提交**

```bash
git add app/services/platform_admin.py app/web/platform_admin.py app/templates/platform_plans.html tests/test_platform_admin.py
git commit -m "平台：扩展公开套餐价格和机器人额度管理"
```

### Task 3: 实现企业注册和 7 天试用事务

**Files:**
- Create: `app/services/registration.py`
- Modify: `app/services/saas_auth.py`
- Create: `tests/test_public_registration.py`

**Step 1: 写注册失败测试**

覆盖：

- 手机号规范化后全局唯一；
- 企业编码由服务端生成；
- 所有者、租户和试用订阅一次提交；
- 用户自设密码后 `must_change_password=False`；
- 密码哈希失败时全部回滚；
- 试用套餐不存在或停用时拒绝注册；
- 企业主可以通过手机号认证，员工认证仍使用企业编码和用户名。

**Step 2: 运行测试确认失败**

```bash
.venv/bin/python -m pytest -q tests/test_public_registration.py
```

**Step 3: 实现注册服务**

新增：

```python
normalize_mobile()
generate_tenant_code()
register_trial_tenant()
authenticate_tenant_owner()
```

`register_trial_tenant()` 必须使用当前 SQLAlchemy Session 的单一事务，不在服务内部提前提交多次。企业编码使用随机 URL 安全字符，不从企业名称拼音推导，避免重复和敏感信息泄露。

**Step 4: 运行定向测试**

```bash
.venv/bin/python -m pytest -q tests/test_public_registration.py
git diff --check
```

**Step 5: 提交**

```bash
git add app/services/registration.py app/services/saas_auth.py tests/test_public_registration.py
git commit -m "注册：增加企业自助开户和七天试用"
```

### Task 4: 建立公开站点 Router 和双登录入口

**Files:**
- Create: `app/web/public_site.py`
- Modify: `app/web/tenant_auth.py`
- Modify: `app/web/auth_common.py`
- Modify: `app/web/__init__.py`
- Modify: `app/main.py`
- Create: `app/templates/marketing_base.html`
- Create: `app/templates/marketing_home.html`
- Create: `app/templates/pricing.html`
- Create: `app/templates/register.html`
- Create: `app/templates/owner_login.html`
- Modify: `app/templates/login.html`
- Create: `tests/test_public_site_auth.py`

**Step 1: 写公开路由和身份边界测试**

断言：

- `/`、`/pricing`、`/register`、`/login` 无 Cookie 可访问；
- `/dashboard` 仍要求租户登录；
- 企业主 `/login` 只接收手机号和密码；
- 员工入口移动到 `/employee/login`；
- 原 `/login` 员工 POST 不再接受 `tenant_code`；
- 登录成功继续签发现有租户 Cookie；
- 平台 Cookie 不能登录企业主入口；
- setup 未完成时仍优先进入 `/setup`。

**Step 2: 运行测试确认失败**

```bash
.venv/bin/python -m pytest -q tests/test_public_site_auth.py
```

**Step 3: 实现 Router 和中间件调整**

- `public_site.py` 负责首页、价格、注册和企业主登录；
- 将现有员工登录常量改为 `/employee/login`；
- 将原租户仪表盘从 `/` 调整为 `/dashboard`；
- 公开路由加入认证中间件白名单；
- 已登录企业主访问登录或注册页时跳转 `/dashboard`；
- 首页根据当前租户会话显示“进入控制台”或“免费试用”，但不能泄漏租户信息；
- 注册成功后创建会话并跳转 `/account/onboarding`。

**Step 4: 增加基础模板**

模板先完成语义结构、表单字段、服务端错误展示和无脚本可用性，不在本任务追求最终视觉。

**Step 5: 运行定向测试**

```bash
.venv/bin/python -m pytest -q tests/test_public_site_auth.py tests/test_saas_login.py
git diff --check
```

**Step 6: 提交**

```bash
git add app/web/public_site.py app/web/tenant_auth.py app/web/auth_common.py app/web/__init__.py app/main.py app/templates/marketing_base.html app/templates/marketing_home.html app/templates/pricing.html app/templates/register.html app/templates/owner_login.html app/templates/login.html tests/test_public_site_auth.py tests/test_saas_login.py
git commit -m "页面：增加曜枢AI公开站点和企业主登录"
```

### Task 5: 实现订单服务和人工付款抽象

**Files:**
- Create: `app/services/payment_gateways/__init__.py`
- Create: `app/services/payment_gateways/base.py`
- Create: `app/services/payment_gateways/manual.py`
- Create: `app/services/billing.py`
- Modify: `app/core/settings.py`
- Modify: `.env.example`
- Modify: `.env.docker.example`
- Create: `tests/test_billing_service.py`

**Step 1: 写订单状态和金额失败测试**

覆盖：

- 客户端伪造金额无效；
- 月付和年付只读取套餐价格；
- 同一租户同一套餐存在未完成订单时复用或拒绝重复创建；
- 付款凭证提交把状态改为 `payment_submitted`；
- 被驳回订单可以重新提交；
- 已批准或取消订单不能再次提交付款；
- 订单查询强制使用可信租户范围。

**Step 2: 运行测试确认失败**

```bash
.venv/bin/python -m pytest -q tests/test_billing_service.py
```

**Step 3: 实现 PaymentGateway**

`base.py` 使用 `Protocol` 或 ABC 定义统一付款说明结构。`manual.py` 从 Settings 读取：

```text
BILLING_PAYMENT_TITLE
BILLING_PAYMENT_INSTRUCTIONS
BILLING_PAYMENT_QR_IMAGE
BILLING_PAYMENT_BANK_DETAILS
```

首版不执行外部网络请求。

**Step 4: 实现订单服务**

新增：

```python
list_public_plans()
calculate_plan_amount()
create_billing_order()
get_tenant_order()
list_tenant_orders()
submit_payment_evidence()
cancel_order()
approve_order()
reject_order()
```

金额使用整数分。`approve_order()` 使用事务和行锁或等价的状态条件更新保证幂等；审核成功后调用现有订阅服务的受控内部能力，不能通过模拟平台页面请求开通。

**Step 5: 运行定向测试**

```bash
.venv/bin/python -m pytest -q tests/test_billing_service.py
git diff --check
```

**Step 6: 提交**

```bash
git add app/services/payment_gateways app/services/billing.py app/core/settings.py .env.example .env.docker.example tests/test_billing_service.py
git commit -m "计费：增加人工付款订单和订阅开通服务"
```

### Task 6: 增加租户订单、套餐和付款凭证页面

**Files:**
- Create: `app/web/billing.py`
- Modify: `app/web/__init__.py`
- Modify: `app/main.py`
- Create: `app/templates/account_onboarding.html`
- Create: `app/templates/account_billing.html`
- Create: `app/templates/account_orders.html`
- Create: `app/templates/account_order_detail.html`
- Modify: `app/templates/base.html`
- Create: `tests/test_tenant_billing_pages.py`

**Step 1: 写页面权限和上传失败测试**

覆盖：

- 只有 `tenant_admin` 或企业所有者可以创建订单；
- 普通员工可以按产品决定只读查看，不能上传付款凭证；
- 其他租户订单返回 404；
- 凭证只允许 PNG、JPEG、WebP 和 PDF；
- 文件大小限制，文件名和真实路径经过清理；
- 页面不显示服务器内部路径；
- 取消和提交付款均校验租户 CSRF。

**Step 2: 运行测试确认失败**

```bash
.venv/bin/python -m pytest -q tests/test_tenant_billing_pages.py
```

**Step 3: 实现凭证私有存储**

凭证保存到：

```text
data/tenant_billing/{tenant_id}/{order_no}/proof.<ext>
```

下载必须经过受控 Route，不通过 `StaticFiles` 暴露。服务端使用解析后的真实路径验证文件仍位于租户订单目录内。

**Step 4: 实现 Router 和模板**

- 新手引导显示试用到期时间和三个下一步；
- 套餐与额度页显示当前套餐、到期时间、已用额度和购买入口；
- 订单列表和详情展示准确状态；
- `payment_submitted` 文案使用“已提交付款凭证，等待审核”，不能写“支付成功”。

**Step 5: 运行定向测试**

```bash
.venv/bin/python -m pytest -q tests/test_tenant_billing_pages.py tests/test_tenant_web_isolation.py
git diff --check
```

**Step 6: 提交**

```bash
git add app/web/billing.py app/web/__init__.py app/main.py app/templates/account_onboarding.html app/templates/account_billing.html app/templates/account_orders.html app/templates/account_order_detail.html app/templates/base.html tests/test_tenant_billing_pages.py
git commit -m "租户：增加套餐订单和付款凭证流程"
```

### Task 7: 增加平台订单审核页面

**Files:**
- Create: `app/web/platform_billing.py`
- Modify: `app/web/__init__.py`
- Modify: `app/main.py`
- Modify: `app/web/platform_admin.py`
- Create: `app/templates/platform_billing_orders.html`
- Create: `app/templates/platform_billing_order_detail.html`
- Create: `tests/test_platform_billing_pages.py`

**Step 1: 写平台审核失败测试**

覆盖：

- 租户 Cookie 不能访问平台订单；
- 平台 CSRF 必须匹配；
- 只有 `payment_submitted` 订单可以批准；
- 重复批准返回幂等成功且只有一条新订阅；
- 驳回必须填写原因；
- 平台下载凭证时不暴露存储路径；
- 审核结果写入 `PlatformAuditLog`。

**Step 2: 实现 Router 和页面**

- 平台导航增加“订单审核”；
- 列表默认把待审核订单排在最前；
- 详情展示企业、套餐、金额、流水号、凭证和历史状态；
- 批准与驳回使用明确确认页面或共享确认层，普通点击必须可用。

**Step 3: 运行定向测试**

```bash
.venv/bin/python -m pytest -q tests/test_platform_billing_pages.py tests/test_platform_admin.py
git diff --check
```

**Step 4: 提交**

```bash
git add app/web/platform_billing.py app/web/__init__.py app/main.py app/web/platform_admin.py app/templates/platform_billing_orders.html app/templates/platform_billing_order_detail.html tests/test_platform_billing_pages.py
git commit -m "平台：增加付款订单审核和订阅履约"
```

### Task 8: 实现通用机器人额度和防滥用绑定

**Files:**
- Create: `app/services/bot_entitlements.py`
- Modify: `app/web/billing.py`
- Modify: `app/web/platform_billing.py`
- Create: `app/templates/account_bot_bindings.html`
- Create: `app/templates/platform_bot_binding_detail.html`
- Create: `tests/test_bot_entitlements.py`

**Step 1: 写绑定规则失败测试**

覆盖：

- 有效绑定数不能超过当前订阅 `bot_limit`；
- `(adapter_type, bot_account_id)` 不能跨租户重复；
- 设备指纹只以 SHA-256 摘要保存；
- 同一设备只能归属一个租户；
- 同一设备只能创建一条 `DeviceTrialClaim`；
- 同一付费租户可以在一个设备绑定多个机器人；
- 试用用户不能自行释放；
- 平台释放保留历史和审计；
- 到期订阅不能新增绑定。

**Step 2: 实现服务**

新增：

```python
normalize_adapter_type()
normalize_bot_account_id()
hash_device_fingerprint()
get_bot_quota()
bind_bot_account()
list_bot_bindings()
release_bot_binding_by_platform()
```

服务方法必须从可信租户 Session 读取 `tenant_id`，不能接受客户端提交的租户归属。设备原始指纹仅在当前请求内参与哈希，不能写入日志、异常或返回值。

**Step 3: 实现页面**

- 租户页面显示额度、绑定账号掩码、适配类型、状态和绑定时间；
- 不提供用户解绑按钮；
- 平台页面提供带原因的受控释放动作；
- 首版绑定表单保留通用 `adapter_type` 和 `bot_account_id`，不引用 Provider 节点字段。

**Step 4: 运行定向测试**

```bash
.venv/bin/python -m pytest -q tests/test_bot_entitlements.py
git diff --check
```

**Step 5: 提交**

```bash
git add app/services/bot_entitlements.py app/web/billing.py app/web/platform_billing.py app/templates/account_bot_bindings.html app/templates/platform_bot_binding_detail.html tests/test_bot_entitlements.py
git commit -m "额度：增加通用机器人绑定和试用防滥用"
```

### Task 9: 完成曜枢 AI 营销视觉和响应式页面

**Files:**
- Modify: `app/templates/marketing_base.html`
- Modify: `app/templates/marketing_home.html`
- Modify: `app/templates/pricing.html`
- Modify: `app/templates/register.html`
- Modify: `app/templates/owner_login.html`
- Modify: `app/templates/login.html`
- Modify: `app/static/css/style.css`
- Create: `app/static/images/yaoshu-console.webp`
- Create: `tests/test_public_marketing_pages.py`

**Step 1: 写营销文案和结构测试**

断言页面包含：

- “曜枢 AI”；
- “一个后台，统一运营所有智能机器人”；
- “免费试用 7 天”；
- 三个公开套餐及服务端价格；
- 不出现“仅企业微信”或 Provider 作为产品定位；
- 注册和登录表单有正确 label、autocomplete 和错误容器。

**Step 2: 实现营销页面**

- 导航含产品能力、使用流程、套餐价格、登录和免费试用；
- H1 必须为“曜枢 AI”；
- 首屏使用真实控制台截图作为全幅背景，文字不放卡片；
- 首屏在所有桌面和移动视口露出下一段内容；
- 页面段落使用全宽分区，不把页面 section 包成漂浮卡片；
- 套餐项可以使用独立卡片，圆角不超过 8px；
- 标准版使用明确推荐标识；
- 不使用紫色渐变、装饰光球、负字距或随视口缩放字体。

**Step 3: 生成真实产品截图资产**

启动隔离测试服务，创建不含真实客户信息的演示租户，使用 Playwright 在桌面视口截图控制台并转为 WebP。截图必须展示实际界面，不使用模糊库存图。

**Step 4: 运行模板测试和浏览器检查**

```bash
.venv/bin/python -m pytest -q tests/test_public_marketing_pages.py tests/test_public_site_auth.py
git diff --check
```

Playwright 检查：

- `1440x1000`；
- `390x844`；
- 首页、套餐、注册、企业主登录、员工登录、订单详情；
- 普通点击，无强制点击；
- 无页面级横向溢出、文字重叠、控制台错误或失败资源。

**Step 5: 提交**

```bash
git add app/templates/marketing_base.html app/templates/marketing_home.html app/templates/pricing.html app/templates/register.html app/templates/owner_login.html app/templates/login.html app/static/css/style.css app/static/images/yaoshu-console.webp tests/test_public_marketing_pages.py
git commit -m "页面：完成曜枢AI营销站和商业化入口"
```

### Task 10: 更新文档并执行本地集成验收

**Files:**
- Modify: `README.md`
- Modify: `docs/deployment.md`
- Modify: `scripts/saas_smoke_test.sh`
- Create: `tests/test_public_commercial_flow.py`

**Step 1: 增加商业闭环冒烟测试**

使用临时 SQLite 数据库验证：

```text
公开套餐 → 注册 → 试用 → 创建订单 → 提交凭证
→ 平台审核 → 订阅生效 → 绑定机器人 → 额度限制
```

测试不得写生产数据库，也不得把付款凭证和设备原始指纹输出到日志。

**Step 2: 更新文档**

- README 改为曜枢 AI 对外品牌和全渠道定位；
- 记录企业主与员工两个登录入口；
- 记录人工付款配置；
- 记录付款凭证私有目录和备份要求；
- 记录默认套餐和平台可编辑边界；
- 部署文档增加迁移、订单审核和回滚检查。

**Step 3: 运行轻量集成检查**

```bash
.venv/bin/python -m pytest -q \
  tests/test_public_billing_models.py \
  tests/test_public_registration.py \
  tests/test_public_site_auth.py \
  tests/test_billing_service.py \
  tests/test_tenant_billing_pages.py \
  tests/test_platform_billing_pages.py \
  tests/test_bot_entitlements.py \
  tests/test_public_marketing_pages.py \
  tests/test_public_commercial_flow.py
.venv/bin/alembic heads
docker compose config
git diff --check
```

Expected: 所列定向测试通过、Alembic 单一 head、Compose 配置有效。不得表述为全量回归完成。

**Step 4: 提交**

```bash
git add README.md docs/deployment.md scripts/saas_smoke_test.sh tests/test_public_commercial_flow.py
git commit -m "文档：补充曜枢AI注册计费和部署说明"
```

### Task 11: 推送、备份、部署和线上验收

**Files:**
- Verify: `.git/config`
- Remote: `192.168.2.142:/data/multi-bot-saas`

**Step 1: 推送**

```bash
git status --short
git push origin main
```

Expected: 工作树干净，`origin/main` 与本地 HEAD 一致。

**Step 2: 加载 SSH 技能并只读盘点**

确认远程当前提交、容器、磁盘、MariaDB、`8280`、旧 `8180` 和备份目录。不得输出 `.env` 或数据库密码。

**Step 3: 远程备份与部署**

- 备份环境文件和 MariaDB；
- 备份权限为 `600`；
- `git pull --ff-only`；
- 构建新镜像；
- 先执行 `alembic upgrade head`；
- 迁移成功后只重建 `multi-bot-saas-app`；
- 不重启旧 `8180` 服务。

**Step 4: 线上验收**

验证：

- `/healthz` 和 `/readyz` 为 200；
- `/`、`/pricing`、`/register`、`/login` 和 `/employee/login` 正常；
- 使用测试手机号完成注册和 7 天试用；
- 创建测试订单、提交非敏感测试凭证、平台审核并确认额度生效；
- 同一测试设备第二次领取试用被拒绝；
- 新容器镜像、Git HEAD 和静态资源一致；
- 旧 `8180` 服务仍正常；
- 最近日志没有密码、设备原始指纹、付款凭证内容或异常堆栈。

**Step 5: 记录实际验证结果**

如仅需更新部署说明，单独中文提交并再次推送。最终说明必须明确列出实际运行的定向测试，不能声称完成全量测试。
