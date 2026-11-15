# 新成员欢迎卡片头像变量 Implementation Plan

> **For Claude:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task.

**Goal:** 在新成员欢迎卡片的缩略图地址中支持 `{avatar_url}`，传递该成员已同步的真实头像地址。

**Architecture:** 在现有 `member_change.handle()` 中按需通过 `IdentityDirectory` 批量加载当前群的新成员头像。卡片统一通过单次占位符替换生成，继续使用 `link_reply` 及微桥 `send_link_card`，保持单一发送路径。

**Tech Stack:** Python、SQLAlchemy、Jinja、pytest。

---

### Task 1: 增加定向测试

**Files:**
- Modify: `tests/test_group_features.py`
- Modify: `tests/test_wechat_webridge_capabilities.py`
- Modify: `tests/test_group_feature_render_golden.py`

1. 在群功能测试夹具增加真实好友表，构造本群/同渠道好友及其他群/渠道的头像资料，验证头像属于正确新成员。
2. 验证多位新成员头像分别替换、空头像或非法头像不阻断卡片、现有自定义 URL 原样发送、配置中未知花括号与成员昵称不被二次替换。
3. 在已有微桥映射测试中断言 `thumb_url` 被原样透传；验证页面说明包含变量并允许保存、重新读取。

### Task 2: 实现头像变量

**Files:**
- Modify: `app/services/group_features/member_change.py`
- Modify: `docs/wechat-webridge-directory.md`
- Modify: `tests/golden/group_features_tab.html`

1. 更新标题、描述、点击网址和缩略图地址的字段提示。
2. 仅在卡片开启且包含头像变量时，批量读取前 `MAX_WELCOME_CARDS` 位新成员的身份目录。
3. 使用单次替换支持 nickname、now、avatar_url，未知占位符保留；头像地址单独校验，缺失或非法时输出无缩略图卡片。
4. 补充使用说明及头像尚未同步的边界，更新页面基准中的对应提示。

### Task 3: 验证并提交

1. 执行与群功能、身份目录、微桥成员事件和链接映射直接相关的定向 pytest。
2. 执行 Python 语法与 `git diff --check`，仅暂存本轮文件，中文提交记录实际验证和未运行全量回归。

### Task 4: 沿用既有 201 部署流程

1. 先通过 `.env.deploy` 的密码 SSH 核对 201 主机及原版本 `ad1afbb`，读取并更新服务器维护档案。
2. 推送内网 main，保留数据库、私有插件和旧镜像备份，从本次提交归档离线构建，仅重建 app。
3. 本轮不变更数据库结构，不改变用户已有欢迎卡片配置，也不向真实群发送验收消息。核对镜像文件、HTTP 就绪、四容器及私有插件状态。

## 实际验证

- 先执行新增定向测试，确认现有代码将 `{avatar_url}` 当成 URL 导致卡片校验失败，7 个相关用例按预期失败；再修改唯一的欢迎卡片生成路径。
- `tests/test_group_features.py`、`tests/test_webridge_member_events.py`、`tests/test_identity_directory.py` 和微桥链接映射定向用例共 27 项通过。
- `tests/test_webridge_member_directory.py` 25 项通过，群功能真实页面渲染/保存用例 3 项通过，总计 55 项。
- Python 语法和 `git diff --check` 通过；未运行全量回归，未使用真实微信发送欢迎卡片验收。
