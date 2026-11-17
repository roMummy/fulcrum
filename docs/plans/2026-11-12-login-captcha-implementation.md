# 登录一次性验证码与全局安全日志 Implementation Plan

> **For Claude:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task.

**Goal:** 为企业和平台登录强制接入一次性图形验证码，并通过有界内存和现有平台全局日志提升抗自动化攻击能力，整个失败尝试链路不新增数据库写入。

**Architecture:** 在现有 HMAC PNG 验证码之上增加用途绑定和线程安全的有界内存注册表，签发时登记令牌摘要，消费时原子移除。登录和注册路由共用消费服务；认证事件经统一辅助函数写入现有 `app.log`，平台运维 `/platform/logs` 解析并展示中文标题。账号会话继续使用现有数据库会话表，失败请求不新增数据库记录。

**Tech Stack:** FastAPI、Jinja2、Python 标准库 `OrderedDict`/`threading.Lock`、Pydantic Settings、现有 logging/RotatingFileHandler、pytest、FastAPI TestClient。

---

### Task 1: 实现有界内存一次性验证码服务

**Files:**
- Modify: `app/core/settings.py`
- Modify: `app/services/image_captcha.py`
- Modify: `tests/test_public_site_auth.py`
- Test: `tests/test_image_captcha.py`（新建，承载纯服务测试）

**Step 1: Write the failing tests**

新增测试覆盖：

```python
def test_captcha_challenge_is_bound_to_purpose_and_consumed_once():
    challenge = create_captcha_challenge(purpose="tenant_login", answer="23456")
    assert consume_captcha_challenge(
        challenge.token, "23456", purpose="tenant_login"
    ).valid is True
    assert consume_captcha_challenge(
        challenge.token, "23456", purpose="tenant_login"
    ).valid is False
    assert consume_captcha_challenge(
        create_captcha_challenge(purpose="tenant_login", answer="23456").token,
        "23456",
        purpose="platform_login",
    ).valid is False
```

同时覆盖错误答案消费、过期、篡改签名、容量淘汰、无效用途和注册用途，替换旧的无状态 `verify_captcha` 断言。

**Step 2: Run tests to verify they fail**

Run: `python -m pytest -q tests/test_image_captcha.py tests/test_public_site_auth.py -k captcha`

Expected: FAIL because `consume_captcha_challenge`、用途参数和有界注册表尚未实现。

**Step 3: Write minimal implementation**

- 在 `Settings` 增加 `captcha_cache_max_entries`（默认 4096）和 `captcha_ttl_seconds`（默认 300），并限制运行时值在安全范围内。
- 在 `image_captcha.py` 增加用途集合、结果数据类和 `OrderedDict + Lock` 注册表；只保存令牌摘要、用途和过期时间。
- 让签名载荷包含用途，避免注册、租户登录、平台登录之间复用令牌。
- `create_captcha_challenge` 签发时登记挑战；`consume_captcha_challenge` 先验证签名和用途，再在锁内清理过期项、原子移除令牌，最后校验答案。错误答案也必须完成移除。
- 删除旧的无状态 `verify_captcha` 调用路径，保留一条统一消费入口；所有新增代码使用详细中文注释说明锁、淘汰和不落库边界。

**Step 4: Run tests to verify they pass**

Run: `python -m pytest -q tests/test_image_captcha.py tests/test_public_site_auth.py -k captcha`

Expected: PASS。

**Step 5: Commit**

```bash
git add app/core/settings.py app/services/image_captcha.py tests/test_image_captcha.py tests/test_public_site_auth.py
git commit -m "安全：实现有界内存一次性验证码"
```

### Task 2: 接入认证路由并写入安全日志

**Files:**
- Modify: `app/web/auth_common.py`
- Modify: `app/web/public_site.py`
- Modify: `app/web/platform_auth.py`
- Modify: `app/main.py`
- Modify: `app/services/log_view.py`
- Modify: `tests/test_public_site_auth.py`
- Modify: `tests/test_saas_login.py`
- Test: `tests/test_auth_security_logging.py`（新建）

**Step 1: Write the failing tests**

新增路由和日志测试：

```python
def test_tenant_login_requires_a_fresh_captcha(client, monkeypatch):
    challenge = create_captcha_challenge(purpose="tenant_login", answer="23456")
    monkeypatch.setattr(public_site, "create_captcha_challenge", lambda **_: challenge)
    page = client.get("/login")
    assert 'name="captcha_token"' in page.text
    missing = client.post("/login", data={"mobile": "13800138000", "password": OWNER_PASSWORD})
    assert missing.status_code == 400
    assert "图形验证码错误或已过期" in missing.text
```

覆盖平台入口、正确挑战登录、错误答案、重复挑战、密码错误刷新挑战、用途不匹配，以及 `caplog` 中安全事件的脱敏字段。

**Step 2: Run tests to verify they fail**

Run: `python -m pytest -q tests/test_public_site_auth.py tests/test_saas_login.py tests/test_auth_security_logging.py`

Expected: FAIL because登录模板和路由尚未提交验证码字段，日志辅助函数和公共验证码接口也不存在。

**Step 3: Write minimal implementation**

- 在 `auth_common.py` 增加租户/平台/注册验证码用途常量、手机号/用户名脱敏函数和 `record_auth_event`。日志只接受固定事件名和安全字段，禁止传入密码、答案或原始令牌。
- `public_site.py`：
  - 注册页、企业登录页签发对应用途挑战。
  - 新增公开 `GET /auth/captcha?purpose=...` JSON 接口供“换一张”使用，只接受固定用途。
  - `/login` POST 增加 `captcha_token` 与 `captcha_answer`，先消费挑战，再认证；失败重新渲染新挑战并记录 `login captcha rejected` 或 `login credentials rejected`，成功记录 `login succeeded`。
  - 注册 POST 改用统一消费服务，保持原有注册错误和成功跳转。
- `platform_auth.py`：平台登录 GET/POST 使用平台用途挑战，接入同样的顺序、错误边界和日志事件。
- `main.py`：把 `/auth/captcha` 加入公开路径，确保未登录请求不会被重定向。
- `log_view.py`：为三个安全事件增加中文标题，为 `auth_scope`、`reason`、`identity_hint`、`client_ip` 增加中文字段名。
- 所有改动保留现有会话 Cookie、状态码和账号入口，不添加数据库失败记录；关键分支补充中文注释。

**Step 4: Run tests to verify they pass**

Run: `python -m pytest -q tests/test_public_site_auth.py tests/test_saas_login.py tests/test_auth_security_logging.py`

Expected: PASS。

**Step 5: Commit**

```bash
git add app/web/auth_common.py app/web/public_site.py app/web/platform_auth.py app/main.py app/services/log_view.py tests/test_public_site_auth.py tests/test_saas_login.py tests/test_auth_security_logging.py
git commit -m "安全：登录入口强制验证码并记录脱敏安全事件"
```

### Task 3: 完成认证页换图交互与样式

**Files:**
- Modify: `app/templates/_auth_fields.html`
- Modify: `app/templates/register.html`
- Modify: `app/templates/enterprise_login.html`
- Modify: `app/templates/platform_login.html`
- Modify: `app/static/js/auth.js`
- Modify: `app/static/css/style.css`
- Modify: `tests/test_public_site_auth.py`
- Modify: `tests/test_saas_login.py`

**Step 1: Write the failing tests**

在页面测试中断言三个入口都输出验证码图片、隐藏令牌、数字输入和换图控件；换图接口返回新的令牌和 `data:image/png` 图片，旧令牌再次提交失败。

**Step 2: Run tests to verify they fail**

Run: `python -m pytest -q tests/test_public_site_auth.py tests/test_saas_login.py -k "captcha or login_page"`

Expected: FAIL because现有企业与平台登录模板没有验证码控件，脚本和样式也没有换图实现。

**Step 3: Write minimal implementation**

- 在 `_auth_fields.html` 增加共享 `captcha_field` 宏，保证字段命名、无障碍标签、图片尺寸和刷新 URL 一致。
- 注册、企业登录、平台登录都调用该宏；失败渲染只回填手机号或用户名。
- 在 `auth.js` 为 `[data-captcha-refresh]` 绑定同源 `fetch`，更新隐藏令牌、图片地址并清空答案；接口异常时回退到当前页刷新。提交按钮锁定逻辑保持不变。
- 在 `style.css` 增加认证页验证码行、图片、刷新按钮和窄屏布局，复用现有语义色和圆角/间距变量；补充中文注释说明不改变表单网格。

**Step 4: Run tests to verify they pass**

Run: `python -m pytest -q tests/test_public_site_auth.py tests/test_saas_login.py -k "captcha or login_page"`

Expected: PASS。

**Step 5: Commit**

```bash
git add app/templates/_auth_fields.html app/templates/register.html app/templates/enterprise_login.html app/templates/platform_login.html app/static/js/auth.js app/static/css/style.css tests/test_public_site_auth.py tests/test_saas_login.py
git commit -m "界面：为认证页增加验证码换图交互"
```

### Task 4: 轻量完整验证与结果记录

**Files:**
- Verify: `app/services/image_captcha.py`, `app/web/public_site.py`, `app/web/platform_auth.py`, `app/web/auth_common.py`, `app/services/log_view.py`
- Verify: `tests/test_image_captcha.py`, `tests/test_public_site_auth.py`, `tests/test_saas_login.py`, `tests/test_auth_security_logging.py`

**Step 1: Run focused tests**

Run: `python -m pytest -q tests/test_image_captcha.py tests/test_public_site_auth.py tests/test_saas_login.py tests/test_auth_security_logging.py`

Expected: PASS；若出现与本次验证码无关的历史失败，只记录具体失败，不扩大范围。

**Step 2: Run syntax and diff checks**

Run: `python -m compileall -q app/services/image_captcha.py app/web/auth_common.py app/web/public_site.py app/web/platform_auth.py app/services/log_view.py` 

Expected: exit code 0。

Run: `git diff --check`

Expected: no output。

**Step 3: Review no-database-write boundary**

Run: `rg -n "Captcha|captcha|login.*throttle|LoginThrottle|captcha_challenges|login_throttles" app migrations tests`

Expected: 只有内存验证码、路由字段、日志和测试引用；没有新模型、迁移或失败请求 `db.add`。

**Step 4: Commit any verification-only correction**

若验证发现本次引入的阻断性问题，先修正并重新执行对应定向检查，再使用中文 commit 说明实际修复；没有代码变更时不创建空提交。

