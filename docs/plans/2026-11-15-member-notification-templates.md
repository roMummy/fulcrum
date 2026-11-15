# 群成员变更通知模板 Implementation Plan

> **For Claude:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task.

**Goal:** 每个群独立配置成员加入、退出、被移出和改昵称通知，支持昵称及变更上下文变量。

**Architecture:** 在既有 `member_change` 声明五个配置项，复用群级存储和通用配置卡片；通知生成只消费本群配置模板。通知和欢迎卡片统一单次变量渲染，不新增硬编码兼容路径或重复发送入口。

**Tech Stack:** Python、SQLAlchemy、Jinja、pytest、Playwright、Docker Compose。

---

### Task 1: 测试通知与群级隔离

**Files:** `tests/test_group_features.py`、`tests/test_webridge_member_events.py`。

1. 从声明字段读取默认配置，更新通知纯函数调用，默认加入、减少和改昵称输出保持一致。
2. 四种事件分别验证自定义模板、昵称、群名、时间及旧/新群昵称，未知变量与成员昵称不递归替换。
3. 真实分发验证不同群模板隔离、多人只生成一条通知、空标题、空正文不发送文字但欢迎卡片仍可发送。

### Task 2: 实现唯一模板生成路径

**Files:** `app/services/group_features/member_change.py`。

1. 新增 `notify_title`、`join_template`、`leave_template`、`removed_template`、`rename_template`，默认值来自当前产品文案，声明变量提示及开关依赖。
2. `handle` 显式传入本群配置和群名；通知纯函数按事件选择模板，逐人渲染后合并一次标题。
3. 用相同单次变量替换服务通知和卡片，不再保留旧的写死文案生成分支。

### Task 3: 页面、说明和轻量验证

**Files:** `tests/test_group_feature_render_golden.py`、`tests/golden/group_features_tab.html`、`docs/wechat-webridge-directory.md`。

1. 真实群功能表单保存全部模板，刷新后保留花括号和换行，并验证另一群不受影响。
2. 更新只涉及本轮小节的页面基准和使用说明。
3. 运行群功能、成员事件、页面保存和相关目录定向测试；浏览器验证 1440/390px 文案输入、保存刷新与已存在的头像来源配置保留，执行语法及差异检查。
4. 仅暂存本轮文件并生成中文 commit，如实记录检查范围，不包含原有未跟踪草稿。

### Task 4: 发布到 201

1. 读取维护档案和凭据，先用密码 SSH 核对 201 原版本 `bebafcd`、四容器和 HTTP 就绪，再推内网并快进同步。
2. 备份数据库、私有插件和旧镜像，从精确 Git archive 离线构建，只重建 app，无数据库迁移。
3. 验证实际运行文件、模板生成、HTTP、服务健康和私有插件，记录维护结果。生产群配置不自动改写，不下发群消息或同步任务。
