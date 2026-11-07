# Windows 本地代理扫码连接器 Implementation Plan

> **For Claude:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task.

**Goal:** 建立一个独立 Windows 连接器，让用户只在微信扫码登录阶段提供本地 SOCKS5 代理，登录成功后由 SaaS 清除 874 中的代理并释放隧道，后续账号运行不依赖连接器。

**Architecture:** SaaS 保存租户归属的连接器、一次性激活码和设备凭证摘要。Windows 连接器主动通过 WebSocket 建立出站连接；登录页面创建短时 `local_connector` 会话，服务端只把中继目标和临时会话令牌发给已认证连接器。登录成功后调用 874 `Tools/setproxy` 写入空代理，成功后结束会话并通知连接器关闭隧道。

**Tech Stack:** FastAPI、WebSocket、SQLAlchemy、Alembic、现有登录代理会话和 874 客户端、Windows Python 连接器（websockets、asyncio、Windows DPAPI 可选）。

---

### Task 1: 增加连接器数据模型和迁移

**Files:**
- Modify: `app/models/saas.py`
- Modify: `app/models/__init__.py`
- Create: `migrations/versions/20261107_0020_local_proxy_connectors.py`
- Test: `tests/test_saas_models.py`

**Steps:**
1. 增加 `ProxyConnector`、`ProxyConnectorActivationCode`、`ProxyConnectorCredential` 模型，状态、租户、设备摘要、心跳和撤销字段使用数据库约束。
2. 将 `local_connector` 加入登录代理模式，并为 `LoginProxySession` 增加 `proxy_connector_id`、中继凭证摘要和清理状态字段。
3. 编写 Alembic 升级和降级，保持现有表数据不变。
4. 增加模型元数据、约束和唯一性定向测试。
5. 运行 `pytest tests/test_saas_models.py -q` 并提交中文 commit。

### Task 2: 实现激活码、设备凭证和连接器服务

**Files:**
- Create: `app/services/local_proxy_connector.py`
- Modify: `app/services/login_proxy_pool.py`
- Test: `tests/test_local_proxy_connector.py`

**Steps:**
1. 实现租户管理员创建连接器和一次性激活码，数据库只保存摘要，响应只返回一次原文激活码。
2. 实现激活码消费、设备密钥签发、设备认证、停用、心跳和在线连接器列表；所有查询按可信租户隔离。
3. 实现 `local_connector` 登录会话创建和临时中继凭证解析，禁止上传或保存本地 SOCKS 主机、用户名和密码。
4. 增加激活码一次性消费、过期、撤销、跨租户隔离、设备吊销和敏感字段不落库测试。
5. 运行定向测试并提交中文 commit。

### Task 3: 增加连接器 WebSocket 会话注册和临时流转发边界

**Files:**
- Create: `app/services/local_proxy_connector_ws.py`
- Create: `app/api/local_proxy_connector.py`
- Modify: `app/main.py`
- Test: `tests/test_local_proxy_connector_ws.py`

**Steps:**
1. 增加设备密钥认证的 WebSocket 端点，支持 `hello`、`hello_ack`、`heartbeat`、`open_result`、`close` 和 `session_end` 控制消息。
2. 维护进程内设备连接注册表，连接断开时把关联登录会话标记失败并释放租约。
3. 为每次登录会话生成只绑定会话的临时 SOCKS 凭证，限制连接数、空闲时间和总时长；中继只允许连接器本地配置的 SOCKS 地址。
4. 增加断线、错误消息、过期令牌和跨会话复用的定向测试。
5. 运行定向测试并提交中文 commit。

### Task 4: 接入租户页面和扫码登录流程

**Files:**
- Modify: `app/web/tenant_admin.py`
- Modify: `app/templates/channel_connect.html`
- Modify: `app/templates/_wechat_ipad_login_network.html`
- Modify: `app/main.py`
- Modify: `app/services/wechat_ipad/login_flow.py`
- Test: `tests/test_local_proxy_connector_pages.py`
- Test: `tests/test_wechat_ipad_login_proxy_flow.py`

**Steps:**
1. 增加租户管理员的连接器列表、创建、激活码展示一次和停用入口；页面只展示名称、在线状态、脱敏代理标签和版本。
2. 在扫码登录网络选择中增加“本地代理连接器”，只列出当前租户在线连接器，不显示本地 SOCKS 密码。
3. `start_login_with_proxy_session` 使用连接器中继取码，二维码轮询继续复用同一会话。
4. 登录状态变为成功后先调用 `WechatIpadApiClient.set_proxy(..., proxy={})`，确认 874 返回成功后再结束会话并发送 `session_end`；清理失败时保持连接器会话并返回可重试错误。
5. 增加页面脱敏、连接器断开、登录成功清理代理和清理失败保持会话测试。
6. 运行定向测试并提交中文 commit。

### Task 5: 创建 Windows 独立连接器程序

**Files:**
- Create: `connectors/windows_local_proxy_connector/README.md`
- Create: `connectors/windows_local_proxy_connector/requirements.txt`
- Create: `connectors/windows_local_proxy_connector/main.py`
- Create: `connectors/windows_local_proxy_connector/config.py`
- Create: `connectors/windows_local_proxy_connector/socks5.py`
- Create: `connectors/windows_local_proxy_connector/tunnel.py`
- Create: `connectors/windows_local_proxy_connector/build.bat`
- Test: `tests/test_windows_local_proxy_connector.py`

**Steps:**
1. 实现首次启动配置、激活码兑换、设备密钥本地受保护保存和 SaaS 地址校验。
2. 实现出站 WebSocket、心跳、自动重连和登录会话生命周期；连接器退出后不保留运行期代理连接。
3. 实现 SOCKS5 本地连接和二进制双向转发，只接受服务端发来的受控目标请求。
4. 提供 Windows 打包脚本和中文使用说明，明确用户扫码登录完成后即可退出连接器。
5. 运行 Python 语法、协议单元测试和最小 SOCKS5 回环测试并提交中文 commit。

### Task 6: 集成验证和部署

**Files:**
- Modify: `docs/plans/2026-11-07-local-proxy-connector-implementation.md`
- Test: `tests/test_local_proxy_connector.py`, `tests/test_local_proxy_connector_ws.py`, `tests/test_wechat_ipad_login_proxy_flow.py`

**Steps:**
1. 运行本次功能相关的轻量测试和 Alembic 检查，记录未执行的全量验证。
2. 在本地使用模拟 SOCKS5 和模拟 874 验证取码、轮询、成功清理 `setproxy`、关闭隧道后的直连运行路径。
3. 提交全部代码后，按仓库约束 SSH 到 `192.168.2.142`，执行 `git pull`、迁移和服务重启；检查健康接口、页面响应和 WebSocket 握手日志。
4. 记录 Windows 真机、真实 SOCKS5 和真实微信扫码仍需外部验收的边界。

