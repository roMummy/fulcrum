# 插件活动排序与消息时效 Implementation Plan

> 按用户明确要求在当前会话执行，保留用户原有未跟踪草稿。

**Goal:** 企业和平台插件卡片按最近活动排序，超过十分钟的入站消息只存档，不触发指令。

**Architecture:** 扩展现有两次批量聚合得到独立于筛选范围的活动时间，共用前端移动原卡片。公共入站时间策略统一解析和判断，在消息入队、插件队列消费、AI 准备与下发边界复用。

**Tech Stack:** FastAPI、SQLAlchemy、Jinja、JavaScript、pytest、Playwright。

---

### 任务 1：插件活动排序

- 修改 `app/services/plugin_usage.py`：在最近调用聚合中同时返回 matched 或 attempted 的最近活动，排除未来时间和未命中扫描；保留当前范围的最近调用展示。
- 修改 `app/static/js/plugin-usage.js`：批量加载成功后按活动时间降序移动卡片，时间相同保留原顺序；排序失败沿用统计加载错误处理，不创建另一套图表读数。
- 扩展 `tests/test_plugin_usage.py` 和 `tests/test_plugin_usage_browser.py`：先运行新增用例观察失败，再实现；验证跨范围、跨企业及手机／桌面两类页面。

### 任务 2：原始时间与过期规则

- 新增 `app/services/inbound_policy.py`，统一现有时间解析和过期摘要。将 `channel_events.py` 及微桥目录中的时间调用改用公共模块，移除旧私有解析函数，不留别名。
- 对现有 Telegram、咕嘎及通用适配器，将其已有原始时间映射至 `details.timestamp`；保留微桥、iPad、企微和 Windows 已有的标准时间路径。
- 新增 `tests/test_inbound_message_age.py`：先覆盖 599／600／601 秒、毫秒与 ISO、缺失／无效时间，以及真实入库和队列记录断言；更新原时间解析测试到唯一公共入口。

### 任务 3：执行边界拦截

- 修改 `app/services/channel_events.py`，在入库完成与指令入队之间返回带 stale_message 原因的存档结果。
- 修改 `app/services/async_workers.py`，在群功能和插件执行前再次检查，保存过期原因，不产生执行统计。
- 修改 `app/services/ai_dispatch.py`、`app/services/ai_intent_worker.py`，在准备模型请求和最终下发前拒绝过期消息；验证没有调用模型、下发回复或执行改写命令。

### 任务 4：轻量验证与中文提交

顺序执行新增消息时效测试、插件统计测试、直接相关的 AI／消息目录／自发消息检查，浏览器测试使用单进程，避免共享应用夹具监听端口冲突。运行变更 Python AST、`node --check app/static/js/plugin-usage.js`、`git diff --check`。记录实际结果与范围外旧失败后中文提交，不纳入用户草稿；本轮不自动发布。
