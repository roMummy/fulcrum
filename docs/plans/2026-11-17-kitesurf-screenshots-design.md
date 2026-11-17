# Kitesurf 项目公共截图能力设计

日期：2026-11-17。用户确认本轮只做公共截图服务与接口，不新增截图插件。交付项目代码和本地部署配置，不执行远程发布。

## 已确认的产品范围

- 项目统一配置 Kitesurf，由所有插件和站内功能复用同一 Python 服务；HTTP 调用也进入同一业务路径。
- 输入为 URL、HTML 二选一；支持视口截图、整页截图、PNG 和 JPEG。
- 统一处理尺寸、图片大小、超时、并发、错误及浏览器会话释放，不向插件暴露浏览器连接和页面对象。
- 截图接口只返回图片字节；插件通过已有图片回复及出站媒体流程发送，不增加另一套文件发布和租户存储规则。

## 架构与接口

`app/services/web_screenshots.py` 定义受限请求、图片结果、稳定错误和同步 `capture_screenshot()`。插件运行在可信租户作用域内，直接调用这个公共函数，不经本机 HTTP 绕行。

`POST /api/tool/screenshots` 复用项目现有工具密钥和 `X-Tenant-Code` 鉴权，认证成功后建立租户作用域，并在线程池调用公共服务。成功响应是 `image/png` 或 `image/jpeg`，附尺寸、耗时信息；失败返回稳定错误码，不返回 CDP 原文、页面 HTML、服务地址或凭证。

全局环境配置使用 `KITESURF_WS_URL`、`KITESURF_TIMEOUT_SECONDS`、`KITESURF_MAX_CONCURRENCY`。未配置服务地址时明确返回未配置错误。服务端地址只由部署环境提供，请求体不能指定截图引擎、凭证或租户。并发额度满时立即返回繁忙，不无限排队。

## 唯一截图流程

1. 校验可信租户上下文与输入边界，URL 限制为无凭证的公网 HTTP/HTTPS；HTML 限制长度。
2. 获取进程内并发额度，以单次调用总截止时间连接全局 Kitesurf CDP。
3. 每次新建浏览器上下文、页面和 CDP 会话，Cookie 和页面状态不在租户或调用间共享。
4. 设置视口，URL 通过 `Page.navigate` 加载，HTML 通过 `Page.setDocumentContent` 加载；检查响应内错误，等待可选短暂渲染间隔。
5. 整页通过布局尺寸计算明确 clip，拒绝超过高度和总像素上限的内容，不悄悄裁掉尾部。PNG/JPEG 数据经过 Base64、文件头和实际尺寸校验，拒绝透明占位图冒充成功。
6. 无论成功、超时或引擎失败，都释放浏览器上下文、关闭连接并归还额度。失败不切换到本地 Chromium 或其他截图提供方。

## 本地运行

官方项目为 `https://github.com/cloudflare/kitesurf`，使用 CDP，而不是假设存在 `/screenshot` REST API。依据 2026-11-17 核对的提交 `39feadfb9a3e6c14b40cf170a559ce417d0fce5d`。

官方支持 `wrangler dev` 本地运行；部署 Cloudflare 才要求 Workers Paid plan。本地运行配置放在 `deploy/kitesurf/`，独立构建并守护，固定源码版本，开启官方建议的 GC 参数。CDP 仅开放到回环/内部 Docker 网络，不公开暴露。引擎的私网访问开关保持关闭，以覆盖重定向和 HTML 子资源的出站边界。

本次不迁移签到、新闻、Markdown 出图，也不新增命令插件；公共服务文档给出 URL、HTML 与 `image_reply` 的接入示例，后续插件无需再实现 CDP。

## 验证

使用本机假 CDP 服务验证 URL/HTML 两条真实协议序列、交错事件、整页 clip、PNG/JPEG、异常响应、超时、并发回收与每次独立上下文。HTTP 验证工具密钥、租户存在/停用、请求字段边界、图片响应和错误状态。补充 Python AST、配置检查和部署说明。真实 Kitesurf 的构建/截图若环境无法执行，必须明确记录，不能把假服务验证称为真实引擎验证。
