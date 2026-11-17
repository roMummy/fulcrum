# 猜图片题库界面整合 Implementation Plan

> **For Claude:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task.

**Goal:** 将猜图片整理为符合当前后台风格的单一缩略图清单，所有题目管理操作通过弹窗完成。

**Architecture:** 题库主页面继续由 Jinja 渲染列表与筛选。专用 JavaScript 管理共用编辑弹窗、导入步骤与资源检查，使用现有租户路由的单一 JSON 协议，成功后刷新保留当前筛选。保留租户隔离、CSRF、导入令牌和审计，删除独立旧页面。

**Tech Stack:** FastAPI、SQLAlchemy、Jinja2、原生 dialog/FormData/fetch、现有 Soft UI CSS、pytest、Playwright。

---

### Task 1: 迁移题库弹窗接口与必要测试

**Files:**
- Modify: `app/web/guess_image.py`
- Test: `tests/test_guess_image_pages.py`

1. 更新页面测试：只读仅有清单；GET `/new` 不再渲染旧表单；详情返回 JSON 并保持跨租户 404。
2. 更新保存与导入断言：创建/更新/删除返回成功 JSON；缺图片、缺授权等返回 400 detail；预览返回 token/report；确认仍幂等且会话隔离。
3. 删除新增 GET 与旧 HTML 返回分支，详情 JSON 包含原始字段和图片地址。创建/编辑失败 rollback，返回 JSON 错误，不丢失客户端输入；保存继续 flush 并审计。
4. 预览、确认与资源检查改为 JSON；导出继续文件下载；检查响应使用 private/no-store。
5. 运行定向页面测试并修复本次阻断问题。

### Task 2: 实现统一清单与三个业务弹窗

**Files:**
- Modify: `app/templates/guess_image_bank.html`
- Modify: `app/static/css/style.css`（替换旧 `.gi-*` 区段）
- Create: `app/static/js/guess-image.js`
- Delete: `app/templates/guess_image_form.html`
- Delete: `app/templates/guess_image_import.html`
- Delete: `app/templates/guess_image_audit.html`

1. 使用 `back/page_intro/hero_actions` 块提供统一页头和带可访问名称的图标按钮。
2. 使用 `page-section`、`compact-filter` 和缩略图清单，状态数量靠近清单标题；保留所有题目字段、筛选、分页与统一通知。
3. 添加共享题目弹窗、导入弹窗、资源检查弹窗，复用 `app-modal/form-grid`；只读不输出管理弹窗。
4. 实现题目加载与重置、Object URL 图片预览与释放、状态改变时授权必填、异步保存、错误展示、删除确认与刷新。
5. 实现 ZIP 选择/预览/确认状态，失效或失败时可重新预览；用 `textContent` 展示不可信内容。
6. 实现检查结果的加载与重试；处理弹窗关闭与请求迟到，锁定重复提交并恢复按钮。
7. 清单在手机重排、弹窗内部滚动，保证输入和增强 select 的包装节点可收缩、触控目标至少 44px。

### Task 3: 验证、记录并中文提交

**Files:**
- Modify: `docs/ui/soft-ui-page-checklist.md`
- Local artifacts: `.run/guess-image-ui-preview/`（忽略，不提交）

1. 执行 `node --check app/static/js/guess-image.js`、Python AST、Jinja 加载与 `git diff --check`。
2. 使用 `.venv/bin/python` 执行 `tests/test_guess_image_pages.py`，必要时直接相关的题库服务测试；沙箱已有 TestClient 限制，使用获准沙箱外执行。
3. 使用真实模板和静态资源，在 1440×900、1024×768、390×844 检查清单、添加、编辑、图片预览、校验失败留输入、导入与资源检查；接口模拟与真实服务端测试分开记录。
4. 记录截图位置与实际验证边界，不宣称全量回归或线上验证。
5. 仅暂存本次文件，保留原有未跟踪群功能文件；执行中文 commit，说明改动与实际轻量测试。

用户已要求直接实施，当前会话按顺序执行，不再等待执行方式选择。
