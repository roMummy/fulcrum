# Multi Bot SaaS Implementation Plan

> **For Claude:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task.

**Goal:** 将现有 `multi-bot-admin` 安全复制为独立的 `multi-bot-saas`，实现共享 MariaDB 下的强制租户隔离、多员工账号、Provider 节点额度，以及企业微信 Windows Provider 的节点级主动连接。

**Architecture:** 云端继续使用 FastAPI、SQLAlchemy 和 Jinja2，但把平台身份、租户身份和 Provider 设备身份拆成三条独立认证链路。所有租户业务模型显式保存 `tenant_id`，SQLAlchemy 租户会话自动增加读取过滤并校验写入归属；Provider 使用一次性激活码换取节点设备密钥，再通过 HTTPS 心跳和长轮询处理节点下多个企业微信渠道。

**Tech Stack:** Python 3.11、FastAPI、SQLAlchemy 2.x、Alembic、Jinja2、MariaDB 11.4、Argon2id、Pytest、Docker Compose、Windows 企业微信 Provider。

**Design:** `docs/plans/2026-07-30-multi-bot-saas-design.md`

**Simplified Bot Baseline:** `docs/plans/2026-07-30-simplify-bot-lifecycle-design.md`。后续迁移和租户化必须以精简后的机器人模型为准，不得重新引入分组、模板、标签、slug、环境、自定义命令前缀或复制流程。

---

## 执行规则

- Task 1 之前只允许修改 `/data/multi-bot-admin/docs/plans`；Task 1 完成后，所有实现只在 `/data/multi-bot-saas` 中进行。
- 源项目当前存在用户未提交的 `app/api/open_api.py` 和 `tests/test_open_api.py` 修改；复制必须基于已提交的 `HEAD`，不得把这两处工作区修改带入新项目。
- 所有手工文件修改使用 `apply_patch`，复杂逻辑书写详细中文注释。
- 每个任务先写失败测试，再实现最小代码，再运行目标测试和相关回归测试。
- 每个任务独立使用中文提交信息；提交前执行 `git diff --check` 和 `git status --short`。
- 测试失败或行为与预期不一致时使用 `@systematic-debugging`，先找根因再改代码。
- 任何密钥、密码、激活码或设备令牌只验证存在性和摘要，不输出真实内容。

### Task 1: 创建独立项目和 Git 基线

**Files:**
- Create: `/data/multi-bot-saas/**`（从源项目已提交 `HEAD` 导出）
- Verify: `/data/multi-bot-admin/app/api/open_api.py`
- Verify: `/data/multi-bot-admin/tests/test_open_api.py`

**Step 1: 验证源项目和目标目录**

Run:

```bash
cd /data/multi-bot-admin
git status --short
git log -1 --oneline
test ! -e /data/multi-bot-saas
```

Expected: 只看到源项目已有的两处用户修改；目标目录不存在。

**Step 2: 从已提交 HEAD 导出源码**

Run:

```bash
saas_stage_dir="$(mktemp -d /tmp/multi-bot-saas.XXXXXX)"
git archive --format=tar --output="$saas_stage_dir/source.tar" HEAD
mkdir /data/multi-bot-saas
tar -xf "$saas_stage_dir/source.tar" -C /data/multi-bot-saas
```

Expected: 新目录不包含 `.git`、`.env`、数据库、日志、虚拟环境或源工作区未提交差异。

**Step 3: 初始化独立仓库**

Run:

```bash
cd /data/multi-bot-saas
git init -b main
git add .
git commit -m "初始化：复制企业微信SaaS项目基线"
```

Expected: 产生独立根提交，且没有远程地址。

**Step 4: 验证基线测试**

Run:

```bash
cd /data/multi-bot-saas
.venv/bin/python -m pytest -q
```

如果新目录没有虚拟环境，先运行：

```bash
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt pytest
.venv/bin/python -m pytest -q
```

Expected: 复制前已有测试全部通过；如基线本身失败，先记录并按 `@systematic-debugging` 定位，不能把已有失败算作 SaaS 改造结果。

### Task 2: 更名并隔离本地运行配置

**Files:**
- Modify: `/data/multi-bot-saas/app/core/settings.py`
- Modify: `/data/multi-bot-saas/app/main.py`
- Modify: `/data/multi-bot-saas/app/templates/base.html`
- Modify: `/data/multi-bot-saas/README.md`
- Modify: `/data/multi-bot-saas/.env.example`
- Modify: `/data/multi-bot-saas/.env.docker.example`
- Modify: `/data/multi-bot-saas/docker-compose.yml`
- Modify: `/data/multi-bot-saas/scripts/deploy_restart_mba.sh`
- Test: `/data/multi-bot-saas/tests/test_saas_branding.py`

**Step 1: 写品牌和隔离配置失败测试**

```python
def test_healthz_uses_saas_service_name(client):
    response = client.get("/healthz")
    assert response.status_code == 200
    assert response.json()["service"] == "multi-bot-saas"


def test_default_settings_use_saas_name():
    from app.core.settings import Settings

    assert Settings(_env_file=None).app_name == "Multi Bot SaaS"
```

**Step 2: 运行测试确认失败**

Run: `.venv/bin/python -m pytest tests/test_saas_branding.py -q`

Expected: 旧服务名仍为 `multi-bot-admin`。

**Step 3: 修改品牌和 Compose 隔离项**

实现要求：

- 默认应用名改为 `Multi Bot SaaS`；
- 健康检查服务名改为 `multi-bot-saas`；
- 默认服务端口使用 `8280`；
- 容器名使用 `multi-bot-saas-app` 和 `multi-bot-saas-mariadb`；
- 数据库名、用户和卷使用 `multi_bot_saas` 前缀；
- MariaDB 密码改为从环境变量读取，不在 Compose 中写固定生产密码；
- 页面品牌文案改为 `Multi Bot SaaS`；
- 部署脚本变量从 `MBA_*` 改为 `MBS_*`，并保留详细中文注释说明兼容边界。

**Step 4: 运行目标测试和配置检查**

Run:

```bash
.venv/bin/python -m pytest tests/test_saas_branding.py -q
docker compose config >/tmp/multi-bot-saas-compose.yml
rg -n "multi-bot-admin|8180" app README.md docker-compose.yml scripts .env.example .env.docker.example
```

Expected: 测试通过；剩余旧名称只允许出现在历史说明或迁移文档中。

**Step 5: 提交**

```bash
git add app/core/settings.py app/main.py app/templates/base.html README.md .env.example .env.docker.example docker-compose.yml scripts/deploy_restart_mba.sh tests/test_saas_branding.py
git commit -m "基础：完成SaaS项目更名和运行隔离"
```

### Task 3: 建立 Alembic 现有结构基线

**Files:**
- Modify: `/data/multi-bot-saas/requirements.txt`
- Create: `/data/multi-bot-saas/alembic.ini`
- Create: `/data/multi-bot-saas/migrations/env.py`
- Create: `/data/multi-bot-saas/migrations/script.py.mako`
- Create: `/data/multi-bot-saas/migrations/versions/20260730_0001_legacy_baseline.py`
- Create: `/data/multi-bot-saas/app/services/database_lifecycle.py`
- Test: `/data/multi-bot-saas/tests/test_alembic_lifecycle.py`

**Step 1: 写空库升级失败测试**

```python
def test_alembic_upgrades_empty_database_to_head(tmp_path, monkeypatch):
    database_path = tmp_path / "saas.db"
    monkeypatch.setenv("DATABASE_URL", f"sqlite:///{database_path}")
    result = subprocess.run(
        [sys.executable, "-m", "alembic", "upgrade", "head"],
        cwd=PROJECT_ROOT,
        text=True,
        capture_output=True,
    )
    assert result.returncode == 0, result.stderr
    assert read_current_revision(database_path) == read_head_revision()
```

**Step 2: 安装依赖并确认测试失败**

在 `requirements.txt` 增加 `alembic>=1.14.0` 和 `pytest>=8.3.0`，安装后运行：

```bash
.venv/bin/pip install -r requirements.txt
.venv/bin/python -m pytest tests/test_alembic_lifecycle.py -q
```

Expected: 因 Alembic 配置或版本文件不存在而失败。

**Step 3: 创建基线迁移**

Run:

```bash
DATABASE_URL=sqlite:////tmp/multi-bot-saas-baseline.db .venv/bin/alembic revision --autogenerate --rev-id 20260730_0001 -m "legacy baseline"
```

逐表检查生成文件，确认包含当前 `Base.metadata` 中的全部表、外键和唯一约束。迁移文件中的非显然兼容处理需要详细中文注释。

**Step 4: 增加数据库版本检查**

`database_lifecycle.py` 提供：

```python
def assert_database_at_head(connection) -> None:
    """拒绝在数据库版本落后时启动业务 Worker，避免旧结构被新代码写坏。"""
    current_revision = MigrationContext.configure(connection).get_current_revision()
    expected_revision = ScriptDirectory.from_config(build_alembic_config()).get_current_head()
    if current_revision != expected_revision:
        raise RuntimeError(f"database_revision_mismatch:{current_revision}:{expected_revision}")
```

此任务只建立检查能力，暂时保留旧初始化调用，等 SaaS 模型和测试迁移完成后再关闭 `create_all`。

**Step 5: 验证并提交**

Run:

```bash
.venv/bin/python -m pytest tests/test_alembic_lifecycle.py -q
DATABASE_URL=sqlite:////tmp/multi-bot-saas-verify.db .venv/bin/alembic upgrade head
DATABASE_URL=sqlite:////tmp/multi-bot-saas-verify.db .venv/bin/alembic current
git diff --check
```

Commit:

```bash
git add requirements.txt alembic.ini migrations app/services/database_lifecycle.py tests/test_alembic_lifecycle.py
git commit -m "数据库：建立Alembic现有结构基线"
```

### Task 4: 增加平台、租户、套餐和数据库会话模型

**Files:**
- Create: `/data/multi-bot-saas/app/models/mixins.py`
- Create: `/data/multi-bot-saas/app/models/saas.py`
- Modify: `/data/multi-bot-saas/app/models/__init__.py`
- Modify: `/data/multi-bot-saas/app/models/entities.py`
- Modify: `/data/multi-bot-saas/app/services/init_db.py`
- Create: `/data/multi-bot-saas/migrations/versions/20260730_0002_saas_identity.py`
- Test: `/data/multi-bot-saas/tests/test_saas_models.py`

**Step 1: 写模型约束失败测试**

```python
def test_same_username_is_allowed_in_different_tenants(db):
    first = create_tenant(db, code="alpha", name="甲租户")
    second = create_tenant(db, code="beta", name="乙租户")
    db.add_all([
        TenantUser(tenant_id=first.id, username="ops", display_name="甲运营", role="operator", password_hash="x"),
        TenantUser(tenant_id=second.id, username="ops", display_name="乙运营", role="operator", password_hash="x"),
    ])
    db.commit()


def test_same_username_is_rejected_inside_one_tenant(db):
    tenant = create_tenant(db, code="alpha", name="甲租户")
    db.add_all([
        TenantUser(tenant_id=tenant.id, username="ops", display_name="运营一", role="operator", password_hash="x"),
        TenantUser(tenant_id=tenant.id, username="ops", display_name="运营二", role="operator", password_hash="x"),
    ])
    with pytest.raises(IntegrityError):
        db.commit()
```

**Step 2: 运行测试确认失败**

Run: `.venv/bin/python -m pytest tests/test_saas_models.py -q`

Expected: `Tenant`、`TenantUser` 等模型尚不存在。

**Step 3: 实现模型**

先把现有 `TimestampMixin` 原样移动到 `app/models/mixins.py`，`entities.py` 和 `saas.py` 都从该文件导入，避免两个模型模块互相导入。

至少实现以下表和约束：

- `platform_users`、`platform_sessions`；
- `tenants`；
- `saas_plans`、`tenant_subscriptions`；
- `tenant_users`、`tenant_sessions`；
- `platform_audit_logs`。

租户操作继续复用现有 `audit_logs`，由 Task 8 增加 `tenant_id` 并纳入强制租户会话。

关键字段示例：

```python
class TenantUser(Base, TimestampMixin):
    __tablename__ = "tenant_users"
    __table_args__ = (UniqueConstraint("tenant_id", "username", name="uq_tenant_user_username"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    tenant_id: Mapped[int] = mapped_column(ForeignKey("tenants.id"), nullable=False, index=True)
    username: Mapped[str] = mapped_column(String(100), nullable=False)
    display_name: Mapped[str] = mapped_column(String(100), nullable=False)
    password_hash: Mapped[str] = mapped_column(String(255), nullable=False)
    role: Mapped[str] = mapped_column(String(30), nullable=False, default="readonly")
    is_enabled: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    auth_version: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    must_change_password: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
```

每个状态字段定义允许值常量，服务层拒绝未知状态；模型文件对订阅有效期和会话吊销字段写详细中文注释。

**Step 4: 生成并检查迁移**

Run:

```bash
DATABASE_URL=sqlite:////tmp/multi-bot-saas-models.db .venv/bin/alembic upgrade 20260730_0001
DATABASE_URL=sqlite:////tmp/multi-bot-saas-models.db .venv/bin/alembic revision --autogenerate --rev-id 20260730_0002 -m "saas identity"
DATABASE_URL=sqlite:////tmp/multi-bot-saas-models.db .venv/bin/alembic upgrade head
```

**Step 5: 验证并提交**

Run: `.venv/bin/python -m pytest tests/test_saas_models.py tests/test_alembic_lifecycle.py -q`

Commit:

```bash
git add app/models app/services/init_db.py migrations/versions/20260730_0002_saas_identity.py tests/test_saas_models.py
git commit -m "租户：增加平台账号租户套餐和会话模型"
```

### Task 5: 实现安全密码、会话和 CSRF 服务

**Files:**
- Modify: `/data/multi-bot-saas/requirements.txt`
- Create: `/data/multi-bot-saas/app/services/saas_auth.py`
- Create: `/data/multi-bot-saas/app/services/csrf.py`
- Test: `/data/multi-bot-saas/tests/test_saas_auth.py`

**Step 1: 写认证失败测试**

```python
def test_tenant_session_is_revoked_after_role_change(db, active_tenant):
    user = create_tenant_user(db, active_tenant, role="operator")
    raw_token = create_tenant_session(db, user).raw_token
    assert load_tenant_principal(db, raw_token).role == "operator"

    user.role = "readonly"
    user.auth_version += 1
    db.commit()

    assert load_tenant_principal(db, raw_token) is None


def test_csrf_requires_cookie_and_form_token_to_match(db, active_tenant):
    user = create_tenant_user(db, active_tenant)
    session = create_tenant_session(db, user)
    assert verify_csrf_token(db, session.raw_token, session.raw_csrf_token, session.raw_csrf_token)
    assert not verify_csrf_token(db, session.raw_token, session.raw_csrf_token, "伪造值")
```

**Step 2: 安装 Argon2 并确认测试失败**

在 `requirements.txt` 增加 `argon2-cffi>=23.1.0`，然后运行目标测试。

**Step 3: 实现认证原语**

`saas_auth.py` 必须提供：

- `hash_password`、`verify_password`；
- `generate_initial_password`，只返回调用方一次，不记录明文；
- `create_tenant_session`、`load_tenant_principal`、`revoke_tenant_sessions`；
- `create_platform_session`、`load_platform_principal`、`revoke_platform_sessions`；
- `hash_credential`，供会话、激活码和设备密钥统一保存 SHA-256 摘要；
- 固定角色顺序和操作权限判断；
- 会话绝对过期时间和最近使用时间更新。

所有验证函数使用恒定时间比较；失败日志不能包含密码或原始令牌。

**Step 4: 实现双提交 CSRF 防护**

登录成功时同时返回随机会话令牌和随机 CSRF 令牌。会话表保存二者摘要；浏览器分别保存 `HttpOnly` 会话 Cookie 和同站 CSRF Cookie，所有修改表单同时提交隐藏字段。

**Step 5: 验证并提交**

Run: `.venv/bin/python -m pytest tests/test_saas_auth.py -q`

Commit:

```bash
git add requirements.txt app/services/saas_auth.py app/services/csrf.py tests/test_saas_auth.py
git commit -m "认证：实现安全密码数据库会话和CSRF校验"
```

### Task 6: 建立平台与租户登录入口

**Files:**
- Create: `/data/multi-bot-saas/app/web/__init__.py`
- Create: `/data/multi-bot-saas/app/web/platform_auth.py`
- Create: `/data/multi-bot-saas/app/web/tenant_auth.py`
- Create: `/data/multi-bot-saas/app/templates/platform_login.html`
- Modify: `/data/multi-bot-saas/app/templates/login.html`
- Modify: `/data/multi-bot-saas/app/templates/base.html`
- Modify: `/data/multi-bot-saas/app/main.py`
- Test: `/data/multi-bot-saas/tests/test_saas_login.py`

**Step 1: 写双入口失败测试**

```python
def test_tenant_login_requires_tenant_code(client, tenant_user):
    response = client.post("/login", data={"tenant_code": "", "username": "ops", "password": "secret"})
    assert response.status_code == 400


def test_platform_login_does_not_accept_tenant_user(client, tenant_user):
    response = client.post("/platform/login", data={"username": "ops", "password": "secret"})
    assert response.status_code == 401
```

**Step 2: 运行测试确认失败**

Run: `.venv/bin/python -m pytest tests/test_saas_login.py -q`

**Step 3: 实现路由和中间件**

- `/login` 使用“租户编码 + 用户名 + 密码”；
- `/platform/login` 只查询平台账号；
- `/logout` 和 `/platform/logout` 吊销数据库会话后删除 Cookie；
- 平台 Cookie 与租户 Cookie 使用不同名称和路径；
- 登录失败返回统一提示，不区分租户、用户或密码哪一项错误；
- 登录失败次数达到阈值后继续复用现有算术验证码，并增加 IP + 账号维度的限速记录；
- 首次密码账号登录后只能访问修改密码和退出接口；
- 旧 `AdminUser` Cookie 不再被 SaaS 页面接受。

**Step 4: 验证登录、吊销和 Cookie 属性**

Run: `.venv/bin/python -m pytest tests/test_saas_login.py tests/test_saas_auth.py -q`

Expected: 平台和租户会话完全隔离，Cookie 包含预期的 `HttpOnly`、`SameSite` 和生产 `Secure` 属性。

**Step 5: 提交**

```bash
git add app/web app/templates/platform_login.html app/templates/login.html app/templates/base.html app/main.py tests/test_saas_login.py
git commit -m "认证：增加平台与租户独立登录入口"
```

### Task 7: 建立 SQLAlchemy 强制租户会话

**Files:**
- Create: `/data/multi-bot-saas/app/core/tenant_context.py`
- Create: `/data/multi-bot-saas/app/core/tenant_db.py`
- Modify: `/data/multi-bot-saas/app/models/mixins.py`
- Modify: `/data/multi-bot-saas/app/core/db.py`
- Test: `/data/multi-bot-saas/tests/test_tenant_db_scope.py`

**Step 1: 写租户会话失败测试**

```python
def test_scoped_session_cannot_load_other_tenant_row(session_factory, tenant_pair):
    alpha, beta = tenant_pair
    alpha_row = create_probe_unscoped(session_factory, alpha.id, "alpha-row")
    beta_row = create_probe_unscoped(session_factory, beta.id, "beta-row")

    with tenant_scope(alpha.id), tenant_session(session_factory) as db:
        assert db.get(TenantScopedProbe, alpha_row.id) is not None
        assert db.get(TenantScopedProbe, beta_row.id) is None


def test_scoped_session_rejects_cross_tenant_insert(session_factory, tenant_pair):
    alpha, beta = tenant_pair
    with tenant_scope(alpha.id), tenant_session(session_factory) as db:
        db.add(TenantScopedProbe(tenant_id=beta.id, name="错误归属"))
        with pytest.raises(TenantScopeError):
            db.flush()
```

**Step 2: 实现上下文和模型混入**

```python
class TenantOwnedMixin:
    """标记必须受租户会话约束的业务模型。"""

    tenant_id: Mapped[int] = mapped_column(
        ForeignKey("tenants.id"),
        nullable=False,
        index=True,
    )
```

`tenant_context.py` 使用 `ContextVar` 保存当前请求或任务的可信租户编号，并提供必须成对调用的上下文管理器。

**Step 3: 实现 SQLAlchemy 事件**

- `do_orm_execute` 对所有带租户上下文的 ORM SELECT 自动增加 `with_loader_criteria(TenantOwnedMixin, ...)`；
- `before_flush` 自动为新对象补当前租户，并拒绝不一致的显式 `tenant_id`；
- 已存在对象的租户字段禁止修改；
- 平台查询只能通过显式 `platform_session()` 设置旁路标记；
- 旁路函数只允许平台服务和迁移脚本导入，普通路由不得调用。

**Step 4: 使用测试专用模型验证机制**

在 `test_tenant_db_scope.py` 内定义只用于测试的 `TenantScopedProbe`，不要提前修改任何正式业务模型。Task 8 再一次性把全部业务模型接入混入并生成匹配的数据库迁移，保证每个提交的正式模型与迁移版本一致。

**Step 5: 验证并提交**

Run: `.venv/bin/python -m pytest tests/test_tenant_db_scope.py -q`

Commit:

```bash
git add app/core/tenant_context.py app/core/tenant_db.py app/core/db.py app/models/mixins.py tests/test_tenant_db_scope.py
git commit -m "租户：建立SQLAlchemy会话级强制隔离"
```

### Task 8: 为全部业务模型增加租户归属

**Files:**
- Modify: `/data/multi-bot-saas/app/models/entities.py`
- Modify: `/data/multi-bot-saas/app/services/seed.py`
- Modify: `/data/multi-bot-saas/app/services/init_db.py`
- Create: `/data/multi-bot-saas/migrations/versions/20260730_0003_tenantize_business_tables.py`
- Test: `/data/multi-bot-saas/tests/test_tenant_model_coverage.py`
- Modify: `/data/multi-bot-saas/tests/conftest.py`

**Step 1: 写模型覆盖失败测试**

```python
TENANT_OWNED_MODELS = {
    Bot, Channel, BotPlugin,
    Conversation, Chatroom, ChatroomMember, WorkWechatContact,
    WorkWechatContactRelation, CustomerOperationRequest, OpenApiApp,
    OpenApiAppChannel, GroupPluginScope, RawWebhookEvent, Message,
    MessageAttachment, TaskJob, WeatherSubscription, SignInRecord,
    SignInHistory, PointsAccount, PointsTransaction, PointsPrisonRecord,
    ErrorLog, PluginExecutionLog, MessageActivityHourly,
    PluginActivityHourly, HealthCheckRecord, AlertRecord, AuditLog,
}


def test_all_business_models_are_tenant_owned():
    assert all(issubclass(model, TenantOwnedMixin) for model in TENANT_OWNED_MODELS)
    assert not issubclass(Plugin, TenantOwnedMixin)
```

**Step 2: 为所有业务模型增加混入**

仅 `Plugin` 目录定义、平台表和套餐表保持全局。旧 `AdminUser` 暂时保留为迁移兼容表，但不再参与新登录。

修改租户内唯一约束，例如：

- `Bot`：`(tenant_id, name)`；机器人不再保存 slug、分组、模板、标签、环境和自定义命令前缀；
- `CustomerOperationRequest`：`(tenant_id, channel_id, request_id)`；
- `OpenApiApp`：`(tenant_id, app_key)`；
- 入站事件和插件业务表按现有幂等字段增加 `tenant_id`。

**Step 3: 编写可回填迁移**

迁移顺序：

1. 检测旧业务表是否已有数据；
2. 仅在有旧数据时创建 `legacy-self` 自营租户和默认订阅；
3. 为业务表增加可空 `tenant_id`；
4. 按外键关系优先从根资源传播租户，剩余旧记录归入自营租户；
5. 重建全局唯一约束为租户内唯一；
6. 将 `tenant_id` 改为不可空并建立索引、外键；
7. 对每张表记录明确中文迁移注释。

**Step 4: 验证空库和带旧数据数据库升级**

Run:

```bash
.venv/bin/python -m pytest tests/test_tenant_model_coverage.py tests/test_tenant_db_scope.py tests/test_alembic_lifecycle.py -q
DATABASE_URL=sqlite:////tmp/multi-bot-saas-empty.db .venv/bin/alembic upgrade head
```

另由测试构造一份 `20260730_0001` 的旧结构样本数据，再升级到 `head`，断言所有业务表 `tenant_id` 非空且关系一致。

**Step 5: 提交**

```bash
git add app/models/entities.py app/services/seed.py app/services/init_db.py migrations/versions/20260730_0003_tenantize_business_tables.py tests/conftest.py tests/test_tenant_model_coverage.py
git commit -m "租户：为全部业务模型增加租户归属"
```

### Task 9: 让 Web 页面和业务服务使用可信租户上下文

**Files:**
- Modify: `/data/multi-bot-saas/app/main.py`
- Modify: `/data/multi-bot-saas/app/services/crud.py`
- Modify: `/data/multi-bot-saas/app/services/ops.py`
- Modify: `/data/multi-bot-saas/app/services/runtime.py`
- Modify: `/data/multi-bot-saas/app/services/analytics.py`
- Modify: `/data/multi-bot-saas/app/services/chatroom_registry.py`
- Modify: `/data/multi-bot-saas/app/services/workwechat_contacts.py`
- Modify: `/data/multi-bot-saas/app/services/workwechat_chatrooms.py`
- Modify: `/data/multi-bot-saas/app/services/open_api_apps.py`
- Test: `/data/multi-bot-saas/tests/test_tenant_web_isolation.py`

**Step 1: 写 URL 主键替换失败测试**

```python
def test_tenant_cannot_open_other_tenant_channel(client, tenant_pair, login_as):
    alpha, beta = tenant_pair
    beta_channel = create_workwechat_channel(beta)
    login_as(alpha, role="tenant_admin")

    response = client.get(f"/channels/{beta_channel.id}")

    assert response.status_code == 404
    assert beta_channel.name not in response.text
```

同时为列表、统计、搜索、编辑、删除和批量操作各写至少一个双租户测试。

**Step 2: 在认证中间件建立租户上下文**

- 验证会话后把 `TenantPrincipal` 放入 `request.state`；
- 使用上下文管理器包住 `call_next`；
- `get_session()` 返回受当前上下文限制的租户会话；
- 未登录、平台路由和机器接口不设置租户上下文；
- 所有写路由调用 `require_tenant_mutation_allowed`，统一处理到期和停用状态。

**Step 3: 审计直接会话创建**

Run:

```bash
rg -n "SessionLocal\(" app/main.py app/services app/api
rg -n "platform_session\(" app
```

把普通业务中的 `SessionLocal()` 改为租户会话或显式接收 `db`；平台旁路只允许出现在白名单模块。

**Step 4: 验证核心页面和服务**

Run:

```bash
.venv/bin/python -m pytest tests/test_tenant_web_isolation.py tests/test_multi_bot_admin.py -q
```

Expected: 原有功能在默认测试租户中通过，跨租户读取和修改均失败。

**Step 5: 提交**

```bash
git add app/main.py app/services tests/test_tenant_web_isolation.py
git commit -m "租户：完成核心页面和业务服务隔离"
```

### Task 10: 租户化 Webhook、开放 API、文件和后台任务

**Files:**
- Modify: `/data/multi-bot-saas/app/api/channel_inbound.py`
- Modify: `/data/multi-bot-saas/app/api/channel_outbound.py`
- Modify: `/data/multi-bot-saas/app/api/customer_operations.py`
- Modify: `/data/multi-bot-saas/app/api/open_api.py`
- Modify: `/data/multi-bot-saas/app/api/open_api_admin.py`
- Modify: `/data/multi-bot-saas/app/api/tool_api.py`
- Modify: `/data/multi-bot-saas/app/services/channel_events.py`
- Modify: `/data/multi-bot-saas/app/services/task_queue.py`
- Modify: `/data/multi-bot-saas/app/services/async_workers.py`
- Modify: `/data/multi-bot-saas/app/services/inbound_media.py`
- Modify: `/data/multi-bot-saas/app/services/outbound_batch.py`
- Modify: `/data/multi-bot-saas/app/services/open_api_auth.py`
- Modify: `/data/multi-bot-saas/app/services/open_webhooks.py`
- Test: `/data/multi-bot-saas/tests/test_tenant_machine_interfaces.py`

**Step 1: 写机器接口越权失败测试**

覆盖以下伪造场景：

- 甲租户开放应用请求乙租户渠道；
- 甲租户渠道密钥提交乙租户会话；
- 任务 payload 混入乙租户 `channel_id`；
- 文件分享 ID 在不同租户间猜测；
- Worker 使用任务租户加载不一致资源。

**Step 2: 统一机器身份转租户上下文**

所有入口遵循两阶段会话：

```python
with platform_session() as auth_db:
    credential = authenticate_machine_credential(auth_db, request)

with tenant_scope(credential.tenant_id), tenant_session() as db:
    return handle_tenant_request(db, credential, payload)
```

认证阶段只能返回最小身份信息，业务阶段必须重新在租户会话中加载资源。

**Step 3: 改造 Worker**

- 全局调度器使用平台会话只领取待执行任务 ID、租户 ID；
- 每个任务打开独立租户上下文和租户会话；
- 校验任务、渠道、机器人、会话和插件绑定的租户一致；
- 未通过校验时终止任务并记录脱敏错误，绝不调用外部 Provider；
- 聚合统计按租户分组写入。

**Step 4: 文件按租户分区**

存储路径改为 `data/tenants/<tenant_id>/...`，下载 URL 中的分享 ID 不能替代数据库租户校验。迁移旧文件时只记录路径映射，不在请求期间临时猜测旧目录。

**Step 5: 验证并提交**

Run:

```bash
.venv/bin/python -m pytest tests/test_tenant_machine_interfaces.py tests/test_open_api.py tests/test_open_api_apps.py tests/test_open_webhooks.py tests/test_outbound_files.py -q
```

Commit:

```bash
git add app/api app/services tests/test_tenant_machine_interfaces.py
git commit -m "租户：隔离机器接口文件链路和后台任务"
```

### Task 11: 建立平台租户和套餐管理后台

**Files:**
- Create: `/data/multi-bot-saas/app/services/platform_admin.py`
- Create: `/data/multi-bot-saas/app/web/platform_admin.py`
- Create: `/data/multi-bot-saas/app/templates/platform_base.html`
- Create: `/data/multi-bot-saas/app/templates/platform_dashboard.html`
- Create: `/data/multi-bot-saas/app/templates/platform_tenants.html`
- Create: `/data/multi-bot-saas/app/templates/platform_tenant_detail.html`
- Create: `/data/multi-bot-saas/app/templates/platform_plans.html`
- Modify: `/data/multi-bot-saas/app/static/css/style.css`
- Modify: `/data/multi-bot-saas/app/main.py`
- Test: `/data/multi-bot-saas/tests/test_platform_admin.py`

**Step 1: 写平台管理失败测试**

```python
def test_platform_admin_can_create_tenant_with_subscription(platform_client, plan):
    response = platform_client.post(
        "/platform/tenants",
        data={
            "code": "acme",
            "name": "示例企业",
            "plan_id": str(plan.id),
            "expires_on": "2027-07-30",
            "admin_username": "admin",
            "admin_display_name": "企业管理员",
        },
    )
    assert response.status_code == 303
    assert load_tenant("acme").subscription.plan_id == plan.id
```

**Step 2: 实现平台服务事务**

创建租户、订阅和首个管理员必须位于一个事务。初始密码只在成功响应页面显示一次，审计日志只记录“已创建初始凭证”，不得记录密码。

**Step 3: 实现页面**

平台页面采用独立导航，不复用租户业务导航。至少完成租户列表、创建、详情、启停、续期、套餐管理、节点用量和管理员重置。

**Step 4: 验证权限和事务回滚**

Run: `.venv/bin/python -m pytest tests/test_platform_admin.py -q`

覆盖普通租户用户访问 `/platform/*`、重复租户编码、禁用套餐、创建管理员失败时的完整回滚。

**Step 5: 提交**

```bash
git add app/services/platform_admin.py app/web/platform_admin.py app/templates/platform_*.html app/static/css/style.css app/main.py tests/test_platform_admin.py
git commit -m "平台：增加租户套餐和有效期管理后台"
```

### Task 12: 建立租户员工管理和到期只读策略

**Files:**
- Create: `/data/multi-bot-saas/app/services/tenant_admin.py`
- Create: `/data/multi-bot-saas/app/web/tenant_admin.py`
- Create: `/data/multi-bot-saas/app/templates/tenant_users.html`
- Create: `/data/multi-bot-saas/app/templates/tenant_subscription.html`
- Modify: `/data/multi-bot-saas/app/templates/base.html`
- Modify: `/data/multi-bot-saas/app/main.py`
- Test: `/data/multi-bot-saas/tests/test_tenant_admin.py`

**Step 1: 写固定角色和即时吊销失败测试**

覆盖：租户管理员创建员工、运营不能创建员工、只读不能修改、角色变化后旧会话失效、不能停用最后一个租户管理员、不能操作其他租户员工。

**Step 2: 实现员工服务**

固定允许角色为 `tenant_admin`、`operator`、`readonly`。所有角色、密码和启停变化都增加 `auth_version` 并吊销现有会话。

**Step 3: 实现订阅状态策略**

提供统一函数：

```python
def require_tenant_mutation_allowed(principal: TenantPrincipal, now: datetime) -> None:
    """租户停用或到期时拒绝写操作，但保留历史数据只读访问。"""
    if principal.tenant_status != "active":
        raise TenantPolicyError("tenant_disabled")
    if principal.subscription_expires_at and principal.subscription_expires_at <= now:
        raise TenantPolicyError("tenant_expired")
```

所有租户修改路由复用该函数；禁止每个页面自行判断日期。

**Step 4: 验证页面和策略**

Run: `.venv/bin/python -m pytest tests/test_tenant_admin.py tests/test_tenant_web_isolation.py -q`

**Step 5: 提交**

```bash
git add app/services/tenant_admin.py app/web/tenant_admin.py app/templates/tenant_*.html app/templates/base.html app/main.py tests/test_tenant_admin.py
git commit -m "租户：增加员工管理和到期只读策略"
```

### Task 13: 增加 Provider 节点、激活码和设备凭证模型

**Files:**
- Modify: `/data/multi-bot-saas/app/models/saas.py`
- Modify: `/data/multi-bot-saas/app/models/entities.py`
- Create: `/data/multi-bot-saas/app/services/provider_nodes.py`
- Create: `/data/multi-bot-saas/migrations/versions/20260730_0004_provider_nodes.py`
- Test: `/data/multi-bot-saas/tests/test_provider_nodes.py`

**Step 1: 写节点额度和激活码失败测试**

```python
def test_provider_node_creation_reserves_quota(db, tenant_with_one_node_plan):
    create_provider_node(db, tenant_with_one_node_plan.id, name="客服电脑A")
    with pytest.raises(ProviderNodeLimitReached):
        create_provider_node(db, tenant_with_one_node_plan.id, name="客服电脑B")


def test_activation_code_can_only_be_consumed_once(db, pending_node):
    raw_code = issue_activation_code(db, pending_node.id).raw_code
    first = activate_provider_node(db, raw_code, fingerprint="device-a", client_version="1.0.0")
    assert first.raw_device_token
    with pytest.raises(ActivationCodeInvalid):
        activate_provider_node(db, raw_code, fingerprint="device-a", client_version="1.0.0")
```

**Step 2: 实现模型**

- `provider_nodes`；
- `provider_node_accounts`；
- `provider_activation_codes`；
- `provider_credentials`；
- `provider_heartbeats`；
- `channels.provider_node_id`、`channels.provider_account_id`；
- `task_jobs.provider_node_id`、`provider_account_id`、`lease_token_hash`、`lease_expires_at`、`claimed_at`。

**Step 3: 实现并发安全服务**

- 创建节点时锁定租户订阅或租户额度行；
- 待激活节点立即占用额度；
- 激活码使用 `secrets.token_urlsafe(32)`，默认 30 分钟有效，只保存摘要；
- 激活使用条件更新或 `SELECT ... FOR UPDATE`；
- 设备密钥只返回一次，数据库保存摘要；
- 降低套餐额度时不禁用已有节点，但阻止新增；
- 停用节点立即吊销全部有效设备凭证。

**Step 4: 生成迁移并验证 MariaDB 兼容性**

Run:

```bash
DATABASE_URL=sqlite:////tmp/multi-bot-saas-provider.db .venv/bin/alembic upgrade head
.venv/bin/python -m pytest tests/test_provider_nodes.py -q
```

**Step 5: 提交**

```bash
git add app/models app/services/provider_nodes.py migrations/versions/20260730_0004_provider_nodes.py tests/test_provider_nodes.py
git commit -m "Provider：增加节点额度激活码和设备凭证"
```

### Task 14: 改造云端 Provider API 为节点级协议

**Files:**
- Modify: `/data/multi-bot-saas/app/api/workwechat_provider_cloud.py`
- Modify: `/data/multi-bot-saas/app/services/workwechat_cloud_poll.py`
- Modify: `/data/multi-bot-saas/app/services/task_queue.py`
- Modify: `/data/multi-bot-saas/app/services/channel_events.py`
- Test: `/data/multi-bot-saas/tests/test_provider_node_api.py`

**Step 1: 写节点级协议失败测试**

覆盖：

- 激活请求不接受客户端提交的 `tenant_id` 或 `channel_id`；
- 一个设备密钥能拉取节点下多个企微渠道的任务；
- 任务必须携带已绑定的节点账号标识，不能由客户端临时改选其他账号；
- 不能拉取同租户其他节点任务；
- 不能回报其他节点或其他租户任务；
- 到期租户只允许心跳，不返回新任务；
- 重复完成回报幂等；
- 过期租约不能完成任务。

**Step 2: 定义端点**

```text
POST /api/provider/activate
POST /api/provider/heartbeat
POST /api/provider/poll
POST /api/provider/jobs/{job_id}/complete
POST /api/provider/jobs/{job_id}/fail
POST /api/provider/inbound
POST /api/provider/inbound-upload
```

保留旧 `/api/workwechat/provider/*` 的短期兼容响应，但新租户不再签发每渠道 `client_token`。

**Step 3: 实现任务租约**

领取任务时原子设置 `provider_node_id`、`status=running`、`lease_token_hash`、`lease_expires_at`。响应只返回原始租约令牌一次。完成和失败接口必须同时验证设备节点、任务节点和租约令牌。

**Step 4: 实现心跳和多渠道状态**

心跳载荷包含客户端版本、Hook 状态、企微进程状态和账号摘要列表。每个账号摘要带节点内稳定的 `account_key`；云端据此更新 `provider_node_accounts`，但不能仅凭客户端上报自动改变渠道租户归属或渠道绑定。

**Step 5: 验证并提交**

Run:

```bash
.venv/bin/python -m pytest tests/test_provider_node_api.py tests/test_workwechat_cloud_poll.py tests/test_workwechat_provider.py -q
```

Commit:

```bash
git add app/api/workwechat_provider_cloud.py app/services/workwechat_cloud_poll.py app/services/task_queue.py app/services/channel_events.py tests/test_provider_node_api.py
git commit -m "Provider：升级节点级心跳长轮询和任务租约"
```

### Task 15: 改造 Windows Provider 支持节点激活和多账号

**Files:**
- Modify: `/data/multi-bot-saas/vendors/workwechat_provider/config.example.json`
- Modify: `/data/multi-bot-saas/vendors/workwechat_provider/cloud_jobs.py`
- Modify: `/data/multi-bot-saas/vendors/workwechat_provider/launcher_app.py`
- Modify: `/data/multi-bot-saas/vendors/workwechat_provider/launcher_controller.py`
- Modify: `/data/multi-bot-saas/vendors/workwechat_provider/launcher_state.py`
- Modify: `/data/multi-bot-saas/vendors/workwechat_provider/app.py`
- Modify: `/data/multi-bot-saas/vendors/workwechat_provider/README.md`
- Test: `/data/multi-bot-saas/tests/test_workwechat_provider_node_client.py`

**Step 1: 写客户端状态迁移失败测试**

覆盖：旧 `authorization_code/client_token/cloud_channel_id` 配置迁移、激活成功只保存设备密钥、日志脱敏、多渠道任务分派、本地重复任务防护、网络超时退避。

**Step 2: 修改配置格式**

推荐配置：

```json
{
  "cloud_base_url": "https://saas.example.com",
  "provider_node_name": "客服电脑A",
  "activation_code": "",
  "device_token": "",
  "device_id": "",
  "heartbeat_interval_seconds": 20,
  "poll_timeout_seconds": 25
}
```

真实 `device_token` 不写入示例文件；本地配置保存时限制文件权限，并在 UI 中只显示是否已激活。

**Step 3: 实现客户端协议**

- 未激活时用一次性激活码调用 `/api/provider/activate`；
- 激活成功后原子写入设备密钥并清空激活码；
- 心跳与长轮询使用设备密钥；
- 根据任务中由云端签发的 `provider_account_key` 选择对应的本地企微会话；
- 无法唯一匹配账号时拒绝发送并回报明确错误；
- 本地锁使用 `job_id + payload_hash`，重复任务只补回报不重复发送；
- 对 401 停止轮询并提示重新激活，对 429/5xx 使用带抖动退避。

**Step 4: 验证客户端单元测试**

Run:

```bash
.venv/bin/python -m pytest tests/test_workwechat_provider_node_client.py tests/test_workwechat_provider_launcher.py tests/test_workwechat_cloud_poll.py -q
```

**Step 5: 提交**

```bash
git add vendors/workwechat_provider tests/test_workwechat_provider_node_client.py
git commit -m "客户端：支持Provider节点激活和多企微账号"
```

### Task 16: 增加租户 Provider 管理页面和企微渠道绑定

**Files:**
- Create: `/data/multi-bot-saas/app/web/provider_nodes.py`
- Create: `/data/multi-bot-saas/app/templates/provider_nodes.html`
- Create: `/data/multi-bot-saas/app/templates/provider_node_detail.html`
- Modify: `/data/multi-bot-saas/app/templates/channels.html`
- Modify: `/data/multi-bot-saas/app/templates/channel_detail.html`
- Modify: `/data/multi-bot-saas/app/templates/base.html`
- Modify: `/data/multi-bot-saas/app/main.py`
- Test: `/data/multi-bot-saas/tests/test_provider_node_pages.py`

**Step 1: 写页面权限和绑定失败测试**

覆盖：管理员创建节点并只看一次激活码、运营只查看节点状态、只读不能生成激活码、跨租户节点不能绑定、停用节点不能新绑定、同节点可绑定多个企微渠道、渠道必须绑定该节点已发现的账号。

**Step 2: 实现节点页面**

列表展示节点名、状态、客户端版本、最后心跳、已绑定渠道数和额度用量。详情页提供生成激活码、吊销、停用、解绑和重新激活操作；敏感操作使用确认弹窗和 CSRF。

**Step 3: 改造渠道页面**

新建和编辑企业微信渠道必须选择本租户可用节点及其已发现账号。服务端同时校验 `provider_node_id` 和 `provider_account_id` 的租户、节点及在线归属，不能只依赖下拉框过滤。

**Step 4: 验证响应式页面**

Run:

```bash
.venv/bin/python -m pytest tests/test_provider_node_pages.py -q
```

使用 Playwright 检查桌面和移动视口，确保额度提示、状态表格、激活码弹窗和长名称不重叠。

**Step 5: 提交**

```bash
git add app/web/provider_nodes.py app/templates/provider_*.html app/templates/channels.html app/templates/channel_detail.html app/templates/base.html app/main.py tests/test_provider_node_pages.py
git commit -m "页面：增加Provider节点管理和企微渠道绑定"
```

### Task 17: 只开放企业微信渠道

**Files:**
- Modify: `/data/multi-bot-saas/app/main.py`
- Modify: `/data/multi-bot-saas/app/schemas/forms.py`
- Modify: `/data/multi-bot-saas/app/templates/channels.html`
- Modify: `/data/multi-bot-saas/app/templates/channel_detail.html`
- Modify: `/data/multi-bot-saas/app/templates/base.html`
- Test: `/data/multi-bot-saas/tests/test_workwechat_only_scope.py`

**Step 1: 写服务端拒绝失败测试**

```python
@pytest.mark.parametrize("channel_type", ["wechat_ipad", "wechat_winauto", "telegram", "openclaw", "simulator"])
def test_tenant_cannot_create_non_workwechat_channel(tenant_admin_client, bot, channel_type):
    response = tenant_admin_client.post(
        "/channels",
        data={"bot_id": bot.id, "name": "非法渠道", "channel_type": channel_type},
    )
    assert response.status_code == 400
    assert count_channels(channel_type) == 0
```

**Step 2: 实现统一允许列表**

建立 `SAAS_ENABLED_CHANNEL_TYPES = ("workwechat",)`，表单选项、创建服务、批量导入和开放 API 管理都复用该常量。不要只从模板移除选项。

**Step 3: 隐藏无关入口**

租户导航和渠道详情不再展示微信 iPad、微信 PC、Telegram、OpenClaw 和模拟器功能。旧适配器源码及其独立测试保留。

**Step 4: 验证并提交**

Run: `.venv/bin/python -m pytest tests/test_workwechat_only_scope.py tests/test_multi_bot_admin.py -q`

Commit:

```bash
git add app/main.py app/schemas/forms.py app/templates/channels.html app/templates/channel_detail.html app/templates/base.html tests/test_workwechat_only_scope.py
git commit -m "渠道：SaaS首期仅开放企业微信接入"
```

### Task 18: 移除生产启动补表并完成数据库生命周期

**Files:**
- Modify: `/data/multi-bot-saas/app/services/init_db.py`
- Modify: `/data/multi-bot-saas/app/services/installer.py`
- Modify: `/data/multi-bot-saas/app/services/runtime.py`
- Modify: `/data/multi-bot-saas/app/main.py`
- Modify: `/data/multi-bot-saas/scripts/deploy_restart_mba.sh`
- Test: `/data/multi-bot-saas/tests/test_database_lifecycle.py`

**Step 1: 写生产环境禁止自动改表测试**

测试生产启动不会调用 `Base.metadata.create_all`、`ensure_runtime_schema` 或执行 `ALTER TABLE`，数据库版本落后时健康检查返回未就绪并阻止 Worker 启动。

**Step 2: 修改启动流程**

- 开发和测试可以通过测试辅助函数创建临时结构；
- 生产启动只执行 Alembic 版本检查和安全种子检查；
- 首次部署由 `alembic upgrade head` 建表；
- 原安装向导不再负责切换数据库或动态建表，SaaS 部署通过环境变量配置数据库；
- `ensure_runtime_schema` 保留为旧项目历史兼容说明，但不在 SaaS 启动链路调用，或在确认无调用后删除。

**Step 3: 修改部署脚本顺序**

部署顺序固定为：安装依赖、数据库备份检查、`alembic upgrade head`、重启应用、检查 `/healthz` 和 `/readyz`。迁移失败时不得重启旧进程。

**Step 4: 验证并提交**

Run:

```bash
.venv/bin/python -m pytest tests/test_database_lifecycle.py tests/test_alembic_lifecycle.py -q
rg -n "create_all|ensure_runtime_schema|ALTER TABLE" app
```

Commit:

```bash
git add app/services/init_db.py app/services/installer.py app/services/runtime.py app/main.py scripts/deploy_restart_mba.sh tests/test_database_lifecycle.py
git commit -m "数据库：生产启动全面切换到Alembic迁移"
```

### Task 19: 增加旧系统数据导入工具

**Files:**
- Create: `/data/multi-bot-saas/scripts/import_legacy_tenant.py`
- Create: `/data/multi-bot-saas/app/services/legacy_import.py`
- Create: `/data/multi-bot-saas/docs/legacy-import.md`
- Test: `/data/multi-bot-saas/tests/test_legacy_import.py`

**Step 1: 写校验模式失败测试**

构造旧 SQLite 数据库和新 SaaS 数据库，断言 `--dry-run` 不写入目标库，正式导入后所有记录属于指定租户、外键映射正确、重复执行被拒绝或幂等跳过。

**Step 2: 实现显式导入服务**

- 来源数据库始终只读；
- 目标租户必须显式给出，不允许默认猜测；
- 按根资源到子资源建立旧 ID 到新 ID 映射；
- 密钥字段只迁移密文或要求轮换，不输出明文；
- 每张表输出读取数、写入数、跳过数和错误数；
- 支持 `--dry-run`、`--tenant-code`、`--source-url` 和 `--report-path`；
- 导入批次写入审计表，防止同一来源重复执行。

**Step 3: 增加关系和数量校验**

导入完成后验证机器人、渠道、联系人、群聊、会话、消息、任务和插件绑定的父子数量及租户一致性。任一关键表不一致则返回非零退出码。

**Step 4: 验证并提交**

Run: `.venv/bin/python -m pytest tests/test_legacy_import.py -q`

Commit:

```bash
git add scripts/import_legacy_tenant.py app/services/legacy_import.py docs/legacy-import.md tests/test_legacy_import.py
git commit -m "迁移：增加旧系统数据导入和校验工具"
```

### Task 20: 完成跨租户安全矩阵和全量回归

**Files:**
- Create: `/data/multi-bot-saas/tests/test_tenant_security_matrix.py`
- Create: `/data/multi-bot-saas/tests/test_saas_end_to_end.py`
- Modify: `/data/multi-bot-saas/README.md`
- Modify: `/data/multi-bot-saas/docs/deployment.md`

**Step 1: 建立攻击矩阵测试**

至少覆盖：

- URL 主键替换；
- 表单隐藏字段篡改；
- 批量 ID 混入其他租户；
- 搜索、统计、导出泄露；
- 开放 API 应用和渠道错配；
- Provider 节点、渠道、任务和租约错配；
- Webhook 与入站文件伪造；
- 文件分享 ID 猜测；
- 缓存键和运行状态串租户；
- 平台 Cookie 用于租户接口、租户 Cookie 用于平台接口。

**Step 2: 建立双租户端到端测试**

端到端流程创建甲乙两个租户、同名员工、同名机器人、同外部 ID 联系人和群聊；分别激活节点、绑定企微渠道、发送任务和上传入站事件，最终断言双方列表、统计、日志和文件完全隔离。

**Step 3: 运行全量测试**

Run:

```bash
.venv/bin/python -m pytest -q
git diff --check
```

Expected: 全部测试通过，无跳过的租户隔离关键测试。

**Step 4: 运行静态审计**

Run:

```bash
rg -n "SessionLocal\(|platform_session\(|tenant_id\s*=|db\.get\(" app
rg -n "client_token|authorization_code|device_token|password" app vendors scripts
```

逐项审计旁路、直接主键查询和敏感日志。发现问题时先增加失败测试，再修复。

**Step 5: 提交**

```bash
git add tests/test_tenant_security_matrix.py tests/test_saas_end_to_end.py README.md docs/deployment.md
git commit -m "测试：完成双租户安全矩阵和全量验收"
```

### Task 21: 验证 Docker、Windows Provider 和本地试运行

**Files:**
- Modify: `/data/multi-bot-saas/Dockerfile`
- Modify: `/data/multi-bot-saas/docker-compose.yml`
- Create: `/data/multi-bot-saas/scripts/saas_smoke_test.sh`
- Modify: `/data/multi-bot-saas/docs/deployment.md`

**Step 1: 构建镜像**

Run:

```bash
docker compose build
docker compose up -d mariadb
docker compose run --rm app alembic upgrade head
docker compose up -d app
```

Expected: 使用独立容器、卷、数据库和端口，不影响现有 `multi-bot-admin`。

**Step 2: 运行健康和冒烟检查**

`saas_smoke_test.sh` 必须自动验证：

- `/healthz` 和 `/readyz`；
- 平台登录页和租户登录页；
- 创建两个测试租户；
- 节点额度和一次性激活；
- 两节点心跳和任务隔离；
- 到期租户不能领取新任务；
- 日志不包含测试设备密钥原文。

Run: `./scripts/saas_smoke_test.sh http://127.0.0.1:8280`

**Step 3: Windows Provider 联调**

在受控 Windows 测试机使用测试租户激活 Provider，完成心跳、两个企微渠道绑定、测试消息任务和结果回报。只使用测试账号，禁止向真实客户发送消息。

**Step 4: 验证重启恢复**

在存在待领取任务、运行中任务和已完成任务三种状态时分别重启云端和 Provider，确认租约恢复且不重复发送。

**Step 5: 提交**

```bash
git add Dockerfile docker-compose.yml scripts/saas_smoke_test.sh docs/deployment.md
git commit -m "部署：完成容器化和Provider联调验收"
```

### Task 22: 创建远程仓库并部署到服务器

**Files:**
- Verify: `/data/multi-bot-saas/.git/config`
- Remote: `192.168.2.142` 上的新项目目录和部署配置

**Step 1: 部署前只读盘点**

执行本任务时先加载 `@ssh` 技能。通过 SSH 只读检查远程 Docker、磁盘、现有端口、反向代理、Git 目录和当前 `multi-bot-admin` 服务，确认 `8280`、容器名、数据库名和卷不会冲突。

**Step 2: 配置独立 Git 远程**

预期远程地址：

```text
http://192.168.2.32:3000/openclaw/multi-bot-saas
```

如果仓库尚不存在，先由具备权限的 Forgejo 账号创建空仓库；不得复用或覆盖 `multi-bot-admin` 远程。

Run:

```bash
git remote add origin http://192.168.2.32:3000/openclaw/multi-bot-saas
git push -u origin main
```

**Step 3: 远程备份和首次部署**

首次部署使用 `git clone`，后续严格使用 `git pull --ff-only`。部署前备份目标数据库和环境配置；运行 Alembic 升级成功后再重启新服务。密码通过远程受限环境文件配置，不能提交到 Git。

**Step 4: 远程验收**

验证：

- 新 PID/容器和旧项目同时存在且互不影响；
- `/healthz`、`/readyz`、平台登录页和租户登录页正常；
- MariaDB revision 为 Alembic head；
- 两个测试租户数据隔离；
- Provider 从客户网络可以通过 HTTPS 激活、心跳和长轮询；
- 远程日志没有密码、激活码或设备密钥原文。

**Step 5: 记录发布提交**

如部署过程中只修改文档或样例配置，单独提交：

```bash
git add docs/deployment.md .env.docker.example
git commit -m "发布：记录SaaS远程部署和验收结果"
git push
```

## 最终验收命令

在 `/data/multi-bot-saas` 执行：

```bash
.venv/bin/python -m pytest -q
.venv/bin/alembic heads
docker compose config
docker compose ps
curl --fail http://127.0.0.1:8280/healthz
curl --fail http://127.0.0.1:8280/readyz
git status --short
git log --oneline --decorate -20
```

Expected:

- 全量测试通过；
- Alembic 只有一个 head；
- Compose 配置有效且服务健康；
- 工作区干净；
- Git 历史由清晰的中文阶段提交组成；
- 现有 `/data/multi-bot-admin` 工作区及线上服务未被覆盖。
