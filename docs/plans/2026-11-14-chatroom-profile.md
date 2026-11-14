# 群资料公告与成员顺序 Implementation Plan

> **For Claude:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task.

**Goal:** 在现有群资料页展示可读取的公告及置顶能力说明，让群主和管理员稳定排在成员列表前面。

**Architecture:** 复用单群同步任务和详情路由，公告规范化存入本群 extra_json 的唯一快照。共用服务处理公告文本、读取状态和成员顺序，全部入口沿用租户隔离、权限与 CSRF；不添加旧结构兼容分支。

**Tech Stack:** Python、FastAPI、SQLAlchemy、Jinja2、现有 CSS 和 pytest；不引入新依赖。

---

## Task 1：记录已确认设计

**Files:** 新增本计划及 `docs/plans/2026-11-14-chatroom-profile-design.md`。

1. 记录用户确认的页面、微桥缓存读取边界、存储和失败处理。
2. 仅提交两份文档，中文 commit：`文档：确认群资料公告与成员排序方案`。

## Task 2：公告快照与成员排序

**Files:** 新增 `app/services/chatroom_profile.py`、`tests/test_chatroom_profile.py`。

1. 先写定向用例，断言成员 ID 顺序为群主、管理员、普通成员、退群成员，覆盖同角色发言时间与群主 ID。
2. 增加公告结果的正文提取、摘要、未缓存、错误保留、租户/渠道、错误群和乱序用例。
3. 实现有中文注释的排序、HTML 转纯文本、快照更新及资料视图函数，保持单一存储路径。
4. 执行 `.venv/bin/python -m pytest tests/test_chatroom_profile.py -q`，预期全部通过。

## Task 3：接入同步与详情

**Files:** 修改 `app/main.py`、`app/services/wechat_webridge/service.py`、`app/services/wechat_webridge/chatrooms.py`、`app/services/workwechat_chatrooms.py`、`app/services/wechat_ipad/service.py`。

1. 共用构建函数与 iPad 构建使用统一排序，角色展示和计数与群主 ID 一致；删除微桥最后发言覆盖排序。
2. 单群同步的微桥分支入队 `room_members` 与 `get_group_notice`；各分支使用经过白名单校验的返回标签。
3. 命令回报复用已有终态处理，公告失败或非法结果不能覆盖既有成功内容，不触发 HTTP 500。
4. 所有支持的群资料详情构建统一公告与置顶能力视图，其他渠道按实际接入状态说明。

## Task 4：资料页与页面测试

**Files:** 修改 `app/templates/chatroom_detail.html`、`app/static/css/style.css`，新增 `tests/test_chatroom_profile_pages.py`，按必要性更新已有同步断言。

1. 资料页按基本资料、公告、置顶说明顺序输出，公告文本安全转义、保留换行并适配长文本。
2. 同步表单提交当前标签，更新微桥按钮与提示文字；成员角色使用轻量徽标。
3. 用真实认证/临时数据库验证同步两条任务、资料标签保留、权限和 CSRF。
4. 验证成功、未缓存、失败保留等展示状态与脚本内容转义，检查 1440/390px 页面。

## Task 5：验证、记录与提交

**Files:** 更新 `docs/ui/soft-ui-page-checklist.md` 的实际验证记录。

1. 分文件运行本次新测试以及直接受影响的微桥群健康/目录、企微与咕嘎群详情测试，避免不相关的全量回归。
2. 执行 Python AST、Jinja 加载和 `git diff --check`，修复本次引入的问题。
3. 记录实际测试、视觉检查、真机验证边界与历史失败。
4. 精确暂存本次文件，以中文说明功能和实际验证后 commit；保留原有未跟踪群功能文件，不部署或提交那些文件。

本会话按用户「直接做」授权顺序执行，不另行询问执行会话或委派方式。部署不属于本次确认的编码步骤。

## Task 6：用户追加并确认的邀请树

**Files:** 新增 `app/services/chatroom_invitations.py`、`app/static/js/chatroom-invitations.js`、`tests/test_chatroom_invitations.py`；修改成员资料适配器、`app/services/channel_events.py`、`app/main.py`、群详情模板与样式，扩展本轮页面测试。

1. 按 `2026-11-14-chatroom-invitations-design.md` 的已确认边界实现单一邀请关系快照，保留旧快照但不新增旧字段兼容读取路径。
2. 企微/iPad 成员同步使用明确邀请人字段；入站系统消息与历史补查共用相同的 ID 解析与保存函数。
3. 成员资料与消息更新合并保留邀请关系，避免一次发言或空资料同步清空关系。
4. 树视图采用迭代构建，按成员 ID 关联，循环只影响树的展开方式，不删除原始证据；只读用户可查看。
5. 执行邀请服务、资料页面、相关渠道目录与操作反馈的定向用例；检查脚本语法及桌面/手机展开操作，记录后提交。
