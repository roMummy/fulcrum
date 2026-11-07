# Platform Managed iPad Service Implementation Plan

> **For Claude:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task.

**Goal:** 将 iPad 874 服务地址收归平台后台维护，让租户接入时只填写账号名称并扫码。

**Architecture:** 使用平台级协议服务实体保存唯一渠道地址，渠道目录声明哪些渠道使用平台托管地址。平台保存配置时同步已有渠道，租户创建与编辑流程只能读取平台结果，不能接受租户覆盖。

**Tech Stack:** Python 3.10、FastAPI、SQLAlchemy、Alembic、Jinja2、Pytest、Docker Compose

---

### Task 1: 新增平台协议服务实体与迁移

**Files:**
- Modify: `app/models/saas.py`
- Modify: `app/models/__init__.py`
- Create: `migrations/versions/20260828_0009_platform_protocol_services.py`

**Steps:**
1. 新增 `PlatformProtocolService`，保存唯一渠道编码、服务地址和开放状态。
2. 新增 Alembic 迁移并连接 `20260827_0008`。
3. 运行模型和迁移语法检查。

### Task 2: 实现平台协议服务管理能力

**Files:**
- Create: `app/services/platform_protocol_services.py`
- Modify: `app/services/channel_catalog.py`
- Test: `tests/test_platform_protocol_services.py`

**Steps:**
1. 为渠道目录增加 `platform_managed_endpoint` 标记，并只为 `wechat_ipad` 启用。
2. 实现查询、保存和要求可用服务三个入口。
3. 保存地址时批量同步同类型渠道的 `api_base` 并记录平台审计。
4. 运行服务定向测试。

### Task 3: 增加平台管理页面

**Files:**
- Modify: `app/web/platform_admin.py`
- Create: `app/templates/platform_protocol_services.html`
- Test: `tests/test_platform_admin.py`

**Steps:**
1. 在平台导航增加“协议服务”。
2. 增加查询页面和 CSRF 保护的保存动作。
3. 页面只展示 iPad 874 地址、开放状态和已同步渠道数量。
4. 运行平台页面定向测试。

### Task 4: 收口租户接入与编辑流程

**Files:**
- Modify: `app/services/account_connections/service.py`
- Modify: `app/main.py`
- Modify: `app/templates/channel_connect.html`
- Modify: `tests/test_account_connection_pages.py`
- Modify: `tests/test_multi_channel_pages.py`

**Steps:**
1. 创建平台托管渠道时忽略租户地址并读取启用的平台服务。
2. 租户 iPad 接入页删除协议地址输入。
3. 高级设置保存时保留平台地址，不接受租户表单覆盖。
4. 增加未配置、已停用及伪造地址测试。

### Task 5: 验证、提交和部署

**Steps:**
1. 运行本次模型、平台服务、平台页面和账号接入轻量测试。
2. 运行 `compileall`、迁移升级和 `git diff --check`。
3. 中文提交并推送 `feature/ipad-874-adapter`。
4. SSH 登录 `192.168.2.142`，执行 `git pull`、构建并差异更新 `app`。
5. 验证迁移版本、`healthz`、`readyz`、平台协议服务页和租户 iPad 接入页。
