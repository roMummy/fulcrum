# WeBridge 长轮询微信渠道接入 Implementation Plan

> **For Claude:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task.

**Goal:** 在多租户 SaaS 中新增 `wechat_webridge` 渠道，接入 WeBridge 8080 服务端长轮询协议，完成文本消息收发、设备状态和事件幂等处理。

**Architecture:** 新增独立渠道目录定义、账号连接器、WeBridge 长轮询协议服务和渠道适配器。手机通过渠道专属 `/agent/*` 路由主动上报，云端把出站消息放入按渠道隔离的任务队列，并在 `/agent/poll` 响应中下发 `send_message` 命令；入站事件复用现有统一事件归一化和租户会话链路。

**Tech Stack:** FastAPI、SQLAlchemy、现有 `TaskJob`/渠道事件服务、Python 标准库 JSON/HTTP、pytest/unittest。

---

### Task 1: 建立协议契约与运行状态服务

**Files:**
- Create: `app/services/wechat_webridge/contract.py`
- Create: `app/services/wechat_webridge/service.py`
- Modify: `app/services/__init__.py`（如需导出）
- Test: `tests/test_wechat_webridge_service.py`

**Step 1: Write the failing test**

覆盖基础地址规范化、设备注册与渠道匹配、状态快照、事件去重键和命令结果状态转换。

**Step 2: Run test to verify it fails**

Run: `.venv/bin/python -m pytest tests/test_wechat_webridge_service.py -q`
Expected: FAIL，因为契约和服务尚未存在。

**Step 3: Write minimal implementation**

实现固定的五个动作契约、设备/会话/账号摘要解析、按渠道隔离的运行快照存储、事件幂等判断和结果摘要。所有公开函数写详细中文注释，敏感字段只保存摘要或哈希。

**Step 4: Run test to verify it passes**

Run: `.venv/bin/python -m pytest tests/test_wechat_webridge_service.py -q`
Expected: PASS。

**Step 5: Commit**

```bash
git add app/services/wechat_webridge tests/test_wechat_webridge_service.py
git commit -m "功能：新增 WeBridge 协议契约和运行状态服务"
```

### Task 2: 增加 WeBridge 入站事件和长轮询协议路由

**Files:**
- Create: `app/api/wechat_webridge.py`
- Modify: `app/main.py`
- Test: `tests/test_wechat_webridge_api.py`

**Step 1: Write the failing test**

覆盖 `register/state/event/poll/command_result` 五个 POST 路由：注册成功、未知设备拒绝、事件进入统一入站函数、无命令返回空数组、结果回报幂等。

**Step 2: Run test to verify it fails**

Run: `.venv/bin/python -m pytest tests/test_wechat_webridge_api.py -q`
Expected: FAIL，因为路由未注册。

**Step 3: Write minimal implementation**

按现有租户上下文和渠道查询方式实现路由。`poll` 使用短暂 hold 或立即返回空命令，不能阻塞数据库会话；命令领取和完成必须带渠道 ID、设备 ID、请求 ID。事件处理调用现有 `ingest_inbound_event`，不复制消息落库逻辑。

**Step 4: Run test to verify it passes**

Run: `.venv/bin/python -m pytest tests/test_wechat_webridge_api.py -q`
Expected: PASS。

**Step 5: Commit**

```bash
git add app/api/wechat_webridge.py app/main.py tests/test_wechat_webridge_api.py
git commit -m "功能：接入 WeBridge 长轮询协议路由"
```

### Task 3: 接入统一渠道目录和账号连接验证

**Files:**
- Modify: `app/services/channel_catalog.py`
- Modify: `app/services/account_connections/registry.py`
- Create: `app/services/account_connections/wechat_webridge.py`
- Test: `tests/test_wechat_webridge_catalog.py`

**Step 1: Write the failing test**

验证渠道编码、展示字段、能力集合、必填配置、适配器工厂和账号连接器注册。

**Step 2: Run test to verify it fails**

Run: `.venv/bin/python -m pytest tests/test_wechat_webridge_catalog.py -q`
Expected: FAIL，因为目录没有新渠道。

**Step 3: Write minimal implementation**

注册 `wechat_webridge`，声明 `send/inbound/health/polling` 能力和服务端基础地址配置。账号连接器基于最近注册状态生成稳定 `VerifiedAccountIdentity`，无法确认时返回明确失败，不回退到其他微信渠道。

**Step 4: Run test to verify it passes**

Run: `.venv/bin/python -m pytest tests/test_wechat_webridge_catalog.py -q`
Expected: PASS。

**Step 5: Commit**

```bash
git add app/services/channel_catalog.py app/services/account_connections app/services/account_connections/wechat_webridge.py tests/test_wechat_webridge_catalog.py
git commit -m "功能：把 WeBridge 注册到渠道目录和账号连接"
```

### Task 4: 实现文本出站适配器与任务完成回报

**Files:**
- Create: `app/services/channel_adapters/wechat_webridge.py`
- Modify: `app/services/channel_adapters/registry.py`
- Modify: `app/services/task_queue.py`（仅接入现有队列扩展点）
- Test: `tests/test_wechat_webridge_adapter.py`

**Step 1: Write the failing test**

验证文本消息被转换为 WeBridge `send_message` 命令，目标会话和请求 ID 正确，未支持的媒体类型明确失败，命令结果可转换成统一发送结果。

**Step 2: Run test to verify it fails**

Run: `.venv/bin/python -m pytest tests/test_wechat_webridge_adapter.py -q`
Expected: FAIL，因为适配器未注册。

**Step 3: Write minimal implementation**

适配器只负责把统一 payload 转换成任务命令并读取结果，不直接模拟微信成功。仅支持文本；图片、语音、视频、文件返回 `unsupported_message_type`。所有分支写中文注释说明协议字段和业务边界。

**Step 4: Run test to verify it passes**

Run: `.venv/bin/python -m pytest tests/test_wechat_webridge_adapter.py -q`
Expected: PASS。

**Step 5: Commit**

```bash
git add app/services/channel_adapters app/services/task_queue.py tests/test_wechat_webridge_adapter.py
git commit -m "功能：新增 WeBridge 文本消息渠道适配器"
```

### Task 5: 完成定向验证、文档和最终提交

**Files:**
- Modify: `docs/bridge-api.md` 或新增 `docs/wechat_webridge_provider.md`
- Modify: `CHANGELOG.md`
- Test: `tests/test_wechat_webridge_*.py`

**Step 1: Run focused validation**

Run: `.venv/bin/python -m pytest tests/test_wechat_webridge_service.py tests/test_wechat_webridge_api.py tests/test_wechat_webridge_catalog.py tests/test_wechat_webridge_adapter.py -q`
Expected: PASS。

**Step 2: Run syntax/static checks**

Run: `.venv/bin/python -m compileall -q app/services/wechat_webridge app/services/account_connections/wechat_webridge.py app/services/channel_adapters/wechat_webridge.py app/api/wechat_webridge.py`
Expected: exit code 0。

**Step 3: Update documentation**

记录渠道创建方式、长轮询五个端点、首期支持范围和真机验收边界，明确未执行完整端到端和真实手机验收。

**Step 4: Commit**

```bash
git add docs CHANGELOG.md tests
git commit -m "文档：补充 WeBridge 渠道接入说明和定向验证记录"
```
