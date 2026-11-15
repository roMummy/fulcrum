# 欢迎卡片缩略图来源二选一 Implementation Plan

> **For Claude:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task.

**Goal:** 用户直接选择自定义图片 URL 或新成员头像，头像模式无需填写变量，切换和保存不丢失 URL。

**Architecture:** 复用 `ConfigField` 声明下拉选项和条件可见关系，统一模板、前端脚本和配置存储消费声明。`member_change` 按显式来源确定缩略图，沿用身份目录及 `link_reply`。

**Tech Stack:** Python、SQLAlchemy、Jinja、原生 JavaScript/CSS、pytest、Playwright。

---

### Task 1: 定向测试

**Files:** `tests/test_group_features.py`、`tests/test_group_feature_render_golden.py`。

1. 将头像测试改为显式 `card_thumb_source=member_avatar`，保留头像归属和单次模板替换验证。
2. 用不同 URL、真实头像与缺失头像检查只按来源发送，缺头像不改用自定义 URL；验证非法选项回到声明默认值。
3. 真实表单分别保存两个来源，检查 URL 保留、选中值与初始可见性；下拉基准只使用合法选项。

### Task 2: 声明与页面

**Files:** `app/services/config_fields.py`、`app/services/group_features/store.py`、`app/templates/_config_card.html`、`app/static/js/group-features.js`、`app/static/css/style.css`。

1. 给通用字段添加 `show_when=(依赖字段, 选中值)`，渲染上下文携带 options、可见性与依赖名称。
2. 下拉框按声明渲染；URL 字段初始 hidden，由 change 同步。隐藏不禁用，保证 FormData 保存原地址。
3. 下拉样式与现有输入框一致，hidden 规则防止 grid 样式覆盖浏览器默认隐藏。
4. select 只保存声明内的值，非法值按当前存储策略使用默认值。

### Task 3: 发送与说明

**Files:** `app/services/group_features/member_change.py`、`docs/wechat-webridge-directory.md`、`tests/golden/group_features_tab.html`。

1. 增加来源声明，URL 仅在自定义模式显示；明确头像缺失边界。
2. 头像来源或生效模板用到头像时才批量读取资料。
3. 根据来源确定缩略图后调用原有 `link_reply`，不新增发送接口或后台降级。

### Task 4: 验证、提交与部署

1. 执行相关 pytest、JavaScript/Python 语法及差异检查，更新本轮基准。
2. 真实 FastAPI 页面配合临时库和 Chromium 检查 1440/390px 下切换、保存、刷新与 URL 保留。
3. 只暂存本轮文件，中文 commit 如实记录测试范围，不带用户未跟踪草稿。
4. 读取服务器档案和本地凭据，先连接 201 核对 `66158ca` 与健康，再备份、同步、离线构建并仅重建 app。检查运行文件、HTTP、四容器和私有插件，追加维护记录。
