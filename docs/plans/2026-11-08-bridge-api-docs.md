# 桥接接口文档中心 Implementation Plan

> **For Claude:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task.

**Goal:** 为已登录的租户员工提供一个 `/developer/docs` 文档中心，帮助他们绕过插件演示，直接使用已授权渠道开发自己的业务系统。

**Architecture:** 新增独立的租户文档 Router 和 Jinja 页面。Router 只读取固定的 `docs/bridge-api.md` Markdown 文件，使用现有 Python Markdown 依赖渲染为页面内容，并复用租户登录与主导航。桥接接口内容以 `/api/open/*` 为推荐协议，`/api/tool/*` 只保留兼容说明，不改动现有业务 API。

**Tech Stack:** FastAPI、Jinja2、Python Markdown、现有租户会话中间件、现有 CSS 设计令牌、pytest。

---

### Task 1: 增加桥接接口文档正文

**Files:**
- Create: `docs/bridge-api.md`
- Reference: `app/api/open_api.py`
- Reference: `app/services/open_api_auth.py`
- Reference: `app/services/open_webhooks.py`
- Reference: `docs/tool_api_for_osm.md`

**Step 1: Write the document sections**

写入快速开始、前置条件、HMAC 鉴权、资源概念、推荐读取接口、推荐发送接口、Webhook、错误与幂等、兼容工具接口和业务架构建议。所有请求字段和响应字段以当前实现为准，示例只使用占位符。

**Step 2: Validate interface names against source**

运行：

```bash
rg -n "@router\.(get|post)|X-App-Key|X-Timestamp|X-Signature|request_id|event_type" app/api/open_api.py app/services/open_api_auth.py app/services/open_webhooks.py
```

预期：文档中的推荐接口、鉴权字段、事件字段均能在当前源代码中找到对应实现。

**Step 3: Check Markdown quality**

运行：

```bash
.venv/bin/python - <<'PY'
from pathlib import Path
import markdown

content = Path("docs/bridge-api.md").read_text(encoding="utf-8")
html = markdown.markdown(content, extensions=["extra", "codehilite"])
assert "api/open/messages" in html
assert "X-Signature" in html
assert len(html) > 1000
PY
```

预期：Markdown 能被当前依赖解析，核心接口和鉴权章节存在。

**Step 4: Commit**

```bash
git add docs/bridge-api.md
git commit -m "文档：增加桥接接口开发说明"
```

### Task 2: 新增登录后的文档 Router

**Files:**
- Create: `app/web/developer_docs.py`
- Modify: `app/main.py`
- Test: `tests/test_developer_docs.py`

**Step 1: Write failing route tests**

覆盖固定文档路径存在、未登录请求重定向 `/login`、已登录请求返回 `200`、响应包含文档标题和推荐接口路径。测试不得依赖真实生产数据库或真实租户密钥，复用现有测试应用和临时运行目录。

**Step 2: Run the focused tests**

```bash
PYTHONPATH=. .venv/bin/pytest -q tests/test_developer_docs.py
```

预期：在 Router 和模板尚未创建时失败。

**Step 3: Implement the Router**

实现 `GET /developer/docs`：

- 使用现有 `request.state.current_user` 判断登录状态。
- 未登录返回到 `/login`。
- 固定读取 `APP_ROOT / "docs" / "bridge-api.md"`，不接受请求参数路径。
- 使用 `markdown.markdown(..., extensions=["extra", "codehilite"])` 转换内容。
- 将渲染结果以明确的 HTML 字段传给模板，并复用 `build_tenant_navigation` 和现有账号上下文。
- 文件不存在或解析异常时只返回稳定的 `503` 文档不可用响应，不输出绝对路径和异常堆栈。

在 `app/main.py` 注册该 Router，不改变任何 `/api/open/*` 路由。

**Step 4: Run the focused tests**

```bash
PYTHONPATH=. .venv/bin/pytest -q tests/test_developer_docs.py
```

预期：路由认证和文档内容测试通过。

**Step 5: Commit**

```bash
git add app/web/developer_docs.py app/main.py tests/test_developer_docs.py
git commit -m "功能：增加登录后的桥接接口文档路由"
```

### Task 3: 增加文档页面和租户导航入口

**Files:**
- Create: `app/templates/developer_docs.html`
- Modify: `app/web/tenant_navigation.py`
- Modify: `app/static/css/style.css`
- Test: `tests/test_developer_docs.py`

**Step 1: Extend the page assertions**

增加模板渲染断言：页面显示“开发文档”、推荐接口提示、代码块和响应式文档容器；主导航在 `/developer/docs` 上将入口标记为 active。

**Step 2: Implement the page**

模板继承 `base.html`，只展示文档内容和适用范围说明。Markdown 生成的 HTML 使用模板的受控 `|safe` 输出；Router 必须保证输入只能来自版本库中的固定文档文件。页面不显示任何租户密钥、应用密钥、扫码二维码或代理凭证。

在租户主导航中增加“开发文档”入口，使用 `/developer/docs` 路径判断 active。补充文档标题、段落、表格、代码块和窄屏横向滚动样式，继续使用现有颜色和间距变量。

**Step 3: Run focused checks**

```bash
PYTHONPATH=. .venv/bin/pytest -q tests/test_developer_docs.py tests/test_open_api.py
.venv/bin/python -m compileall -q app/web/developer_docs.py
git diff --check
```

预期：文档页面和既有开放接口测试通过，Python 语法检查与差异检查通过。

**Step 4: Commit**

```bash
git add app/templates/developer_docs.html app/web/tenant_navigation.py app/static/css/style.css tests/test_developer_docs.py
git commit -m "界面：增加租户开发文档中心入口"
```

### Task 4: 运行态验证与发布记录

**Files:**
- Modify: `README.md`
- Modify: `CHANGELOG.md`

**Step 1: Add navigation references**

在 README 的开发者相关章节加入文档中心用途和 `docs/bridge-api.md` 内容源说明；CHANGELOG 记录登录后文档中心和推荐 `/api/open/*` 协议。

**Step 2: Run the targeted validation set**

```bash
PYTHONPATH=. .venv/bin/pytest -q tests/test_developer_docs.py tests/test_open_api.py tests/test_open_webhooks.py
git diff --check
```

预期：全部定向测试通过；若历史环境导致与本次无关的失败，只记录具体失败，不扩大本次范围。

**Step 3: Verify the live page locally or on the target service**

验证未登录 `/developer/docs` 返回 `303 -> /login`，登录态访问返回 `200`，页面包含 `/api/open/messages` 和 HMAC 鉴权章节；检查日志不包含密钥和绝对文件路径。

**Step 4: Commit**

```bash
git add README.md CHANGELOG.md
git commit -m "文档：记录桥接接口文档中心入口"
```

**Step 5: Deploy only after all focused checks pass**

按照仓库部署约束 SSH 到 `192.168.2.142`，执行 `git pull` 后运行现有部署脚本，验证 `/healthz`、`/readyz` 和文档页面。部署说明中如实记录轻量测试结果，不宣称完成全量回归。

