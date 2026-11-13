# 插件功能卡片改版 Implementation Plan

> **For Claude:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task.

**Goal:** 放大企业与平台插件页卡片，清楚展示版本号与完整功能介绍。

**Architecture:** 继续使用 FastAPI、Jinja2 和现有插件目录字段，调整两份页面模板并共用卡片基础样式。搜索、启停、配置、可见范围与版本弹窗沿用现有入口，移除被替代的紧凑卡片样式。用户已要求直接实施，本次在当前工作区顺序完成，保留已有未跟踪文件。

**Tech Stack:** Python 3、FastAPI、Jinja2、CSS Grid、pytest、Playwright。

---

### Task 1: 统一卡片内容与样式

**Files:**
- Modify: `app/templates/plugins.html`
- Modify: `app/templates/platform_plugins.html`
- Modify: `app/static/css/style.css`
- Modify: `app/main.py`（仅更新插件页说明注释）

1. 企业卡片改为“标识与名称 → 版本徽标 → 功能介绍 → 开发者和操作”的纵向结构，实际版本来自 `plugin.version`。
2. 平台卡片采用相同的信息结构，保留所有 `data-pp-*` 属性、开关和弹窗按钮；搜索文本增加 `plugin.description`，占位提示同步更新。
3. 两页共用三列网格、20px 间距与内边距、48px 标识、可换行标题、版本徽标和自然展开的说明。使用媒体查询切换为两列、一列。
4. 保留清单错误、警告、平台停用及目录缺失状态；中文注释说明共享样式、长文本处理和数据来源。
5. 更新 `app/main.py` 中已失效的“紧凑排布”注释，不改变接口实现。

### Task 2: 定向验证和浏览器预览

**Files:**
- Modify: `tests/test_plugin_detail_page.py`（更新现有列表展示断言和失效注释）
- Modify: `tests/test_plugin_governance.py`（更新现有平台页面断言）

1. 现有页面测试检查实际版本、介绍、配置入口以及错误提示；不新增仅镜像 HTML 结构的测试。
2. 分文件隔离运行定向测试，命令：`PYTHON=python3 JOBS=2 scripts/run_tests_isolated.sh /tmp/plugin-card-test-files.txt`，清单只含以上两个文件。预期两个文件均通过。
3. 使用临时预览脚本渲染真实模板，加载仓库静态资源，在 1440×1000、1024×900、390×844 下检查三列、两列、一列与横向溢出，人工查看截图。
4. 执行 `git diff --check`，检查未声明版本、空介绍及较长名称/介绍的实际渲染。

### Task 3: 中文提交

1. 审查本次模板、样式、注释及现有测试的差异，排除其他工作区文件。
2. 执行 `git add` 指定本次文件，并使用中文提交说明，例如 `界面：放大插件卡片并展示版本与功能介绍`。
3. 提交正文如实记录定向测试、浏览器预览与“未执行全量测试”。
