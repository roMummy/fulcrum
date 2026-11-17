# 群详情导航与插件自定义设置实施计划

> **For Codex:** 用户已确认直接执行推荐方案，在当前会话按下面步骤实施、定向验证并提交。

**Goal:** 统一群详情导航，提供真正按群生效的插件指令与结构化配置。

**Architecture:** 复用群作用域表、插件字段声明、群功能卡片交互和现有内存消息视图。新建统一命令工具与群设置服务，目录匹配、原生消息及定时执行读取同一群设置，不新增 JSON 配置入口或数据库迁移。

**Tech Stack:** FastAPI、SQLAlchemy、Jinja、原生 CSS/JavaScript、pytest、Playwright。

---

## 任务 1：指令映射与原生执行回归

文件：新增 `tests/test_group_plugin_settings.py`、`tests/test_group_plugin_commands.py`；按需复用 `tests/test_existing_stateful_plugins.py`、`tests/test_provider_node_pages.py` 的临时库与身份夹具。

1. 编写失败用例：本群“打卡”匹配签到，“补打卡”独立匹配补签；设置只影响本群。
2. 验证原生插件实际接收关闭图片及积分配置，而非仅目录上下文正确。
3. 使用真实 SQL 读取原消息，确认插件事务提交及异常退出都不写入转换后的命令。
4. 执行新增用例并记录预期失败，不在真实群重放消息。

## 任务 2：统一群设置与命令处理

文件：新增 `app/services/plugin_commands.py`、`app/services/group_plugin_settings.py`；修改 `app/services/plugin_directory.py`、`app/services/plugin_runtime/service.py`、`app/services/ai_dispatch.py`、`app/services/ai_intent_worker.py`，更新对应内存视图测试。

1. 移动现有内存消息上下文管理器到共用命令模块，并直接更新全部调用。
2. 实现命令映射、最长前缀匹配及参数保留；静态匹配合并群配置，插件匹配器与执行使用正式业务命令。
3. 原生执行从可信群会话读取群覆盖，敏感配置以绑定密文为准。
4. 群设置服务生成非敏感字段视图，校验并保存差异；保留明确空值，提供只恢复配置与全部恢复的清晰语义。
5. 运行新增命令与设置用例，修复本次引入的问题。

## 任务 3：结构化页面与路由

文件：修改 `app/main.py`、`app/templates/chatroom_detail.html`、`app/static/css/style.css`、`app/static/js/group-features.js`；必要时新增群插件局部模板和脚本。

1. 接入本群设置视图与新的结构化保存/恢复路由，校验身份、CSRF、渠道和绑定；不恢复废弃 JSON 路由。
2. Tab 统一圆角分段样式，手机导航自行滚动，增加当前页语义并清理旧 `.tab-bar` 样式。
3. 复用可展开卡片展示版本、说明、当前指令、状态与来源；专属字段按声明分组，开关与保存互不影响。
4. 保存成功后同步卡片摘要及来源提示，失败保留输入，恢复后同步表单状态。

## 任务 4：定向校验与浏览器验收

文件：更新 `tests/test_chatroom_tab_redirects.py`、`tests/test_page_navigation.py`；新增本群设置浏览器用例。

1. 用真实表单与临时库验证保存、刷新、配置恢复和启停独立；删除“禁止任何群配置”的旧测试断言，继续禁止 JSON 编辑器。
2. 运行本轮新增用例及直接相关的插件详情、群作用域、AI 内存视图轻量测试；不执行全量回归。
3. 在 1440px、390px 验证页面、Tab、指令、长文本、空配置、保存提示、手机无横向溢出，输出本地截图供复核。
4. 检查变更 Python AST、JS 语法、模板编译和 `git diff --check`。
5. 中文 commit 中说明实际变更与测试结果；仅暂存本轮改动，不提交未跟踪群功能草稿或凭据。
