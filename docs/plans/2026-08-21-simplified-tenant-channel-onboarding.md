# Simplified Tenant and Channel Onboarding Implementation Plan

> **For Claude:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task.

**Goal:** 把平台租户开通和企业渠道接入收敛为少字段、可验证、自动占用额度的清晰主流程。

**Architecture:** 保留现有 `Bot`、`Channel`、`BotBinding` 和执行端模型，在其上新增通用账号连接契约与事务服务。渠道实现只负责返回可信账号身份，核心服务统一处理租户上下文、订阅额度、设备试用限制、机器人和渠道创建以及最终绑定。

**Tech Stack:** FastAPI、Jinja2、SQLAlchemy 2、Alembic、MariaDB、pytest、原生 JavaScript/CSS。

**Design:** `docs/plans/2026-08-21-simplified-tenant-channel-onboarding-design.md`

---

## 实施原则

- 每个任务只运行与当前改动直接相关的轻量测试。
- 所有新增或复杂业务代码写详细中文注释。
- 不增加旧字段别名、双写路径或未知渠道降级逻辑。
- 不允许租户手工提交可信账号 ID 或原始设备指纹。
- 每个任务完成后单独提交，提交信息使用中文。
- 遇到异常行为时先使用 `@systematic-debugging` 查明原因，再修改代码。
- 页面完成后按 `@web-design-guidelines` 做一次定向检查。

### Task 1: 建立绑定与机器人、渠道的真实关联

**Files:**
- Modify: `app/models/saas.py`
- Modify: `app/models/entities.py`
- Create: `migrations/versions/20260821_0007_bot_binding_links.py`
- Modify: `tests/test_tenant_model_coverage.py`
- Modify: `tests/test_alembic_lifecycle.py`

**Step 1: 写模型失败测试**

在 `tests/test_tenant_model_coverage.py` 增加断言，要求 `BotBinding` 具备可空但唯一的 `bot_id` 与 `channel_id` 外键，并能从绑定读取机器人和渠道。

```python
def test_bot_binding_links_to_created_bot_and_channel():
    assert BotBinding.__table__.c.bot_id.foreign_keys
    assert BotBinding.__table__.c.channel_id.foreign_keys
    assert BotBinding.bot.property.mapper.class_ is Bot
    assert BotBinding.channel.property.mapper.class_ is Channel
```

在 `tests/test_alembic_lifecycle.py` 增加 `20260821_0007` 为新 head 的断言。

**Step 2: 运行测试确认失败**

Run:

```bash
pytest -q tests/test_tenant_model_coverage.py tests/test_alembic_lifecycle.py
```

Expected: FAIL，提示 `bot_id`、`channel_id` 或新 revision 不存在。

**Step 3: 添加模型字段与关系**

在 `BotBinding` 中增加：

```python
# 绑定必须关联实际创建的机器人和主渠道；旧记录可以为空，新的账号接入事务必须填写。
bot_id: Mapped[int | None] = mapped_column(
    ForeignKey("bots.id"), nullable=True, unique=True, index=True
)
channel_id: Mapped[int | None] = mapped_column(
    ForeignKey("channels.id"), nullable=True, unique=True, index=True
)
bot = relationship("Bot")
channel = relationship("Channel")
```

只增加真实关系，不增加旧字段别名。现有绑定保留空关联，平台可以在绑定详情识别为未完成接入。

**Step 4: 编写 Alembic 迁移**

迁移 `20260821_0007_bot_binding_links.py`：

- `down_revision = "20260821_0006"`
- 新增两个可空外键列。
- 新增唯一索引或唯一约束。
- downgrade 按约束、索引、列的逆序删除。
- 不猜测或拼接历史账号关系，不做兼容性回填。

**Step 5: 运行定向测试**

Run:

```bash
pytest -q tests/test_tenant_model_coverage.py tests/test_alembic_lifecycle.py
```

Expected: PASS。

**Step 6: 提交**

```bash
git add app/models/saas.py app/models/entities.py migrations/versions/20260821_0007_bot_binding_links.py tests/test_tenant_model_coverage.py tests/test_alembic_lifecycle.py
git commit -m "模型：关联机器人额度绑定与实际渠道"
```

### Task 2: 提取可参与外层事务的额度绑定能力

**Files:**
- Modify: `app/services/bot_entitlements.py`
- Modify: `tests/test_bot_entitlements.py`

**Step 1: 写事务失败测试**

新增测试，要求内部绑定函数只 `flush` 不 `commit`，外层回滚后机器人绑定和试用领取记录都不存在。

```python
def test_add_verified_binding_rolls_back_with_outer_transaction(db_session):
    binding = add_verified_bot_binding(
        db_session,
        adapter_type="telegram",
        bot_account_id="10001",
        device_fingerprint_hash="a" * 64,
        display_name="客服机器人",
        bot_id=11,
        channel_id=21,
    )
    assert binding.id is not None
    db_session.rollback()
    assert db_session.scalar(select(BotBinding.id)) is None
```

同时覆盖额度不足、账号重复、跨租户设备占用和试用设备重复。

**Step 2: 运行测试确认失败**

Run:

```bash
pytest -q tests/test_bot_entitlements.py -k "verified_binding or outer_transaction"
```

Expected: FAIL，提示 `add_verified_bot_binding` 不存在。

**Step 3: 提取内部事务函数**

新增：

```python
def add_verified_bot_binding(
    db: Session,
    *,
    adapter_type: str,
    bot_account_id: str,
    device_fingerprint_hash: str,
    display_name: str,
    bot_id: int,
    channel_id: int,
) -> BotBinding:
    """把已验证外部身份加入当前事务，不在函数内提交。"""
```

函数复用现有额度、全局账号唯一、设备归属和试用领取规则；校验摘要必须是 64 位小写十六进制。调用 `db.flush()` 获取主键，但由外层决定提交或回滚。

保留 `bind_bot_account` 作为服务级便捷入口，让它完成原始指纹哈希后调用新函数并提交。租户页面不得再直接调用该入口。

**Step 4: 运行定向测试**

Run:

```bash
pytest -q tests/test_bot_entitlements.py
```

Expected: PASS。

**Step 5: 提交**

```bash
git add app/services/bot_entitlements.py tests/test_bot_entitlements.py
git commit -m "额度：支持账号接入事务内完成绑定"
```

### Task 3: 建立通用账号连接契约与严格注册表

**Files:**
- Create: `app/services/account_connections/__init__.py`
- Create: `app/services/account_connections/contracts.py`
- Create: `app/services/account_connections/registry.py`
- Create: `tests/test_account_connection_contracts.py`

**Step 1: 写契约失败测试**

```python
def test_unknown_account_connector_is_rejected():
    with pytest.raises(AccountConnectionError, match="connection_type_not_supported"):
        get_account_connector("unknown")


def test_verified_identity_requires_stable_account_and_device():
    with pytest.raises(AccountConnectionError, match="account_identity_invalid"):
        VerifiedAccountIdentity(
            adapter_type="telegram",
            account_id="",
            display_name="Bot",
            device_key="",
        )
```

**Step 2: 运行测试确认失败**

Run:

```bash
pytest -q tests/test_account_connection_contracts.py
```

Expected: FAIL，模块不存在。

**Step 3: 实现契约**

`contracts.py` 定义：

```python
@dataclass(frozen=True)
class VerifiedAccountIdentity:
    adapter_type: str
    account_id: str
    display_name: str
    device_key: str


@dataclass(frozen=True)
class AccountConnectionResult:
    state: Literal["connecting", "verified", "failed"]
    identity: VerifiedAccountIdentity | None
    next_action: str
    message: str


class AccountConnector(Protocol):
    channel_type: str

    def verify(self, db: Session, channel: Channel) -> AccountConnectionResult: ...
```

构造身份时调用现有 `normalize_adapter_type` 和 `normalize_bot_account_id`，拒绝空设备键。错误只使用稳定错误码。

**Step 4: 实现严格注册表**

`registry.py` 使用显式字典映射四种正式渠道。未知类型直接抛出 `connection_type_not_supported`，不返回通用 HTTP Connector。

**Step 5: 运行测试**

Run:

```bash
pytest -q tests/test_account_connection_contracts.py tests/test_channel_catalog.py
```

Expected: PASS。

**Step 6: 提交**

```bash
git add app/services/account_connections tests/test_account_connection_contracts.py
git commit -m "接入：建立通用账号连接契约"
```

### Task 4: 实现四类渠道的可信身份解析

**Files:**
- Create: `app/services/account_connections/telegram.py`
- Create: `app/services/account_connections/workwechat.py`
- Create: `app/services/account_connections/wechat_ipad.py`
- Create: `app/services/account_connections/wechat_winauto.py`
- Modify: `app/services/account_connections/registry.py`
- Modify: `app/services/channel_adapters/telegram.py`
- Modify: `app/services/channel_adapters/wechat_winauto.py`
- Create: `tests/test_account_connection_connectors.py`

**Step 1: 写四类 Connector 失败测试**

分别覆盖：

- Telegram `getMe` 返回 `id` 与 `username`。
- 企业微信账号来自当前租户的 active 节点和 online 账号。
- 微信 iPad 只接受已登录渠道中的 `wxid` 与 `device_id`。
- Windows 微信健康响应必须提供 `account_id` 与 `device_id`。
- 缺少可信账号 ID 或设备键时返回失败，禁止使用用户输入补齐。

示例：

```python
def test_telegram_connector_uses_verified_bot_id(monkeypatch, draft_channel):
    monkeypatch.setattr(
        TelegramAdapter,
        "inspect_account",
        lambda self, channel: {"ok": True, "id": 10001, "username": "sales_bot"},
    )
    result = TelegramAccountConnector().verify(db_session, draft_channel)
    assert result.identity.account_id == "10001"
    assert result.identity.device_key == "telegram-bot:10001"
```

**Step 2: 运行测试确认失败**

Run:

```bash
pytest -q tests/test_account_connection_connectors.py
```

Expected: FAIL，四类 Connector 尚不存在。

**Step 3: 实现 Telegram 与 Windows 身份检查**

- Telegram Adapter 提取无副作用的 `inspect_account(channel)`，只调用 `getMe` 并返回标准字段。
- Windows Adapter 从 `/health` 结构化响应读取账号和设备身份；响应缺失时返回 `account_identity_missing`，不使用 URL、IP 或租户表单伪造身份。

**Step 4: 实现企业微信与微信 iPad 身份检查**

- 企业微信使用 `provider_account.external_user_id or provider_account.account_key` 作为账号 ID，使用已激活节点的 `device_id` 作为设备键。
- 微信 iPad 从服务端登录结果的结构化配置读取 `wxid`、昵称和协议设备 ID；登录未完成时返回 `connecting`。
- 每个查询都在可信租户会话中执行，不能按客户端提供的 `tenant_id` 查询。

**Step 5: 注册四类 Connector**

注册表只允许 `workwechat`、`wechat_ipad`、`wechat_winauto` 和 `telegram`。

**Step 6: 运行定向测试**

Run:

```bash
pytest -q tests/test_account_connection_connectors.py tests/test_channel_catalog.py tests/test_provider_nodes.py
```

Expected: PASS。

**Step 7: 提交**

```bash
git add app/services/account_connections app/services/channel_adapters/telegram.py app/services/channel_adapters/wechat_winauto.py tests/test_account_connection_connectors.py
git commit -m "接入：实现四类渠道可信身份解析"
```

### Task 5: 实现草稿、验证与最终启用事务

**Files:**
- Create: `app/services/account_connections/service.py`
- Modify: `app/services/crud.py`
- Modify: `app/services/security.py`
- Create: `tests/test_account_connection_service.py`

**Step 1: 写主流程失败测试**

覆盖：

- 开始连接创建停用 Bot 与 `status=draft` 的 Channel，不创建 `BotBinding`。
- 验证成功后只保存账号 ID、显示名称和设备摘要，不保存原始设备键。
- 最终确认在一个事务中创建绑定、关联 bot/channel 并启用。
- 额度并发耗尽、账号重复或设备冲突时全部回滚，草稿保持未启用。
- 未验证草稿不能最终启用。

```python
def test_finalize_connection_creates_one_atomic_account(db_session, verified_draft):
    account = finalize_account_connection(
        db_session,
        channel_id=verified_draft.id,
        display_name="售后微信",
    )
    assert account.binding.bot_id == account.bot.id
    assert account.binding.channel_id == account.channel.id
    assert account.bot.is_enabled is True
    assert account.channel.is_enabled is True
```

**Step 2: 运行测试确认失败**

Run:

```bash
pytest -q tests/test_account_connection_service.py
```

Expected: FAIL，连接服务不存在。

**Step 3: 提取无提交的 CRUD 构造函数**

在 `crud.py` 增加 `add_bot` 与 `add_channel`，只负责构造、`db.add()` 和 `db.flush()`。现有需要独立提交的入口调用它们后再提交，避免账号连接服务复制加密和字段初始化逻辑。

**Step 4: 实现开始连接**

```python
def begin_account_connection(
    db: Session,
    *,
    channel_type: str,
    display_name: str,
    connection_values: Mapping[str, object],
) -> Channel:
    """创建不占额度、不可调度的连接草稿。"""
```

- 先调用 `get_bot_quota`，只确认当前有资格开始，不预占额度。
- 创建 `is_enabled=False` 的 Bot 和 Channel。
- Channel 使用 `status="draft"`。
- 只保存目录允许的连接字段。

**Step 5: 实现验证连接**

- 从注册表取得 Connector。
- 获取可信身份。
- 立即 HMAC 设备键，只把摘要写入 `runtime_info_json.account_connection`。
- 写入 `state=verified`、账号 ID、显示名称和摘要。
- 原始设备键不得进入数据库、日志或异常。

**Step 6: 实现最终事务**

- 重新锁定并读取草稿。
- 重新检查订阅和额度。
- 调用 `add_verified_bot_binding`。
- 写入 `bot_id`、`channel_id`。
- 启用 Bot 与 Channel。
- 单次 `db.commit()`；任何异常统一回滚。

**Step 7: 运行定向测试**

Run:

```bash
pytest -q tests/test_account_connection_service.py tests/test_bot_entitlements.py
```

Expected: PASS。

**Step 8: 提交**

```bash
git add app/services/account_connections/service.py app/services/crud.py app/services/security.py tests/test_account_connection_service.py
git commit -m "接入：完成账号草稿验证与原子启用"
```

### Task 6: 简化平台企业开通

**Files:**
- Modify: `app/services/platform_admin.py`
- Modify: `app/web/platform_admin.py`
- Modify: `app/templates/platform_tenants.html`
- Modify: `app/templates/platform_tenant_detail.html`
- Modify: `tests/test_platform_admin.py`
- Create: `tests/test_simplified_tenant_pages.py`

**Step 1: 写简化表单失败测试**

要求页面只提交 `name`、`admin_mobile`、`plan_id`、`expires_on` 和 CSRF；不得出现 `code`、`admin_username`、`admin_display_name`、`contact_name`、`contact_phone`、`notes` 输入框。

服务测试要求租户编码自动生成，管理员内部用户名固定为 `owner`，手机号同时成为默认联系电话。

**Step 2: 运行测试确认失败**

Run:

```bash
pytest -q tests/test_platform_admin.py tests/test_simplified_tenant_pages.py
```

Expected: FAIL，页面仍包含旧字段。

**Step 3: 收敛服务签名**

`create_tenant_with_subscription` 只接受：

```python
def create_tenant_with_subscription(
    db: Session,
    *,
    name: str,
    plan_id: int,
    expires_at: datetime | None,
    admin_mobile: str,
    actor_user_id: int | None = None,
) -> CreatedTenant:
```

内部复用 `generate_tenant_code(db)`，设置 `username="owner"`、`display_name="企业管理员"`、`contact_phone=admin_mobile`。

**Step 4: 重构平台页面**

- 企业清单放在页面首段。
- “开通企业”使用按钮打开简化弹窗。
- 套餐展示 `bot_limit`，不再显示旧节点额度。
- 租户编码和主键移入详情页折叠的“高级信息”。

**Step 5: 运行定向测试**

Run:

```bash
pytest -q tests/test_platform_admin.py tests/test_simplified_tenant_pages.py
```

Expected: PASS。

**Step 6: 提交**

```bash
git add app/services/platform_admin.py app/web/platform_admin.py app/templates/platform_tenants.html app/templates/platform_tenant_detail.html tests/test_platform_admin.py tests/test_simplified_tenant_pages.py
git commit -m "平台：简化企业开通与租户信息展示"
```

### Task 7: 建立企业账号接入向导

**Files:**
- Modify: `app/main.py`
- Create: `app/templates/channel_connect.html`
- Modify: `app/templates/channels.html`
- Modify: `app/templates/channel_detail.html`
- Modify: `app/static/css/style.css`
- Create: `tests/test_account_connection_pages.py`
- Modify: `tests/test_multi_channel_pages.py`

**Step 1: 写页面与路由失败测试**

覆盖：

- `/channels` 页面标题为“账号接入”，只展示总账号、在线账号和剩余额度。
- 账号列表不出现 Adapter Config、Webhook、API Base、Provider 节点列。
- `/channels/connect` 展示个人微信、企业微信、Telegram。
- 个人微信下一步提供 iPad 与 Windows 两种方式。
- 不同渠道只展示必要字段。
- 最终提交调用通用连接服务，不接收 `bot_account_id` 或 `device_fingerprint`。

**Step 2: 运行测试确认失败**

Run:

```bash
pytest -q tests/test_account_connection_pages.py tests/test_multi_channel_pages.py
```

Expected: FAIL，新向导不存在。

**Step 3: 添加向导路由**

在 `main.py` 添加：

```text
GET  /channels/connect
POST /channels/connect/start
POST /channels/{channel_id}/connect/verify
POST /channels/{channel_id}/connect/finalize
```

- 所有写操作要求 `tenant_admin` 和租户 CSRF。
- start 只接收渠道类型、显示名称和该渠道允许的连接字段。
- verify 从服务端草稿解析身份。
- finalize 只接收最终显示名称。
- 错误页面不回显 Token、设备键或其他秘密。

**Step 4: 构建分步模板**

`channel_connect.html` 根据服务端步骤渲染：

1. 选择平台。
2. 连接账号。
3. 确认启用。

企业微信无在线账号时提供“连接企业微信客户端”，进入重命名后的执行端页面。微信 iPad 复用现有登录服务与轮询 API，但向导只显示二维码、连接状态和重新扫码操作。

**Step 5: 重做账号列表**

`channels.html` 每行只显示账号名称与平台、连接状态、机器人、最近在线、额度状态和详情操作。空状态只保留一个“接入账号”按钮。

**Step 6: 简化账号详情**

- 首屏展示账号状态、机器人、额度和可用能力。
- 常用连接动作按渠道显示。
- 服务地址、代理和策略进入“高级设置”。
- 删除原始适配 JSON 编辑框。
- 企业微信页面使用“客户端”而不是 Provider 作为面向租户的主文案。

**Step 7: 增加定向样式**

- 使用稳定三步进度条和紧凑平台选择项。
- 不嵌套卡片，不使用大圆角或解释性宣传区块。
- 表单标签、键盘焦点、移动端宽度和错误 `aria-live` 完整。

**Step 8: 运行定向测试**

Run:

```bash
pytest -q tests/test_account_connection_pages.py tests/test_multi_channel_pages.py tests/test_provider_node_pages.py
```

Expected: PASS。

**Step 9: 提交**

```bash
git add app/main.py app/templates/channel_connect.html app/templates/channels.html app/templates/channel_detail.html app/static/css/style.css tests/test_account_connection_pages.py tests/test_multi_channel_pages.py
git commit -m "界面：上线统一企业账号接入向导"
```

### Task 8: 收敛企业导航与移除手工绑定入口

**Files:**
- Create: `app/web/tenant_navigation.py`
- Modify: `app/main.py`
- Modify: `app/web/billing.py`
- Modify: `app/web/tenant_admin.py`
- Modify: `app/web/provider_nodes.py`
- Modify: `app/templates/account_onboarding.html`
- Modify: `app/templates/account_billing.html`
- Modify: `app/templates/bots.html`
- Delete: `app/templates/account_bot_bindings.html`
- Modify: `tests/test_tenant_billing_pages.py`
- Modify: `tests/test_provider_node_pages.py`
- Modify: `tests/test_generic_channel_dashboard.py`

**Step 1: 写导航与安全失败测试**

要求：

- 主导航为“总览、账号接入、运营能力、消息、套餐、企业设置”。
- 不再显示独立“机器人”和“Provider 节点”标签。
- `/account/bot-bindings` 的 GET 与 POST 路由不存在。
- 页面中不存在租户手填 `adapter_type`、`bot_account_id` 和 `device_fingerprint` 的表单。
- `/bots` 不再提供手工新建机器人入口。

**Step 2: 运行测试确认失败**

Run:

```bash
pytest -q tests/test_tenant_billing_pages.py tests/test_provider_node_pages.py tests/test_generic_channel_dashboard.py
```

Expected: FAIL，旧导航和手工入口仍存在。

**Step 3: 建立共享租户导航**

`tenant_navigation.py` 输出统一主导航和模块二级导航。`main.py`、billing、tenant_admin 和 provider_nodes 共用该函数，避免后续标签再次漂移。

**Step 4: 移除手工绑定页面与路由**

- 删除租户 GET/POST `/account/bot-bindings`。
- 删除 `account_bot_bindings.html`。
- 套餐页只展示额度与订单入口。
- 新手引导第一动作直接进入 `/channels/connect`。

平台绑定清单和受控释放继续保留，因为它们属于平台治理，不是租户手工绑定。

**Step 5: 收口机器人页面**

- 从主导航删除机器人入口。
- 删除租户手工新建机器人按钮和 POST 创建入口。
- 已有机器人详情继续作为账号内部关联页面使用。
- 机器人创建只允许通用账号连接服务调用内部 CRUD。

**Step 6: 重命名执行端租户文案**

Provider 数据模型和机器接口名称不改，但租户页面标题统一为“企业微信客户端”。详细节点管理只从企业微信连接步骤进入。

**Step 7: 运行定向测试**

Run:

```bash
pytest -q tests/test_tenant_billing_pages.py tests/test_provider_node_pages.py tests/test_generic_channel_dashboard.py tests/test_account_connection_pages.py
```

Expected: PASS。

**Step 8: 提交**

```bash
git add app/web/tenant_navigation.py app/main.py app/web/billing.py app/web/tenant_admin.py app/web/provider_nodes.py app/templates/account_onboarding.html app/templates/account_billing.html app/templates/bots.html tests/test_tenant_billing_pages.py tests/test_provider_node_pages.py tests/test_generic_channel_dashboard.py
git rm app/templates/account_bot_bindings.html
git commit -m "导航：收敛企业账号接入与运营入口"
```

### Task 9: 完成定向审查、文档和发布准备

**Files:**
- Modify: `README.md`
- Modify: `CHANGELOG.md`
- Modify: `docs/plans/2026-08-21-simplified-tenant-channel-onboarding.md`

**Step 1: 执行页面规范检查**

按 `@web-design-guidelines` 检查本轮模板和 CSS，重点确认：

- 表单都有标签和正确 autocomplete。
- 错误区域具有 `aria-live="polite"`。
- Icon-only 按钮具有 `aria-label`。
- 焦点状态可见。
- 不存在 `transition: all`。
- 移动端不出现横向页面滚动。

发现问题时只修复本轮页面。

**Step 2: 运行聚焦测试**

Run:

```bash
pytest -q \
  tests/test_account_connection_contracts.py \
  tests/test_account_connection_connectors.py \
  tests/test_account_connection_service.py \
  tests/test_account_connection_pages.py \
  tests/test_bot_entitlements.py \
  tests/test_platform_admin.py \
  tests/test_simplified_tenant_pages.py \
  tests/test_multi_channel_pages.py \
  tests/test_provider_node_pages.py \
  tests/test_tenant_billing_pages.py
```

Expected: PASS。不要表述为完整回归。

**Step 3: 运行语法和差异检查**

Run:

```bash
python -m py_compile \
  app/services/account_connections/contracts.py \
  app/services/account_connections/registry.py \
  app/services/account_connections/service.py \
  app/services/account_connections/telegram.py \
  app/services/account_connections/workwechat.py \
  app/services/account_connections/wechat_ipad.py \
  app/services/account_connections/wechat_winauto.py \
  app/services/bot_entitlements.py \
  app/services/platform_admin.py \
  app/web/platform_admin.py \
  app/main.py
git diff --check
```

Expected: 无输出且退出码为 0。

**Step 4: 做基础桌面和移动端检查**

只检查：

- 平台企业清单与开通弹窗。
- 企业账号清单。
- 渠道选择步骤。
- 一个凭证型连接步骤。
- 一个扫码或客户端型连接步骤。

使用 `1536x864` 和 `390x844`，确认无遮挡、无页面横向滚动、表单按钮可操作。无需运行完整 Playwright 套件。

**Step 5: 更新文档**

README 说明新的企业开通与账号接入流程。CHANGELOG 记录手工账号绑定入口移除、四类连接器和高级配置收口。计划末尾补充实际执行的测试及未验证范围。

**Step 6: 提交**

```bash
git add README.md CHANGELOG.md docs/plans/2026-08-21-simplified-tenant-channel-onboarding.md
git commit -m "文档：更新企业账号接入与验证说明"
```

**Step 7: 发布前确认**

```bash
git status --short --branch
git log -9 --oneline
```

Expected: 除用户自己的 `.rcodex/` 外无未提交文件。推送和部署在用户明确要求发布时执行。
