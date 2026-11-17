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
- 对现有 Telegram、咕嘎及 Windows 适配器，将其已有原始时间映射至 `details.timestamp`；保留微桥、iPad、企微 Hook／Provider 已有的原始时间路径。
- 新增 `tests/test_inbound_message_age.py`：先覆盖 599／600／601 秒、毫秒与 ISO、缺失／无效时间，以及真实入库和队列记录断言；更新原时间解析测试到唯一公共入口。

### 任务 3：执行边界拦截

- 修改 `app/services/channel_events.py`，在入库完成与指令入队之间返回带 stale_message 原因的存档结果。
- 修改 `app/services/async_workers.py`，在群功能和插件执行前再次检查，保存过期原因，不产生执行统计。
- 修改 `app/services/ai_dispatch.py`、`app/services/ai_intent_worker.py`，在准备模型请求和最终下发前拒绝过期消息；验证没有调用模型、下发回复或执行改写命令。

### 任务 4：轻量验证与中文提交

顺序执行新增消息时效测试、插件统计测试、直接相关的 AI／消息目录／自发消息检查，浏览器测试使用单进程，避免共享应用夹具监听端口冲突。运行变更 Python AST、`node --check app/static/js/plugin-usage.js`、`git diff --check`。记录实际结果与范围外旧失败后中文提交，不纳入用户草稿；本轮不自动发布。

## 实施结果与实际验证

- 企业和平台共用批量统计返回独立于时间筛选的 `last_activity_at`，只统计真实命中或执行，排除未命中扫描和未来日志。前端稳定移动既有卡片节点，活动时间相同或无活动时保留初始顺序；底部时间点读数未改动。
- 新增公共原始时间与时效模块，原有资料／免打扰时间解析统一迁入，移除旧私有函数并更新全部调用和对应测试，不保留别名。微桥、iPad、企微 Hook／Provider、Windows、咕嘎和 Telegram 的现行时间路径均有检查；没有为已废弃的模拟渠道新增兼容。
- 超过 600 秒的消息完成原始事件和正文存档后返回 `storage_only / skipped / stale_message`，不创建插件指令任务。插件消费、AI 入队、AI 准备和结果下发复用同一判断，积压及重试不刷新原始时间；恰好 600 秒允许，支持小数秒，缺失或无效时间不推断为历史消息。定时和手动执行没有套用入站时效规则。
- 本轮 165 个不同的直接相关检查通过：插件统计／API 31 项，最终消息时效与现行渠道映射 35 项，企业／平台 × 桌面／手机浏览器 4 项，AI、自发消息、群开关、成员和消息目录等回归 95 项。最终时效测试又独立复验 35 项通过；浏览器无脚本错误和页面横向溢出，切换范围与刷新后排序正确。
- 相关回归有一项原有失败：`test_system_events_do_not_refresh_directory_or_count_as_speech[system]` 使用裸会话，未建立可信租户上下文，调用邀请资料记录时被 `require_tenant_id` 拒绝。在精确原始提交 `e78ac57` 的临时副本中复现同一异常，未扩大修复历史夹具或削弱产品租户校验。
- 16 个变更 Python 文件 AST、JavaScript `node --check` 和 `git diff --check` 通过。没有执行全量测试，没有新增依赖或数据库迁移，没有调用真实微信或模型，也没有部署。截图和验证 JSON 保存在 `.run/plugin-activity-message-age/`。
