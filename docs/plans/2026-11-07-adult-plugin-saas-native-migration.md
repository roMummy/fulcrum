# 成人插件 SaaS 原生迁移实施计划

> **For Claude:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task.

**Goal:** 将已确认的六个非 AI 成人业务插件按当前 Multi Bot SaaS 目录插件协议重构，使其在租户绑定、标准消息、媒体、积分、状态和统一调度边界内可独立运行。

**Architecture:** 不复制 A-BOT 运行时，不新增兼容层，不读取 A-BOT Redis、旧数据库、微信客户端对象或绝对路径。每个插件以 `config.toml` + `plugin.py` 为唯一入口，复杂业务拆为插件目录内的纯业务模块，并通过当前平台的 HTTP、回复、消息附件、积分、状态、临时文件和调度能力完成执行。

**Tech Stack:** Python 3、SQLAlchemy、当前 `PluginExecutionRequest/Result` 契约、`PluginHttpClient`、`PluginStateStore`、`PluginMessageReader`、标准回复构造器、统一任务队列、pytest、AST/TOML 迁移校验脚本。

---

## 实施约束

- 所有插件默认关闭，只能在租户机器人绑定级别显式启用。
- 插件不得从请求载荷覆盖 `tenant_id`、`binding_id`、`bot_id`、渠道、会话、消息和发送人等可信身份字段。
- 本地资源只允许来自当前租户绑定授权目录；下载文件只允许进入当前租户临时目录，并在成功入队或失败后清理。
- 外部请求统一使用 `PluginHttpClient`，明确超时、重定向、响应体、重试和内容类型上限；不在日志记录密钥、代理密码、完整敏感 URL 或原始正文。
- 视频、图片和 PDF 必须先做大小、媒体类型和处理结果校验，再交给标准出站队列。
- 成人插件不加入宣传页、公共演示数据、默认套餐描述和平台公共推荐。
- 每个任务完成后执行与本次改动直接相关的轻量测试、`python -m compileall` 或 AST 检查、`git diff --check`，并单独使用中文 commit。
- 不为历史 A-BOT 入口保留别名、跳转、双写或隐式降级。

## Task 1: 更新迁移目录台账和验证基线

**Files:**
- Modify: `scripts/verify_plugin_migration.py`
- Modify: `tests/test_plugin_migration_catalog.py`
- Test: `tests/test_plugin_migration_catalog.py`

**Step 1: 扩展目标集合**

把 `beautyleg`、`video`、`video_man`、`fanhao_search`、`xiuren_image`、`sehuatang_push` 从禁止集合移入 A-BOT 目标集合，并把目标数量、目录数量和输出断言更新为最终 44 个目录（42 个 A-BOT 目标 + 2 个 SaaS 自有目录）。

**Step 2: 增加六个插件的共同契约检查**

在脚本中增加对插件内部模块的 AST 检查：不得导入 A-BOT 运行时、不得出现 `/mnt/nfs_share`、不得声明微信专用发送方法或直接写入任务载荷中的敏感字段。检查结果只输出插件代码和稳定错误文本，不输出配置值。

**Step 3: 运行目录基线测试**

Run: `pytest tests/test_plugin_migration_catalog.py -q`

Expected: 由于六个目录尚未创建，测试应只报告缺失目录；确认断言和错误信息已经指向新的最终目录集合。

**Step 4: 提交台账变更**

```bash
git add scripts/verify_plugin_migration.py tests/test_plugin_migration_catalog.py
git commit -m "校准成人插件迁移目录台账与契约校验"
```

## Task 2: 建立第一批共享媒体和资源边界

**Files:**
- Create: `plugins/_shared/media.py`
- Create: `plugins/_shared/resources.py`
- Test: `tests/test_plugin_media_boundaries.py`

**Step 1: 编写失败测试**

覆盖以下行为：授权目录内的图片可以读取；目录外路径、符号链接越界和不存在文件被拒绝；下载响应必须匹配允许的媒体类型并受字节上限限制；临时文件完成后可清理；视频首帧失败不能生成可发送视频。

**Step 2: 实现最小共享能力**

资源模块只接收平台传入的租户资源根目录和文件标识，不接受绝对路径覆盖；媒体模块提供图片读取、受限二进制下载、视频封面生成、文件大小检查和安全清理函数。实现中写明中文注释，统一抛出插件内部稳定异常。

**Step 3: 运行定向测试**

Run: `pytest tests/test_plugin_media_boundaries.py -q`

Expected: PASS，且不需要安装系统级视频或浏览器依赖。

**Step 4: 提交共享能力**

```bash
git add plugins/_shared/media.py plugins/_shared/resources.py tests/test_plugin_media_boundaries.py
git commit -m "新增插件媒体下载与租户资源边界能力"
```

## Task 3: 迁移第一批 `beautyleg`

**Files:**
- Create: `plugins/beautyleg/config.toml`
- Create: `plugins/beautyleg/plugin.py`
- Create: `plugins/beautyleg/service.py`
- Test: `tests/test_plugin_beautyleg.py`

**Step 1: 定义目录和配置**

声明命令型插件、文本命令匹配、支持 direct/group 和全部当前渠道；配置包括 `enabled`、命令、积分消耗、群频率窗口、资源目录标识、随机结果数和图片大小上限。资源目录只保存平台分配的标识，不保存 `/mnt/nfs_share` 等旧路径。

**Step 2: 编写失败测试**

覆盖命令匹配、未启用、积分不足、群频率限制、随机图片选择、跨租户路径拒绝、空目录和标准图片回复。测试通过临时租户资源目录和假的平台积分/状态边界完成，不调用真实渠道。

**Step 3: 实现插件入口**

`plugin.py` 只负责配置读取、可信上下文校验、动作分派和标准结果；`service.py` 负责从授权资源目录选择图片并调用积分和状态服务。回复使用 `image_reply` 与 `standard_replies`，不出现微信专用发送对象。

**Step 4: 运行定向验证**

Run: `pytest tests/test_plugin_beautyleg.py -q`

Run: `python -m compileall -q plugins/beautyleg`

Expected: PASS；无真实资源目录时只返回稳定业务错误。

**Step 5: 提交第一批图片插件**

```bash
git add plugins/beautyleg tests/test_plugin_beautyleg.py
git commit -m "迁移 beautyleg 为租户原生图片插件"
```

## Task 4: 迁移第一批 `video`

**Files:**
- Create: `plugins/video/config.toml`
- Create: `plugins/video/plugin.py`
- Create: `plugins/video/service.py`
- Test: `tests/test_plugin_video.py`

**Step 1: 定义配置和限制**

配置外部视频地址、请求超时、响应体上限、视频时长上限、积分消耗、频率窗口和临时文件保留秒数。敏感代理凭证只声明在 `secrets.fields`，manifest 默认值必须为空。

**Step 2: 编写失败测试**

覆盖外部响应不是视频、响应过大、下载超时、视频时长超限、封面生成、临时文件清理、积分不足和成功标准视频回复。

**Step 3: 实现下载与回复**

使用 `PluginHttpClient` 下载到租户临时目录，校验 `content-type`、长度和媒体探测结果，生成受限首帧后转为平台标准视频回复；失败时不暴露第三方响应正文、临时路径或代理信息。

**Step 4: 运行定向验证**

Run: `pytest tests/test_plugin_video.py -q`

Run: `python -m compileall -q plugins/video`

Expected: PASS；不要求真实第三方视频站点可用。

**Step 5: 提交视频插件**

```bash
git add plugins/video tests/test_plugin_video.py
git commit -m "迁移 video 为受限租户视频插件"
```

## Task 5: 迁移第一批 `video_man`

**Files:**
- Create: `plugins/video_man/config.toml`
- Create: `plugins/video_man/plugin.py`
- Create: `plugins/video_man/service.py`
- Test: `tests/test_plugin_video_man.py`

**Step 1: 编写失败测试**

覆盖和 `video` 相同的下载边界，并额外覆盖首帧生成失败、视频编码不支持、不同租户临时目录隔离和取消后清理。

**Step 2: 实现插件**

复用 `_shared/media.py` 的下载、探测、首帧和清理能力；插件自身只保存业务配置和结果文案，不复制 `aiohttp`、`aiofiles` 或 A-BOT 客户端调用链。

**Step 3: 运行定向验证**

Run: `pytest tests/test_plugin_video_man.py -q`

Run: `python -m compileall -q plugins/video_man`

Expected: PASS；缺少本机编码器时测试使用可注入的媒体处理器并明确标记真实环境依赖。

**Step 4: 提交第一批完成结果**

```bash
git add plugins/video_man tests/test_plugin_video_man.py
git commit -m "迁移 video_man 为租户原生视频插件"
```

## Task 6: 迁移第二批 `fanhao_search`

**Files:**
- Create: `plugins/fanhao_search/config.toml`
- Create: `plugins/fanhao_search/plugin.py`
- Create: `plugins/fanhao_search/service.py`
- Create: `plugins/fanhao_search/parsers.py`
- Test: `tests/test_plugin_fanhao_search.py`
- Test: `tests/test_plugin_inbound_media_reader.py`（仅在现有附件读取能力缺少验证时修改）

**Step 1: 编写解析和边界测试**

覆盖番号格式识别、JavBus 详情解析、备用站点失败、封面和磁力结果数量上限、引用图片附件归属校验、图片大小/尺寸限制和无结果提示。

**Step 2: 实现文本查询**

使用 `PluginHttpClient` 访问配置中的站点，解析标题、封面、演员和磁力链接，回复使用标准文本、图片和链接卡片；站点地址和代理属于租户配置，密钥只进密文配置。

**Step 3: 实现引用图片查番**

通过当前平台消息/附件读取服务获取引用图片，严格校验租户、机器人、渠道、会话和消息归属，不接受载荷直接传入任意 URL 或本地路径。

**Step 4: 运行定向验证并提交**

Run: `pytest tests/test_plugin_fanhao_search.py -q`

Run: `python -m compileall -q plugins/fanhao_search`

```bash
git add plugins/fanhao_search tests/test_plugin_fanhao_search.py
git commit -m "迁移 fanhao_search 为租户原生查询插件"
```

## Task 7: 迁移第三批 `xiuren_image`

**Files:**
- Create: `plugins/xiuren_image/config.toml`
- Create: `plugins/xiuren_image/plugin.py`
- Create: `plugins/xiuren_image/service.py`
- Create: `plugins/xiuren_image/cache.py`
- Test: `tests/test_plugin_xiuren_image.py`

**Step 1: 编写缓存和租户隔离测试**

覆盖缓存状态仅属于当前租户绑定、空缓存只创建一次更新任务、已有可用缓存不会因单次抓取失败被清空、图片摘要和数量上限、过期文件清理。

**Step 2: 实现租户缓存**

使用 `PluginStateStore` 保存有限图片元数据、摘要、更新时间和抓取游标；文件放入租户授权目录。禁止全局 Redis、跨绑定状态和 `/mnt/nfs_share` 路径。

**Step 3: 实现定时更新与随机回复**

通过统一调度器创建 `refresh` 动作；请求动作只从已有缓存返回，缓存为空时入队一次更新，不在消息处理线程内无限抓取。

**Step 4: 运行定向验证并提交**

Run: `pytest tests/test_plugin_xiuren_image.py -q`

Run: `python -m compileall -q plugins/xiuren_image`

```bash
git add plugins/xiuren_image tests/test_plugin_xiuren_image.py
git commit -m "迁移 xiuren_image 为租户隔离图片缓存插件"
```

## Task 8: 迁移第四批 `sehuatang_push`

**Files:**
- Create: `plugins/sehuatang_push/config.toml`
- Create: `plugins/sehuatang_push/plugin.py`
- Create: `plugins/sehuatang_push/service.py`
- Create: `plugins/sehuatang_push/parsers.py`
- Create: `plugins/sehuatang_push/pdf_renderer.py`
- Test: `tests/test_plugin_sehuatang_push.py`

**Step 1: 先验证运行依赖**

检查当前开发/运行镜像是否已经提供 PDF 渲染器、字体和必要的 HTML 解析依赖；只记录检测结果，不在插件运行时隐式安装系统包，也不回退到 A-BOT 入口。

**Step 2: 编写抓取、PDF 和幂等测试**

覆盖抓取页数/影片数/图片数/并发/间隔/重试限制、详情解析、PDF 大小上限、任务状态持久化、重启后不重复推送、目标会话归属和部分失败重试。

**Step 3: 实现统一计划任务**

使用 `PluginScheduler` 创建按绑定和计划时间幂等的 `crawl_and_push` 动作，状态保存抓取水位、摘要和目标会话；成功生成 PDF 后只入统一文件出站队列。

**Step 4: 运行定向验证并提交**

Run: `pytest tests/test_plugin_sehuatang_push.py -q`

Run: `python -m compileall -q plugins/sehuatang_push`

```bash
git add plugins/sehuatang_push tests/test_plugin_sehuatang_push.py
git commit -m "迁移 sehuatang_push 为幂等定时文件推送插件"
```

## Task 9: 完成六插件目录和运行时集成验证

**Files:**
- Modify: `app/services/plugin_runtime/registry.py`（仅在目录自动发现未覆盖新模块时修改）
- Modify: `app/services/plugin_runtime/scheduler.py`（加入明确的成人插件定时动作白名单）
- Modify: `tests/test_plugin_scheduler.py`
- Modify: `tests/test_plugin_native_boundaries.py`
- Modify: `tests/test_plugin_migration_catalog.py`
- Modify: `scripts/verify_plugin_migration.py`

**Step 1: 检查目录自动发现**

确认六个插件无需额外兼容注册即可被当前目录发现、manifest 校验和绑定配置渲染；只有现有注册器确实要求显式导入时才增加唯一入口，不复制旧注册方式。

**Step 2: 加入定时插件白名单和边界测试**

为 `xiuren_image`、`sehuatang_push` 增加可调度动作，覆盖禁用绑定不入队、任务租户不一致、参数中含敏感字段被拒绝、重复计划返回同一任务和重启恢复。

**Step 3: 执行轻量完整验证**

Run: `pytest tests/test_plugin_migration_catalog.py tests/test_plugin_native_boundaries.py tests/test_plugin_scheduler.py -q`

Run: `python scripts/verify_plugin_migration.py`

Run: `python -m compileall -q plugins app/services/plugin_runtime`

Run: `git diff --check`

Expected: 六个插件全部进入确认目录；无 A-BOT 导入、旧路径、旧入口和兼容分支错误。

**Step 4: 提交集成结果**

```bash
git add app/services/plugin_runtime tests/test_plugin_scheduler.py tests/test_plugin_native_boundaries.py tests/test_plugin_migration_catalog.py scripts/verify_plugin_migration.py
git commit -m "完成成人插件目录与统一运行时集成校验"
```

## Task 10: 交付前检查和范围记录

**Files:**
- Modify: `docs/plans/2026-11-07-adult-plugin-saas-native-migration-design.md`（补充实际完成状态和真实环境限制）
- Create: `docs/reports/2026-11-07-adult-plugin-saas-native-migration-validation.md`

**Step 1: 记录轻量验证结果**

记录每个插件实际执行的测试、语法检查和目录校验；没有执行全量测试时明确写“未执行全量验证”。

**Step 2: 单独记录真实外部依赖**

记录真实第三方站点、代理、PDF 渲染器和渠道发送未在模拟测试中证明的部分；不得把模拟 HTTP 或内存附件测试描述为真实外部验收。

**Step 3: 提交文档**

```bash
git add docs/plans/2026-11-07-adult-plugin-saas-native-migration-design.md docs/reports/2026-11-07-adult-plugin-saas-native-migration-validation.md
git commit -m "补充成人插件迁移验证记录与外部依赖说明"
```

