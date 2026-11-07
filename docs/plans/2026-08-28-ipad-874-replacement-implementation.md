# iPad 874 Protocol Replacement Implementation Plan

> **For Claude:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task.

**Goal:** 将现有 `wechat_ipad` 原位替换为 `ipad-874` 最新 HTTP 契约，并删除旧协议兼容路径。

**Architecture:** 通过独立契约模块集中管理固定版本、默认端口和端点，客户端统一编码查询参数，运行服务和渠道适配器只引用该契约。账号身份、渠道编码和通用运行能力保持不变，不新增 Provider 依赖或第二套入口。

**Tech Stack:** Python 3.10、FastAPI、SQLAlchemy、urllib、Pytest、Docker Compose

---

### Task 1: 建立 874 契约测试

**Files:**
- Create: `tests/test_wechat_ipad_874_contract.py`
- Reference: `.run/protocol-sources/ipad-874/swagger/swagger.json`

**Step 1: 写入失败测试**

覆盖协议源提交、默认端口、登录和工具端点、查询参数编码、唤醒请求体、旧数据库探针移除及配置字段清理。

**Step 2: 运行测试确认失败**

Run: `pytest -q tests/test_wechat_ipad_874_contract.py`

Expected: 因契约模块不存在或旧端点仍存在而失败。

**Step 3: 提交测试**

```bash
git add tests/test_wechat_ipad_874_contract.py
git commit -m "测试：建立iPad 874协议契约"
```

### Task 2: 新增统一 874 契约并替换客户端

**Files:**
- Create: `app/services/wechat_ipad/contract.py`
- Modify: `app/services/wechat_ipad/client.py`

**Step 1: 定义固定契约**

集中声明协议仓库、源提交、默认服务地址以及登录、消息、联系人、群聊、用户和工具端点。每组常量使用中文注释说明业务边界和对应 874 路由。

**Step 2: 统一请求构造**

为 `request_json` 增加结构化 `query` 参数，使用 `urllib.parse.urlencode` 生成 URL。删除客户端方法的 `endpoint` 覆盖参数，并将缓存查询、唤醒登录和工具方法改为 874 请求格式。

**Step 3: 删除废弃能力**

删除 `check_database` 和 `/CheckDatabaseOK`，确保客户端文件中不再出现旧登录、旧工具端点。

**Step 4: 运行契约测试**

Run: `pytest -q tests/test_wechat_ipad_874_contract.py`

Expected: 客户端契约测试通过。

### Task 3: 清理运行服务的端点兼容

**Files:**
- Modify: `app/services/wechat_ipad/service.py`
- Modify: `tests/test_multi_bot_admin.py`

**Step 1: 删除端点配置读取**

二维码登录、退出、状态刷新、心跳、二次登录、唤醒、消息同步和群同步直接引用统一契约，不再从 `adapter_config_json` 读取 `*_endpoint`。

**Step 2: 删除数据库探针动作**

从操作台动作、动作规格和相关旧测试中移除 `check_database`。

**Step 3: 运行服务定向测试**

Run: `pytest -q tests/test_multi_bot_admin.py -k 'wechat_ipad and not qrcode_image'`

Expected: iPad 协议运行服务相关测试通过。

### Task 4: 对齐通用渠道适配器和配置入口

**Files:**
- Modify: `app/services/channel_adapters/wechat_ipad.py`
- Modify: `app/services/channel_catalog.py`
- Modify: `app/templates/channel_connect.html`
- Modify: `app/services/account_connections/service.py`
- Modify: `tests/test_channel_catalog.py`
- Modify: `tests/test_multi_channel_pages.py`

**Step 1: 复用统一客户端契约**

健康检查通过客户端发送 874 心跳和缓存查询；消息发送端点从契约模块读取，避免适配器自行拼接 `/api` 路径。

**Step 2: 清理无效访问凭证**

微信 iPad 配置目录移除 `bot_token`，连接表单只在其他需要凭证的渠道显示 Token 输入。账号连接服务仅为目录中声明的字段保存凭证。

**Step 3: 更新默认端口**

协议服务地址提示改为 `http://127.0.0.1:9064`，并增加页面与目录断言。

**Step 4: 运行渠道定向测试**

Run: `pytest -q tests/test_account_connection_connectors.py tests/test_channel_catalog.py tests/test_multi_channel_pages.py`

Expected: 账号身份、渠道目录和连接页面测试通过。

### Task 5: 集中验证并提交实现

**Files:**
- Modify: 本计划涉及的实现和测试文件

**Step 1: 运行轻量验证**

```bash
pytest -q tests/test_wechat_ipad_874_contract.py tests/test_account_connection_connectors.py tests/test_channel_catalog.py tests/test_channel_runtime.py tests/test_multi_channel_pages.py
python -m compileall -q app/services/wechat_ipad app/services/channel_adapters/wechat_ipad.py app/services/account_connections/wechat_ipad.py
```

Expected: 所有定向测试和语法检查通过。

**Step 2: 检查旧契约已清除**

Run: `rg -n 'Login/GetQR|Login/CheckQR|Login/Awaken|GetChatroomQRCode|GetMyQRCode|/DownloadVoice|/DownloadVideo|/SetStep|/SetProxy|CheckDatabaseOK|8848' app tests`

Expected: 不再出现 iPad 旧契约；测试夹具中与其他渠道无关的地址除外。

**Step 3: 提交实现**

```bash
git add app tests
git commit -m "功能：替换为最新iPad 874协议"
```

### Task 6: 推送、部署和运行验证

**Files:**
- Runtime: `docker-compose.yml`

**Step 1: 推送功能分支**

Run: `git push -u origin feature/ipad-874-adapter`

Expected: 远端分支更新到本地实现提交。

**Step 2: 在 192.168.2.142 更新代码**

通过项目指定 SSH 账号进入服务器，拉取 `feature/ipad-874-adapter`，构建并仅更新应用容器。

**Step 3: 验证运行状态**

检查应用容器和数据库容器健康状态，访问 `http://192.168.2.142:8280/healthz`、`/readyz` 及微信 iPad 连接页面。

Expected: 服务正常，页面显示 874 默认端口且无访问凭证字段。

**Step 4: 记录验收边界**

说明已完成 Swagger/HTTP 契约级验证；真实扫码、心跳和收发消息需在提供运行中的 874 服务地址后补充验收。
