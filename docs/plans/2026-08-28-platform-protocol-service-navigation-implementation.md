# Platform Protocol Service Navigation Implementation Plan

> **For Claude:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task.

**Goal:** 将协议服务从平台顶级导航迁入系统运维二级导航，并保持现有平台配置能力不变。

**Architecture:** 新增统一的平台导航构造模块，平台各 Router 只传入当前模块编码。协议服务页面和保存动作迁入平台运维 Router，继续使用现有 URL 与服务层，不保留双路由或跳转兼容。

**Tech Stack:** Python 3.10、FastAPI、Jinja2、Pytest

---

### Task 1: 统一平台导航构造

**Files:**
- Create: `app/web/platform_navigation.py`
- Modify: `app/web/platform_admin.py`
- Modify: `app/web/platform_billing.py`
- Modify: `app/web/platform_operations.py`

**Steps:**
1. 定义唯一的平台顶级导航，删除“协议服务”顶级项。
2. 定义系统运维二级导航：运行监控、协议服务、全局日志。
3. 三个平台 Router 改为调用统一构造函数。

### Task 2: 迁移协议服务页面归属

**Files:**
- Modify: `app/web/platform_admin.py`
- Modify: `app/web/platform_operations.py`

**Steps:**
1. 从平台管理 Router 删除协议服务导入、错误转换和路由。
2. 在平台运维 Router 注册现有查询与保存路由。
3. 协议服务页面激活顶级“系统运维”和二级“协议服务”。

### Task 3: 更新定向页面测试

**Files:**
- Modify: `tests/test_platform_protocol_services.py`
- Modify: `tests/test_platform_operations.py`

**Steps:**
1. 协议服务页面测试改为注入平台运维 Router。
2. 断言协议服务不在顶级导航，系统运维顶级入口激活。
3. 断言三个运维页面均包含完整二级导航和正确激活项。
4. 运行相关 Pytest、`compileall` 和 `git diff --check`。

### Task 4: 提交与部署

**Steps:**
1. 使用中文提交说明记录导航层级、路由归属和实际测试。
2. 推送 `feature/ipad-874-adapter`。
3. SSH 登录 `192.168.2.142`，拉取后仅重建 app 服务。
4. 验证容器、`healthz`、`readyz` 以及登录后的运维二级导航。
