# Kitesurf 公共截图 Implementation Plan

> **For Claude:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task.
> 本会话按用户已确认范围直接执行；不存在上述外部技能时遵守本仓库 AGENTS.md，不创建子代理或再次询问执行方式。

**Goal:** 提供本地 Kitesurf 支撑的项目公共截图服务与鉴权接口，不新增截图插件。

**Architecture:** Python 公共服务是唯一截图路径，HTTP 接口鉴权后调用同一服务。每次独立 CDP 连接与浏览器上下文，输出受限图片字节；已有出站流程继续负责图片发送。

**Tech Stack:** FastAPI、Pydantic、websockets 16 异步客户端、Pillow、Kitesurf CDP、Docker Compose；对插件提供同步公共函数。

---

### Task 1：截图请求、错误与 CDP 服务

**Files:** 新建 `app/services/web_screenshots.py`、`tests/test_web_screenshots.py`；修改 `app/core/settings.py`。

1. 添加 URL/HTML 二选一、宽高、格式、整页、质量、等待时间请求模型；边界失败必须明确拒绝。
2. 添加全局引擎配置与进程内并发额度；要求 `require_tenant_id()`，不信任调用载荷里的租户编号。
3. 基于官方 CDP 新建上下文/target/session，设置尺寸、加载内容、计算整页范围并截图；解析 ID 对应的响应，跳过事件，设置整体截止时间。
4. 校验图片字节与实际尺寸，处理占位、引擎报错、超大图和断连；在 finally 回收上下文、连接及并发额度。
5. 用受控假 CDP 和图片字节执行定向单元/协议测试。

### Task 2：鉴权 HTTP 接口

**Files:** 修改 `app/api/tool_api.py`；新建 `tests/test_screenshot_api.py`。

1. 新增 `POST /api/tool/screenshots`，沿用 `authenticate_tool_tenant()`。
2. 在线程池中复用公共服务；响应原始图片及尺寸/耗时头，关闭缓存；业务错误映射为 400/413/429/502/503/504。
3. 验证错误密钥、缺失租户、停用租户、额外字段、有效图片与错误状态，不启动业务后台 Worker。

### Task 3：本地部署配置与开发文档

**Files:** 新建 `deploy/kitesurf/Dockerfile`、`deploy/kitesurf/docker-compose.yml`、`deploy/kitesurf/README.md`；修改 `.env.example`、`docs/plugin-development.md`、`.github/ci-test-files.txt`。

1. 固定上游版本，通过官方 Bazel 构建，运行本地 Wrangler；独立容器、GC、健康检查、回环端口与内部网络配置。
2. 文档写明本地构建依赖、启动与项目连接配置、资源限制和官方实验性限制；不给出无法核实的预构建镜像。
3. 提供 Python URL/HTML、图片回复和 HTTP 调用示例，不新增插件目录。
4. 执行本次测试、Python 语法、差异空白和 Compose 配置检查，中文 commit 如实记录真实引擎验证边界。
