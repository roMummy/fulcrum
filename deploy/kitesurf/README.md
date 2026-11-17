# 本地 Kitesurf 截图引擎

Fulcrum 把 Kitesurf 作为独立基础服务，通过 CDP 调用。此目录交付本地构建、运行及应用接入配置；本轮没有新增截图命令插件。

依据官方项目 `https://github.com/cloudflare/kitesurf`，固定提交 `39feadfb9a3e6c14b40cf170a559ce417d0fce5d`。本地使用 Wrangler/Workerd，不需要 Cloudflare 账号；只有发布 Cloudflare 云端才需要 Workers Paid plan。上游仍为实验性浏览器，复杂网站的渲染结果需要以实际验证为准。

## 独立启动

在仓库根目录执行：

```bash
docker compose -f deploy/kitesurf/docker-compose.yml up -d --build
curl --fail http://127.0.0.1:8787/health
```

首次构建会下载 Node 依赖、Bazel、Rust 和 WASM 工具链，耗时和网络流量明显多于普通 Python 应用构建。Dockerfile 固定 pnpm 和 Bazelisk 版本，由上游 `.bazelversion` 选择 Bazel。运行阶段不再构建 Rust，也不启动 Chrome。

镜像使用 `wrangler.fulcrum.jsonc`：保留上游本地引擎需要的动态 Worker 和字体资源，去掉开发测试用的 fixtures 绑定与本机 tracing 地址，不要求额外启动测试服务。升级固定源码版本时，应一并核对这份运行配置。

可用 `KITESURF_PORT=8799` 更改宿主机回环端口，容器内始终为 8787。请保留回环绑定：引擎本身没有认证，所有站内 HTTP 请求应经过 Fulcrum 的工具鉴权接口。运行配置保留上游私网拦截，避免网页重定向和 HTML 子资源访问内网。不要开启 `KITESURF_ALLOW_PRIVATE_NETWORK`。

已启用官方建议的 `--expose-gc`；单容器内存上限 4GB、CPU 上限 2 核。Fulcrum 并发额度按应用进程计算，多 Worker 部署要结合总并发调整引擎资源。

## Fulcrum 接入

宿主机直接运行 Python 时，在项目未提交的环境文件设置：

```dotenv
KITESURF_WS_URL=ws://127.0.0.1:8787/devtools/browser
KITESURF_TIMEOUT_SECONDS=45
KITESURF_MAX_CONCURRENCY=2
```

Fulcrum 运行在 Docker 时，先启动上面的引擎，再将应用加入相同的内部网络。在仓库根目录沿用正常部署环境执行：

```bash
docker compose -f docker-compose.yml -f deploy/kitesurf/app-compose.yml up -d --build app
```

此覆盖文件只为 app 增加截图网络和 `ws://kitesurf:8787/devtools/browser`，数据库仍在原网络。合并 Compose 时继续使用项目正常的数据库环境变量和 `.env.docker`，无需在此目录另存凭据。

## 接口与故障

- Python：`app.services.web_screenshots.capture_screenshot(ScreenshotRequest(...))`。要求可信租户作用域，返回图片字节、格式、宽高及耗时。
- HTTP：`POST /api/tool/screenshots`，使用 `X-API-Key` 或 Bearer 工具密钥及 `X-Tenant-Code`。完整示例见 `docs/plugin-development.md`。
- `screenshot_not_configured`：应用尚未设置全局连接地址。
- `screenshot_busy`：当前应用进程的并发额度已满，稍后再调用。
- `screenshot_timeout`、`screenshot_unavailable`：检查引擎健康、网络以及应用配置，服务不会切换到其他截图实现。
- `screenshot_too_large`：整页超过宽度、8192 高度或 16M 像素上限，改用视口截图或缩小内容。
- 站内需要截图的登录页面可先渲染为 HTML，再通过 HTML 模式提交；接口不转发后台登录 Cookie，不开放任意凭证或请求头参数。

## 本轮验证边界

公共服务与接口通过受控 CDP、真实本机 WebSocket 传输、PNG/JPEG 校验及鉴权测试。当前工作环境没有运行中的 Docker daemon，因此未构建或启动真实 Kitesurf，也未到 201 发布；Compose 校验通过不等于真实浏览器已完成截图验收。
