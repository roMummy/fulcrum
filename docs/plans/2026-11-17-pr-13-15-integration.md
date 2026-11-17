# PR 整合与跨渠道好友聊天 Implementation Plan

> **For Claude:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task.

**Goal:** 按用户确认的审计方案整合 PR #13、#15，修复阻断问题，并让具备私聊能力的现行渠道从好友页发起聊天。

**Architecture:** 在统一渠道目录声明私聊能力，页面和服务端使用同一能力判断。消息撤回按渠道原生编号严格定位，插件默认配置与显式音源保持一致。保留当前业务内容，建立可追溯的合并祖先后正常推送。

**Tech Stack:** Python、FastAPI、SQLAlchemy、Alembic、Jinja、pytest、原生 JavaScript、Git/GitHub。

---

### Task 1：复核并合流代码基线

**Files:** `docs/reviews/2026-11-17-github-pr-13-15-audit.md`；两份 PR 的既有文件。

1. `gh api repos/liu-x-x-lab/fulcrum/pulls/13` 与 `/15` 核对精确提交，fetch 双端主线和 PR。
2. `git diff github/main main` 检查所有业务差异，确认本地保留最新已确认功能。
3. 为不同根历史建立保留当前内容的合流，再正常合并 PR #13 和 #15；合并中文说明保留每个来源，遇到新变化先审查。
4. 从 PR #15 撤销扫码中继 OSError 吞错改动。

### Task 2：修复音乐默认值与测试隔离

**Files:** `plugins/music/{config.toml,plugin.py,service.py,README.md}`、`tests/conftest.py`、`tests/test_music_plugin.py`、新增 `tests/test_test_runtime_isolation.py`。

1. 在真实 `_build_directory_context` 中验证无 provider 的 QQ 绑定仍保持 QQ，修复前运行该回归并确认失败。
2. 清单 runtime/schema、dataclass、loader 和 README 统一 `qqmusic` 默认，删除按 `api_base` 猜测源的分支；显式 netease 保持。
3. 两个子进程加载 conftest，验证各自受控临时环境不被覆盖、默认目录唯一、业务数据库无法进入测试环境；修复前确认固定共享库导致失败。
4. 使用临时目录和明确测试环境作为唯一隔离路径；运行新增隔离测试和音乐相关节点，通过后中文提交。

### Task 3：撤回精确定位与通知存档

**Files:** `app/services/message_actions.py`、`app/services/channel_events.py`、`tests/test_message_actions.py`。

1. 增加 server 编号定位第一条同文消息、编号歧义、未知编号、无编号的回归；修复前运行并确认误撤问题。
2. 撤回只在同账号会话中比较确定编号，明确区别 server/local 编号，排除同文猜测；必须唯一命中才修改原记录。
3. 未知撤回通知作为系统消息存档，不伪造对方发言，不触发插件/AI。
4. 运行 guga 引用、撤回和状态相关节点，中文提交。

### Task 4：统一好友发起聊天能力

**Files:** `app/services/channel_catalog.py`、`app/services/directory_pages.py`、`app/main.py`、`app/templates/contact_directory.html`、`tests/test_message_actions.py`、`tests/test_directory_pages.py`。

1. 参数化验证六个正式渠道的创建/复用，补充跨租户、只读角色、无效 CSRF、失效好友与删除渠道边界；修复前确认非咕嘎入口失败。
2. 使用 `channel_supports(channel.channel_type, "direct_chat")` 共享能力，在声明中标记实际支持渠道。使用同租户/机器人/渠道/外部 ID 的 direct 会话查询，保留角色、CSRF 校验。
3. 列表为有效好友显示统一入口；支持已有会话“聊天记录”，点击只创建会话不调用发送适配器。
4. 定向运行好友入口和目录测试，通过后中文提交。

### Task 5：修复已确认 CI 基线问题并回归

**Files:** `tests/test_chatroom_scope_actions.py`、`tests/test_webridge_message_directory.py`、`tests/test_plugin_scheduler.py`、`app/templates/{_chatroom_invitations,guess_image_bank}.html`、`app/static/css/style.css`。

1. 使用实际路由权限验证替换历史源码计数；给测试会话补可信租户上下文，不放宽生产校验。
2. 折叠元素采用共享 disclosure 图标样式，补齐使用中的 arrow-right 图标；保留已确认的页面结构。
3. 固定调度测试业务时间，并覆盖周日任务，消除运行日期影响。
4. 运行上述文件、历史排序/迁移、旧消息策略、新人头像等待、群插件设置、统计排序等直接关联检查；耗时文件使用具体节点。
5. `git diff --check` 和变更 Python AST/浏览器脚本检查；记录真实结果并中文提交。

### Task 6：同步并复核合并结果

**Files:** `docs/reviews/2026-11-17-github-pr-13-15-audit.md`。

1. 记录本轮 R4 修订、修复、实际验证及限制；保留原审计时点记录。
2. 推送前再次 fetch，确认两端没有未知新变更；使用普通 push 同步主线。
3. GitHub API 复核两份 PR 的 `merged` 和合并提交，检查新一轮 CI；如有失败按根因处理，不伪报绿灯。
4. 给出提交、真实 PR 状态和验证结果。本轮无部署操作。
