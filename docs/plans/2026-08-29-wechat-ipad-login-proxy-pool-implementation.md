# 微信 iPad 扫码登录代理池 Implementation Plan

> **For Claude:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task.

**Goal:** 建设平台通用 SOCKS 代理池，并将微信 iPad 首次接入和重新登录重构为只呈现业务状态的统一扫码流程。

**Architecture:** 平台代理使用平台级实体统一维护，租户每次扫码创建独立登录代理会话；会话解析为统一代理字典后再交给 874 客户端。二维码生成前允许在同一省市最多轮换 3 个池代理，二维码生成后固定代理，终态立即释放；旧渠道长期代理配置和租户技术诊断入口直接删除。

**Tech Stack:** FastAPI、SQLAlchemy 2、Alembic、MariaDB、Jinja2、原生 JavaScript、Pytest、Docker Compose。

---

## 实施约束

- 每次新增或修改代码必须写详细中文注释，尤其是租户隔离、代理独占、凭证清理和状态流转。
- 每项任务完成后立即使用中文提交，不把未跟踪的 `.rcodex/` 加入提交。
- 不保留渠道 `adapter_config` 代理读取、旧代理编辑字段、旧刷新接口或租户诊断展示的兼容分支。
- 只执行本计划列出的定向测试、语法检查和部署验证，不运行全量历史回归。

### Task 1: 新增平台代理与登录代理会话模型

**Files:**
- Modify: `app/models/saas.py`
- Modify: `app/models/__init__.py`
- Create: `migrations/versions/20260829_0010_login_proxy_pool.py`
- Modify: `tests/test_saas_models.py`

**Step 1: 写模型注册和约束测试**

在 `tests/test_saas_models.py` 增加以下断言：

```python
def test_login_proxy_pool_tables_and_status_sets_are_registered() -> None:
    """平台代理和租户登录会话必须进入统一元数据，并使用唯一状态集合。"""

    assert {"platform_proxies", "login_proxy_sessions"}.issubset(Base.metadata.tables)
    assert PROXY_HEALTH_STATUSES == frozenset({"healthy", "unhealthy"})
    assert LOGIN_PROXY_MODES == frozenset({"direct", "platform_pool", "custom"})
    assert LOGIN_PROXY_SESSION_STATUSES == frozenset(
        {"preparing", "qrcode_ready", "completed", "failed", "cancelled", "expired"}
    )
```

再增加数据库约束测试，验证相同“主机 + 端口 + 用户名”不能重复、端口必须在 `1..65535`、登录会话必须带可信 `tenant_id` 和 `channel_id`。

**Step 2: 运行测试确认失败**

Run: `.venv/bin/pytest tests/test_saas_models.py -q`

Expected: FAIL，提示模型或状态常量尚不存在。

**Step 3: 实现模型**

在 `app/models/saas.py` 增加：

```python
PROXY_HEALTH_STATUSES = frozenset({"healthy", "unhealthy"})
LOGIN_PROXY_MODES = frozenset({"direct", "platform_pool", "custom"})
LOGIN_PROXY_SESSION_STATUSES = frozenset(
    {"preparing", "qrcode_ready", "completed", "failed", "cancelled", "expired"}
)
```

新增 `PlatformProxy`，包含：

- `name`、`province`、`city`、`host`、`port`、`username`、`password_encrypted`。
- `is_enabled`、`health_status`、`consecutive_failures`。
- `lease_key`、`lease_expires_at`，用于无循环外键的原子租约。
- `last_checked_at`、`last_assigned_at`、`last_success_at`、`last_failure_at`、`last_result_summary`。
- 唯一约束 `host + port + username`。
- 端口、失败次数和健康状态检查约束。

新增 `LoginProxySession(Base, TenantOwnedMixin, TimestampMixin)`，包含：

- `channel_id` 和服务端生成的唯一 `session_key`。
- `mode`、`province`、`city`、`platform_proxy_id`。
- 自有代理的 `custom_host`、`custom_port`、`custom_username`、`custom_password_encrypted`。
- `state`、`attempt_count`、`attempted_proxy_ids_json`。
- `qrcode_expires_at`、`expires_at`、`last_error_code`、`result_summary`。

`PlatformProxy` 不带租户字段；`LoginProxySession` 必须由租户会话访问。将两个模型导出到 `app/models/__init__.py`。

**Step 4: 新增 Alembic 迁移**

创建 `20260829_0010_login_proxy_pool.py`，`down_revision` 指向 `20260828_0009`。先创建 `platform_proxies`，再创建 `login_proxy_sessions`，建立唯一约束、外键和状态/时间索引。`downgrade()` 按相反顺序删除，不修改其他业务表。

**Step 5: 运行定向测试和语法检查**

Run: `.venv/bin/pytest tests/test_saas_models.py -q`

Expected: PASS。

Run: `.venv/bin/python -m compileall app/models migrations/versions`

Expected: exit 0。

**Step 6: 提交**

```bash
git add app/models/saas.py app/models/__init__.py migrations/versions/20260829_0010_login_proxy_pool.py tests/test_saas_models.py
git commit -m "功能：建立平台代理池与登录会话模型"
```

### Task 2: 实现通用代理池与登录会话服务

**Files:**
- Create: `app/services/login_proxy_pool.py`
- Create: `tests/test_login_proxy_pool.py`

**Step 1: 写代理保存和凭证安全测试**

测试以下行为：

- 新增和编辑时规范化省市、主机、端口和用户名。
- 密码使用 `encrypt_secret()` 保存，列表和异常不返回原文。
- 编辑密码留空时保留原密文。
- 重复代理返回稳定错误 `proxy_duplicate`。
- CSV 固定读取 `名称、省、市、主机、端口、用户名、密码`，逐行返回成功、跳过和失败计数。

**Step 2: 写分配和生命周期失败测试**

覆盖：

```python
def test_pool_proxy_is_exclusive_until_session_finishes(...):
    first = create_login_proxy_session(..., mode="platform_pool", province="浙江省", city="杭州市")
    with pytest.raises(LoginProxyPoolError, match="proxy_region_unavailable"):
        create_login_proxy_session(...same region...)
    finish_login_proxy_session(..., state="completed")
    second = create_login_proxy_session(...same region...)
    assert second.platform_proxy_id == first.platform_proxy_id
```

同时验证：

- 只匹配精确省市，不跨地区。
- 按最久未使用优先。
- 过期租约可重新分配。
- 自有代理密码在终态被清除。
- 池代理连续 3 次失败后变为 `unhealthy`。
- 二维码就绪后拒绝轮换，返回 `proxy_session_locked`。

**Step 3: 运行测试确认失败**

Run: `.venv/bin/pytest tests/test_login_proxy_pool.py -q`

Expected: FAIL，提示服务模块不存在。

**Step 4: 实现服务接口**

在 `app/services/login_proxy_pool.py` 实现以下稳定接口：

```python
class LoginProxyPoolError(ValueError):
    """代理池配置、分配和登录会话状态错误。"""


def save_platform_proxy(db: Session, *, proxy_id: int | None, values: dict[str, object]) -> PlatformProxy: ...
def import_platform_proxies_csv(db: Session, *, content: bytes) -> dict[str, object]: ...
def list_platform_proxies(db: Session) -> list[dict[str, object]]: ...
def build_platform_proxy_summary(db: Session) -> dict[str, int]: ...
def set_platform_proxy_enabled(db: Session, *, proxy_id: int, is_enabled: bool) -> PlatformProxy: ...
def check_platform_proxy(db: Session, *, proxy_id: int, timeout_seconds: float = 3.0) -> PlatformProxy: ...
def list_available_proxy_regions(db: Session) -> list[dict[str, object]]: ...
def create_login_proxy_session(db: Session, *, channel: Channel, mode: str, values: dict[str, object]) -> LoginProxySession: ...
def get_active_login_proxy_session(db: Session, *, channel_id: int) -> LoginProxySession | None: ...
def resolve_login_proxy_credentials(db: Session, session: LoginProxySession) -> dict[str, str] | None: ...
def rotate_login_proxy_before_qrcode(db: Session, session: LoginProxySession, *, error_code: str) -> LoginProxySession: ...
def mark_login_proxy_qrcode_ready(db: Session, session: LoginProxySession, *, qrcode_expires_at: datetime | None) -> None: ...
def finish_login_proxy_session(db: Session, session: LoginProxySession, *, state: str, error_code: str = "", summary: str = "") -> None: ...
def expire_login_proxy_sessions(db: Session) -> int: ...
```

关键实现规则：

- 使用 `select(...).with_for_update()` 锁定候选代理行。
- 只有 `is_enabled=True`、`health_status="healthy"` 且租约为空或已过期的代理可以分配。
- 每个会话生成服务端 `session_key`，代理租约只保存该键和到期时间。
- 释放时必须校验 `lease_key == session.session_key`，不能释放其他会话的新租约。
- 列表仅输出 `host` 的脱敏值，不输出密文或解密后的密码。
- `check_platform_proxy()` 只做 SOCKS 端口 TCP 连通检查；真实可用性继续由取码结果更新。

**Step 5: 运行定向测试**

Run: `.venv/bin/pytest tests/test_login_proxy_pool.py -q`

Expected: PASS。

Run: `.venv/bin/python -m compileall app/services/login_proxy_pool.py`

Expected: exit 0。

**Step 6: 提交**

```bash
git add app/services/login_proxy_pool.py tests/test_login_proxy_pool.py
git commit -m "功能：实现通用代理池分配与会话管理"
```

### Task 3: 增加系统运维代理池页面

**Files:**
- Modify: `app/web/platform_navigation.py`
- Modify: `app/web/platform_operations.py`
- Create: `app/templates/platform_proxy_pool.html`
- Modify: `app/static/css/style.css`
- Create: `tests/test_platform_proxy_pool.py`

**Step 1: 写平台权限、导航和页面测试**

测试：

- 未登录访问 `/platform/proxy-pool` 返回 401。
- 平台会话可以访问，顶级“系统运维”和二级“代理池”同时激活。
- 平台顶级导航不出现“代理池”。
- 页面包含总数、健康、异常、占用四项摘要和紧凑列表。
- 新增、编辑、启停、检查和 CSV 导入均校验平台 CSRF。
- 页面和响应不包含代理密码原文。

**Step 2: 运行测试确认失败**

Run: `.venv/bin/pytest tests/test_platform_proxy_pool.py -q`

Expected: FAIL，代理池路由或模板不存在。

**Step 3: 增加运维二级导航**

在 `_PLATFORM_OPERATIONS_NAVIGATION` 中加入：

```python
("proxy_pool", "代理池", "/platform/proxy-pool"),
```

顺序固定为：运行监控、协议服务、代理池、全局日志。

**Step 4: 实现平台路由**

在 `app/web/platform_operations.py` 增加：

- `GET /platform/proxy-pool`。
- `POST /platform/proxy-pool`。
- `POST /platform/proxy-pool/import`。
- `POST /platform/proxy-pool/{proxy_id}/toggle`。
- `POST /platform/proxy-pool/{proxy_id}/check`。

所有写操作先恢复平台员工身份并执行 `require_platform_csrf()`，再调用通用服务。平台审计只记录代理 ID、动作、地区和结果，不记录主机原文、用户名或密码。

**Step 5: 实现模板和样式**

`platform_proxy_pool.html` 使用现有 Swiss Style 页面语言：

- 顶部四项平齐摘要，不嵌套卡片。
- 主体使用可扫描的紧凑列表。
- 新增/编辑使用单个模态表单。
- 密码输入框永不写入 `value`。
- CSV 导入是次要操作，不增加大段技术说明。

在 `style.css` 中增加固定列宽、状态标识和响应式规则，移动端允许列表横向滚动，但页面本身不出现额外二级滚动条。

**Step 6: 运行定向测试**

Run: `.venv/bin/pytest tests/test_platform_proxy_pool.py tests/test_platform_operations.py tests/test_platform_protocol_services.py -q`

Expected: PASS。

**Step 7: 提交**

```bash
git add app/web/platform_navigation.py app/web/platform_operations.py app/templates/platform_proxy_pool.html app/static/css/style.css tests/test_platform_proxy_pool.py
git commit -m "界面：增加系统运维代理池管理"
```

### Task 4: 将 874 登录改为读取临时代理会话

**Files:**
- Create: `app/services/wechat_ipad/login_flow.py`
- Modify: `app/services/wechat_ipad/service.py`
- Modify: `app/main.py`
- Modify: `tests/test_wechat_ipad_qrcode_runtime.py`
- Create: `tests/test_wechat_ipad_login_proxy_flow.py`

**Step 1: 写 874 代理注入和自动轮换测试**

测试：

- `start_wechat_ipad_login(..., proxy=...)` 将统一代理字典原样交给 `WechatIpadApiClient.get_qr_code()`。
- 平台池前两个代理取码失败、第三个成功时，调用次数为 3，前两个代理释放并记录失败，第三个会话进入 `qrcode_ready`。
- 二维码就绪后再次开始不会轮换代理。
- 扫码成功、过期和明确失败都会结束代理会话并释放租约。
- `poll` 不接受客户端提交的代理 IP、代理 ID 或租户 ID。

**Step 2: 运行测试确认失败**

Run: `.venv/bin/pytest tests/test_wechat_ipad_qrcode_runtime.py tests/test_wechat_ipad_login_proxy_flow.py -q`

Expected: FAIL，当前登录仍从渠道适配配置读取代理。

**Step 3: 修改 874 登录服务参数**

将 `start_wechat_ipad_login()` 和二维码刷新所需内部函数改为显式接收：

```python
proxy: dict[str, str] | None = None
```

调用 `client.get_qr_code(..., proxy=proxy)`。删除 `_proxy_from_config()` 及所有从 `adapter_config` 获取登录代理的调用。普通心跳和消息同步不读取登录代理会话。

**Step 4: 实现统一登录编排器**

在 `app/services/wechat_ipad/login_flow.py` 实现：

```python
def start_login_with_proxy_session(db: Session, channel: Channel, *, client=None) -> dict[str, object]: ...
def poll_login_with_proxy_session(db: Session, channel: Channel, *, client=None) -> dict[str, object]: ...
def cancel_login_with_proxy_session(db: Session, channel: Channel) -> None: ...
```

编排器负责解析当前登录代理会话、调用 874、在二维码生成前最多轮换 3 次、标记二维码锁定状态，并在成功/失败/过期时释放会话。底层 874 客户端保持只理解统一代理字典。

**Step 5: 收口登录接口**

在 `app/main.py` 中：

- `POST /channels/{id}/wechat-ipad/login-flow/start` 只读取服务端当前登录代理会话。
- `POST /channels/{id}/wechat-ipad/login-flow/poll` 使用统一编排器并处理终态。
- 新增 `POST /channels/{id}/wechat-ipad/login-flow/cancel`。
- 删除旧 `login-flow/refresh` 和 `login-flow/logout` 页面调用链；重新开始必须先取消旧会话并创建新会话。
- JSON 写操作从 `X-CSRF-Token` 读取租户 CSRF，并执行 `require_tenant_csrf()`。

**Step 6: 运行定向测试**

Run: `.venv/bin/pytest tests/test_wechat_ipad_qrcode_runtime.py tests/test_wechat_ipad_login_proxy_flow.py tests/test_wechat_ipad_874_contract.py -q`

Expected: PASS。

**Step 7: 提交**

```bash
git add app/services/wechat_ipad/login_flow.py app/services/wechat_ipad/service.py app/main.py tests/test_wechat_ipad_qrcode_runtime.py tests/test_wechat_ipad_login_proxy_flow.py
git commit -m "功能：接入微信扫码登录代理会话"
```

### Task 5: 重构租户扫码页面并删除技术展示

**Files:**
- Modify: `app/main.py`
- Modify: `app/templates/channel_connect.html`
- Modify: `app/templates/channel_detail.html`
- Create: `app/templates/_wechat_ipad_login_network.html`
- Modify: `app/static/css/style.css`
- Modify: `tests/test_account_connection_pages.py`
- Modify: `tests/test_multi_channel_pages.py`

**Step 1: 写业务页面断言**

在页面测试中断言：

- 微信 iPad 配置阶段出现“智能选择、自有代理、直接登录”。
- 智能选择出现省市选择，自有代理出现 SOCKS 主机、端口、用户名和密码。
- 扫码阶段只出现“准备二维码、等待扫码、请在手机确认、登录成功”业务状态和二维码。
- 账号详情出现“重新登录”，并复用登录网络表单。
- 租户页面不出现“协议地址、API Base、UUID、二维码链接、刷新次数、CheckQR、探针、初始化登录、同步状态、原始适配配置、代理密码回显”。

**Step 2: 运行测试确认失败**

Run: `.venv/bin/pytest tests/test_account_connection_pages.py tests/test_multi_channel_pages.py -q`

Expected: FAIL，当前页面尚未提供登录网络选择且仍包含技术信息。

**Step 3: 接入页创建临时登录会话**

扩展 `/channels/connect/start` 表单参数：

```text
proxy_mode
proxy_province
proxy_city
custom_proxy_host
custom_proxy_port
custom_proxy_username
custom_proxy_password
```

创建微信 iPad 草稿后调用 `create_login_proxy_session()`；其他渠道忽略这些字段。服务端返回稳定业务错误，不把数据库错误或 874 错误拼进 URL。

账号详情的“重新登录”进入同一网络选择页面，提交后取消旧会话、废弃旧二维码并创建新会话。

**Step 4: 重构模板**

`_wechat_ipad_login_network.html` 使用单选分段控制：

- 智能选择：省、市。
- 自有代理：SOCKS 主机、端口、用户名、密码。
- 直接登录：不显示额外字段。

`channel_connect.html` 的二维码区域改为单列专注布局，自动发起取码并轮询。JS 只渲染业务状态、二维码和错误提示，请求头带 CSRF。操作只保留“重新开始”和“稍后处理”。

`channel_detail.html` 删除微信 iPad 的技术登录面板、高级连接代理字段、健康检查和诊断信息；保留账号状态、机器人、额度、最近在线、消息、联系人和群聊等业务内容。重新登录按钮进入统一扫码流程。

**Step 5: 增加稳定响应式样式**

在 `style.css` 中定义固定二维码尺寸、登录状态轨道、网络方式分段控件和表单切换区域。桌面端保持二维码居中，移动端字段单列；任何状态文字不得改变二维码容器尺寸或产生二级滚动条。

**Step 6: 运行页面测试和模板检查**

Run: `.venv/bin/pytest tests/test_account_connection_pages.py tests/test_multi_channel_pages.py tests/test_wechat_ipad_login_proxy_flow.py -q`

Expected: PASS。

Run: `.venv/bin/python -m compileall app/main.py`

Expected: exit 0。

**Step 7: 提交**

```bash
git add app/main.py app/templates/channel_connect.html app/templates/channel_detail.html app/templates/_wechat_ipad_login_network.html app/static/css/style.css tests/test_account_connection_pages.py tests/test_multi_channel_pages.py
git commit -m "界面：重构微信扫码登录业务流程"
```

### Task 6: 删除渠道长期代理兼容路径

**Files:**
- Modify: `app/main.py`
- Modify: `app/services/channel_catalog.py`
- Modify: `app/templates/channel_detail.html`
- Modify: `app/services/wechat_ipad/service.py`
- Modify: `tests/test_channel_catalog.py`
- Modify: `tests/test_wechat_ipad_874_contract.py`
- Modify: `tests/test_multi_bot_admin.py`

**Step 1: 写旧路径不存在的测试**

断言：

- 微信 iPad 渠道目录不再声明 `proxy_host`、`proxy_port`、`proxy_username`、`proxy_password`。
- `app/main.py` 不再包含 `extract_wechat_ipad_proxy_form()`。
- `merge_channel_adapter_config()` 不再接收任何代理参数。
- 微信 iPad 服务不再包含 `_proxy_from_config()`。
- 渠道编辑表单提交代理字段不会建立第二条配置路径。

**Step 2: 运行测试确认失败**

Run: `.venv/bin/pytest tests/test_channel_catalog.py tests/test_wechat_ipad_874_contract.py -q`

Expected: FAIL，当前目录和服务仍包含渠道长期代理字段。

**Step 3: 删除旧实现和旧测试**

直接删除：

- `extract_wechat_ipad_proxy_form()`。
- `merge_channel_adapter_config()` 中的代理合并逻辑。
- 渠道编辑动作的代理表单参数和保留密码逻辑。
- 渠道目录中的四个代理字段。
- 模板中的旧代理输入和展示。
- 依赖旧登录初始化、二维码刷新、同步状态和诊断控制台的租户页面测试。

保留 874 客户端构造 `ProxyIp`、`ProxyUser`、`ProxyPassword` 的通用契约，因为新登录代理会话继续调用该能力。

**Step 4: 运行定向清理检查**

Run: `rg -n "extract_wechat_ipad_proxy_form|_proxy_from_config|name=\"proxy_(host|port|username|password)\"|wechat-ipad/qrcode-refresh" app tests`

Expected: 无旧业务路径命中；新代理池模型和登录网络表单中的 `custom_proxy_*` 不属于旧渠道配置。

Run: `.venv/bin/pytest tests/test_channel_catalog.py tests/test_wechat_ipad_874_contract.py tests/test_account_connection_pages.py tests/test_multi_channel_pages.py -q`

Expected: PASS。

**Step 5: 提交**

```bash
git add app/main.py app/services/channel_catalog.py app/templates/channel_detail.html app/services/wechat_ipad/service.py tests/test_channel_catalog.py tests/test_wechat_ipad_874_contract.py tests/test_multi_bot_admin.py
git commit -m "重构：删除微信渠道长期代理配置"
```

### Task 7: 完成定向验证和研发记录

**Files:**
- Modify: `CHANGELOG.md`
- Modify: `docs/wechat_ipad_provider.md`

**Step 1: 更新说明**

记录：

- 平台运维代理池入口。
- 三种扫码网络方式。
- 登录会话内固定、终态释放、下次可重选。
- 租户扫码页删除技术诊断信息。
- 874 仅支持 SOCKS 代理。

不得写成已完成全量回归。

**Step 2: 运行定向测试集合**

Run:

```bash
.venv/bin/pytest \
  tests/test_saas_models.py \
  tests/test_login_proxy_pool.py \
  tests/test_platform_proxy_pool.py \
  tests/test_platform_operations.py \
  tests/test_platform_protocol_services.py \
  tests/test_account_connection_pages.py \
  tests/test_multi_channel_pages.py \
  tests/test_wechat_ipad_874_contract.py \
  tests/test_wechat_ipad_qrcode_runtime.py \
  tests/test_wechat_ipad_login_proxy_flow.py -q
```

Expected: PASS。

**Step 3: 运行静态检查**

Run: `.venv/bin/python -m compileall app migrations/versions tests`

Expected: exit 0。

Run: `git diff --check`

Expected: 无输出。

Run: `git status --short`

Expected: 只包含本任务文档修改和既有未跟踪 `.rcodex/`。

**Step 4: 提交**

```bash
git add CHANGELOG.md docs/wechat_ipad_provider.md
git commit -m "文档：补充扫码代理池使用说明"
```

### Task 8: 推送并部署到 142

**Files:**
- No code changes.

**Step 1: 加载远程部署能力并检查本地状态**

执行前加载 `@ssh` 技能。确认当前分支为 `feature/ipad-874-adapter`，工作区除 `.rcodex/` 外无未提交变更。

Run: `git status --short --branch && git log -8 --oneline`

Expected: 功能提交完整，`.rcodex/` 未被提交。

**Step 2: 推送分支**

Run: `git push origin feature/ipad-874-adapter`

Expected: 远端分支更新到本轮最后提交。

**Step 3: 远端差异部署**

通过 `liuwei@192.168.2.142` 登录，进入 `/data/multi-bot-saas`，执行：

```bash
git pull --ff-only origin feature/ipad-874-adapter
docker compose build app
docker compose run --rm app alembic upgrade head
docker compose up -d --no-deps app
```

不执行数据库备份，不重建 MariaDB，不删除卷。若镜像构建因外部仓库 TLS 超时失败，先确认现有应用镜像与依赖未变化，再使用当前镜像增量覆盖应用代码并重建 app 容器，仍必须执行 Alembic 迁移。

**Step 4: 验证服务和页面**

Run:

```bash
curl -fsS http://192.168.2.142:8280/healthz
curl -fsS http://192.168.2.142:8280/readyz
curl -I http://192.168.2.142:8280/platform/proxy-pool
curl -I http://192.168.2.142:8280/channels/connect?channel_type=wechat_ipad
docker compose ps
```

Expected:

- `healthz` 和 `readyz` 成功。
- 未登录页面进入正确认证边界，不出现 500。
- app 与 MariaDB 健康。
- 使用两个测试账号登录后，平台可管理代理池，租户可进入业务化扫码流程。

**Step 5: 报告实际结果**

最终报告必须列出：

- 分支和提交号。
- 实际执行的定向测试数量与结果。
- 迁移版本。
- 部署地址。
- 平台和租户两个测试账号。
- 未执行全量回归的事实。
