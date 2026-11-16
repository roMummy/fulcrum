# 插件卡片调用趋势实施计划

**Goal:** 为企业和平台插件卡片添加真实调用趋势、计数及最近调用时间，并补齐四类执行入口的统计。

**Architecture:** 复用现有执行日志和小时表，由一个公共记录函数负责分类与原子计数；各执行边界负责提供最终结果。企业和平台接口调用同一批量查询服务，前端通过共用模板与原生 SVG 展示，避免引入图表依赖。

**Tech Stack:** FastAPI、SQLAlchemy、Jinja、原生 JavaScript／SVG、pytest、Chromium。

## 任务 1：统一记录与调用入口

文件：修改 `app/services/analytics.py`、`app/services/ai_intent_worker.py`、`app/services/plugin_runtime/scheduler.py`、`app/main.py`；新增 `tests/test_plugin_usage.py`。

1. 用临时数据库编写分类测试，覆盖匹配、正常、执行失败、超时、发送失败、跳过及拦截。
2. 公共写入函数显式接收可信插件身份与上下文；日志与小时表同事务写入，增量更新使用 SQL 表达式。
3. 原消息分发记录函数改为公共函数的适配器；AI 重分发、手动及定时入口接入同一记录路径。拦截在执行前完成，桥接函数不重复记录。
4. 定向验证异常仍有记录、重试按实际执行计数以及现有插件参数／密钥边界不变。

## 任务 2：批量查询与安全接口

文件：新增 `app/services/plugin_usage.py`；修改 `app/main.py`、`app/web/platform_plugins.py`；扩展 `tests/test_plugin_usage.py`。

1. 从小时表按插件及小时／日期聚合，统一用业务时间构造完整时间轴，补齐无数据桶。
2. 日志仅批量查询最近调用时间；以固定查询数量获取全部卡片，避免查询数随插件数增长。
3. 新增企业和平台各自受认证保护的只读统计入口。企业用当前租户会话和可见插件集合，不接受客户端指定租户；平台必须使用平台会话。
4. 定向验证 24h／7d／30d、非法时间范围、跨租户隔离、跨企业汇总及隐藏插件不泄漏。

## 任务 3：卡片图表

文件：新增 `app/templates/_plugin_usage.html`、`app/static/js/plugin-usage.js`；修改 `app/templates/plugins.html`、`app/templates/platform_plugins.html`、`app/static/css/style.css`。

1. 在两类页面加入共用时间筛选和每卡统计区，保留原有开关、配置、版本与筛选。
2. 批量请求统计，绘制三条共用刻度的 SVG 趋势线，提供时间轴、图例、最近调用时间及明确的空／失败状态。
3. 交互支持悬停、触屏点读、键盘方向选择；切换范围取消或忽略旧请求，避免旧结果覆盖新选择。
4. 桌面与手机检查文字、提示位置和无横向溢出，颜色之外仍用标签区分指标。

## 任务 4：轻量验证与提交

1. 运行 `pytest -q tests/test_plugin_usage.py`，再运行直接相关的插件调度、AI、目录卡片和治理测试。
2. 对新前端脚本执行 `node --check`，对修改 Python 执行 AST 检查，对 Jinja 做实际页面渲染验证，运行 `git diff --check`。
3. 使用临时数据库的浏览器验证两类页面、两种视口、时间切换、点读和无数据／失败情况；只记录实际完成的验证。
4. 中文提交功能与测试，不纳入用户原有未跟踪群功能草稿；记录未执行全量验证及发现的范围外历史问题。
