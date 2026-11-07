# 微信 iPad 二维码运行状态精简 Implementation Plan

> **For Claude:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task.

**Goal:** 让真实 874 二维码回包可以在 MariaDB 中稳定持久化，并保持租户接入页面正常显示二维码。

**Architecture:** 服务层只把 UUID、二维码 URL、过期时间与结构化调用摘要写入渠道运行状态。统一清理历史 Base64 字段，不扩展数据库字段，也不保留 Base64 回退展示路径。

**Tech Stack:** Python 3.11、FastAPI、SQLAlchemy、pytest、MariaDB、Docker Compose。

---

### Task 1: 固化超大二维码回包边界

**Files:**
- Modify: `tests/test_multi_bot_admin.py`

**Step 1: Write the failing test**

新增真实量级 Base64 回包测试，断言取码成功后：

- `runtime_info_json` 小于 MariaDB `TEXT` 上限；
- 不包含 `qrcode_image_base64`；
- 不包含原始 `QrBase64`；
- 保留 UUID、二维码 URL 和精简调用摘要；
- 二维码图片路由继续重定向到短 URL。

**Step 2: Run test to verify it fails**

Run: `.venv/bin/python -m pytest tests/test_multi_bot_admin.py -k "qrcode_runtime_omits_large_base64" -q`

Expected: FAIL，当前实现会把完整 Base64 和原始回包写入 `runtime_info_json`。

### Task 2: 精简二维码运行状态

**Files:**
- Modify: `app/services/wechat_ipad/service.py`
- Modify: `tests/test_multi_bot_admin.py`

**Step 1: Write minimal implementation**

- 增加运行状态精简函数，删除二维码 Base64 与媒体类型字段。
- 把 `last_login_init_response` 统一转换为消息、返回码、HTTP 状态和字段清单。
- 成功取码只接受二维码 URL 进入可展示状态，不再把 Base64 作为持久化回退。
- 调整旧的 Base64 展示测试，使其验证短 URL 单一路径。

**Step 2: Run focused tests**

Run: `.venv/bin/python -m pytest tests/test_multi_bot_admin.py -k "qrcode or login_flow" -q`

Expected: PASS。

Run: `.venv/bin/python -m pytest tests/test_account_connection_pages.py tests/test_account_connection_service.py tests/test_platform_protocol_services.py -q`

Expected: PASS。

Run: `.venv/bin/python -m compileall -q app`

Expected: exit 0。

**Step 3: Commit**

```bash
git add app/services/wechat_ipad/service.py tests/test_multi_bot_admin.py
git commit -m "修复：精简微信iPad二维码运行状态"
```

### Task 3: 推送、部署与真实流程验证

**Files:**
- No source file changes.

**Step 1: Push**

Run: `git push`

Expected: 远端分支更新到本次修复提交。

**Step 2: Deploy**

在 142 当前部署目录执行差异构建，只重建应用服务，不重启 MariaDB。

**Step 3: Verify runtime**

- `/healthz` 与 `/readyz` 返回成功。
- 使用演示企业账号创建或打开微信 iPad 草稿。
- 调用登录流程开始接口返回 JSON 200。
- 返回 `qrcode_visible=true`，二维码图片地址可访问。
- 数据库中的 `runtime_info_json` 不包含 Base64，长度远低于 64 KB。

