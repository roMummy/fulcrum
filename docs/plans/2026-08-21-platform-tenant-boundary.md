# Platform And Tenant Boundary Implementation Plan

> **For Claude:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task.

**Goal:** 把平台员工与企业用户彻底分开，企业用户统一使用手机号登录，并将系统运维、全局日志从租户后台迁移到平台后台。

**Architecture:** 继续使用现有 `PlatformUser`/`PlatformSession` 和 `TenantUser`/`TenantSession` 双会话架构。企业登录通过全局唯一手机号恢复可信 `tenant_id`；平台运维使用新的 `/platform/operations` 和 `/platform/logs` 路由及平台 CSRF。旧租户内部工具路由由中间件统一返回 `404`，但不修改机器签名开放 API。

**Tech Stack:** FastAPI, Jinja2, SQLAlchemy 2, Pydantic, pytest, MariaDB/SQLite focused tests.

---

### Task 1: 统一企业手机号登录

**Files:**
- Modify: `app/services/saas_auth.py`
- Modify: `app/web/public_site.py`
- Modify: `app/web/tenant_auth.py`
- Modify: `app/web/auth_common.py`
- Modify: `app/templates/owner_login.html`
- Modify: `app/templates/public_base.html`
- Test: `tests/test_public_site_auth.py`

**Step 1: 写失败测试**

- 为非所有者 `TenantUser` 填入全局唯一手机号。
- 断言企业所有者和企业员工都能通过 `POST /login` 登录。
- 断言 `GET/POST /employee/login` 均返回 `404`。

**Step 2: 运行失败测试**

Run: `.venv/bin/python -m pytest tests/test_public_site_auth.py -q`

Expected: 员工手机号登录和旧路由删除断言失败。

**Step 3: 实现最小登录改动**

- 在 `saas_auth.py` 增加按手机号验证任意启用企业用户的函数，不再限制 `is_owner=True`。
- `/login` 统一调用新函数，页面文案改为“企业账号登录”。
- 删除 `/employee/login` GET/POST、独立员工登录模板和产品页面入口。
- 删除按租户编码与用户名认证的旧函数及对应测试，保持单一手机号登录链。

**Step 4: 运行定向测试**

Run: `.venv/bin/python -m pytest tests/test_public_site_auth.py -q`

Expected: PASS.

### Task 2: 强制企业员工手机号唯一

**Files:**
- Modify: `app/services/tenant_admin.py`
- Modify: `app/web/tenant_admin.py`
- Modify: `app/templates/tenant_users.html`
- Test: `tests/test_tenant_admin.py`
- Test: `tests/test_tenant_web_isolation.py`

**Step 1: 写失败测试**

- 断言创建员工时必须提供有效手机号。
- 断言另一租户已占用的手机号不能重复使用。
- 断言更新手机号或角色后旧会话立即失效。

**Step 2: 运行失败测试**

Run: `.venv/bin/python -m pytest tests/test_tenant_admin.py tests/test_tenant_web_isolation.py -q`

Expected: 新的 `mobile` 参数和唯一性规则断言失败。

**Step 3: 实现员工手机号规则**

- 复用 `registration.normalize_mobile()`，不复制手机号正则。
- `create_tenant_user()` 和 `update_tenant_user()` 校验全局唯一性，冲突转换为稳定业务错误。
- Web 表单新增必填手机号，错误提示不暴露对方租户。

**Step 4: 运行定向测试**

Run: `.venv/bin/python -m pytest tests/test_tenant_admin.py tests/test_tenant_web_isolation.py -q`

Expected: PASS.

### Task 3: 增加平台员工管理

**Files:**
- Create: `app/services/platform_staff.py`
- Create: `app/templates/platform_users.html`
- Modify: `app/web/platform_admin.py`
- Modify: `app/templates/platform_base.html`
- Test: `tests/test_platform_admin.py`

**Step 1: 写失败测试**

- 断言平台管理员可创建 `platform_admin`。
- 断言初始密码只在创建或重置成功响应展示一次。
- 断言不能停用最后一个启用平台管理员。
- 断言平台员工表单和模型不包含租户编码。

**Step 2: 运行失败测试**

Run: `.venv/bin/python -m pytest tests/test_platform_admin.py -q`

Expected: 平台员工服务和页面未存在。

**Step 3: 实现平台员工服务与页面**

- 服务层实现列表、创建、更新启停和重置密码。
- 任何角色或启停变化通过 `auth_version` 和会话吊销立即生效。
- 添加 `/platform/users` 页面和平台导航入口。
- 服务和路由保留详细中文注释，初始密码不写日志或跳转 URL。

**Step 4: 运行定向测试**

Run: `.venv/bin/python -m pytest tests/test_platform_admin.py -q`

Expected: PASS.

### Task 4: 收紧企业导航和旧内部路由

**Files:**
- Modify: `app/main.py`
- Modify: `app/web/tenant_admin.py`
- Modify: `app/web/provider_nodes.py`
- Modify: `app/web/billing.py`
- Modify: `app/templates/dashboard.html`
- Test: `tests/test_public_site_auth.py`
- Test: `tests/test_tenant_security_matrix.py`

**Step 1: 写失败测试**

- 断言企业页面不再渲染“开放接口”、“排障”、“实验台”、“重启服务”。
- 断言企业会话访问 `/monitoring`、`/errors`、`/lab`、`/open-api/apps` 返回 `404`。
- 断言 `/api/open/**` 仍然使用原机器签名边界，未被页面封闭规则误伤。

**Step 2: 运行失败测试**

Run: `.venv/bin/python -m pytest tests/test_public_site_auth.py tests/test_tenant_security_matrix.py -q`

Expected: 旧租户内部页面仍可访问导致失败。

**Step 3: 实现租户产品边界**

- 在认证中间件中对旧内部页面路径做精确 `404` 拦截。
- 从共享租户导航、模块导航和总览页删除相关入口。
- 不删除现有机器 API 和历史处理函数，把数据迁移与路由收口分开。

**Step 4: 运行定向测试**

Run: `.venv/bin/python -m pytest tests/test_public_site_auth.py tests/test_tenant_security_matrix.py -q`

Expected: PASS.

### Task 5: 迁移平台运维和全局日志

**Files:**
- Create: `app/web/platform_operations.py`
- Create: `app/templates/platform_monitoring.html`
- Create: `app/templates/platform_logs.html`
- Modify: `app/web/__init__.py`
- Modify: `app/main.py`
- Modify: `app/web/platform_admin.py`
- Modify: `app/web/platform_billing.py`
- Test: `tests/test_platform_operations.py`

**Step 1: 写失败测试**

- 断言无平台会话访问 `/platform/operations` 和 `/platform/logs` 跳转平台登录。
- 断言有效平台会话可读取全局监控和日志页。
- 断言重启、处理告警缺失或伪造平台 CSRF 时返回 `403`。
- 重启测试只验证到 CSRF 边界，不执行真实脚本。

**Step 2: 运行失败测试**

Run: `.venv/bin/python -m pytest tests/test_platform_operations.py -q`

Expected: 新平台路由未存在。

**Step 3: 实现平台运维 Router**

- Router 只从 `request.state.platform_principal` 取身份。
- 全局查询使用 `SessionLocal`，不使用 `open_tenant_session()`。
- 重启和告警处理表单添加平台 CSRF 字段并在服务端校验。
- 平台导航中添加“平台员工”和“系统运维”。

**Step 4: 运行定向测试**

Run: `.venv/bin/python -m pytest tests/test_platform_operations.py -q`

Expected: PASS.

### Task 6: 轻量联合验证和提交

**Files:**
- Verify: all modified files

**Step 1: 运行定向测试**

Run:

```bash
.venv/bin/python -m pytest \
  tests/test_public_site_auth.py \
  tests/test_tenant_admin.py \
  tests/test_tenant_web_isolation.py \
  tests/test_platform_admin.py \
  tests/test_platform_operations.py \
  tests/test_tenant_security_matrix.py -q
```

Expected: PASS.

**Step 2: 运行语法和差异检查**

Run:

```bash
.venv/bin/python -m compileall -q app
git diff --check
```

Expected: 无输出、退出码为 0。

**Step 3: 中文提交**

```bash
git add app tests docs/plans/2026-08-21-platform-tenant-boundary.md
git commit -m "权限：收口平台与企业双后台边界"
```

提交说明必须如实记录实际执行的定向测试，不得声称已运行全量回归。
