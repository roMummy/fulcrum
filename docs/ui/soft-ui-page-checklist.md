# Soft UI 逐页实施与验收清单

依据：`soft-ui-guidelines.md`。更新时间：2026-10-21。先整理调性和规约，再实施认证、大盘、清单、详情与公开站点。

## 证据说明

- **D/M**：隔离应用真实路由，1440×900 / 390×844 均返回 200、单一 h1、无页面脚本错误、无根节点横向溢出。不是完整业务回归。
- **T**：补查 1024×768。公开站点与认证 5 页、后台代表 7 页。
- **交互**：实际执行了对应点击或键盘路径，不等同于只检查模板。
- **共享覆盖**：当前模板沿用新组件样式，不表示每个模板都必须重复改写结构。
- 截图根目录：`.run/soft-ui-screenshots/`。表中短名对应 `{短名}-{1440|390}.png`；该目录是本机忽略产物，不提交仓库，也不是线上数据。

| 顺序 | 页面 / 模板 | 本页实施结果 | 验收证据 / 截图短名 |
| --- | --- | --- | --- |
| 01 | 企业登录 `enterprise_login.html` | 双栏认证壳、密码切换、身份提示 | D/M/T；错误返回、键盘切换、无脚本路径；`enterprise-login` |
| 02 | 平台登录 `platform_login.html` | 独立平台身份、统一视觉 | D/M/T；错误返回、键盘切换；`platform-login` |
| 03 | 注册 `register.html` | 字段成组、相关字段双列、手机单列 | D/M/T；标签与自动填充测试；`register` |
| 04 | 初始化 `setup.html` / `setup_completed.html` | 共享认证材质与紧凑表单样式覆盖 | 模板语法与共享样式检查；未执行首次安装流程或浏览器验收 |
| 05 | 平台概览 `platform_dashboard.html` | 独立等高指标、趋势和运营分区 | D/M/T；`platform-dashboard` |
| 06 | 企业总览 `dashboard.html` | 简化重复页头、单曲线及按需小时明细 | D/M/T；键盘展开 24 个读数；`tenant-dashboard` |
| 07 | 平台租户 `platform_tenants.html` | 紧凑清单、精简眉题 | D/M；`platform-tenants` |
| 08 | 租户详情 `platform_tenant_detail.html` | 表面分组、统一字段间距 | D/M；`platform-tenant` |
| 09 | 平台员工 `platform_users.html` | 新增移入弹窗，清单优先 | D/M/T；新增/编辑 Escape、关闭与焦点返回；`platform-users` |
| 10 | 企业员工 `tenant_users.html` | 新增移入弹窗，保留角色与一次性密码提示 | D/M；新增/编辑交互；`tenant-users` |
| 11 | 套餐管理 `platform_plans.html` | 套餐清单优先、新增与编辑弹窗 | D/M；新增/编辑交互与 CSRF 字段；`platform-plans` |
| 12 | 订单审核 `platform_billing_orders.html` | 共享清单、金额和状态样式覆盖 | D/M；`platform-orders` |
| 13 | 审核详情 `platform_billing_order_detail.html` | 付款信息分组与审核动作样式 | D/M；`platform-order-detail` |
| 14 | 平台绑定清单 `platform_bot_bindings.html` | 共享状态、清单样式覆盖 | D/M；`platform-bindings` |
| 15 | 绑定详情 `platform_bot_binding_detail.html` | 分组层次、保留危险操作提示 | D/M；`platform-binding` |
| 16 | 运行监控 `platform_monitoring.html` | 紧凑指标、保留中断提示 | D/M；`platform-monitoring` |
| 17 | 协议服务 `platform_protocol_services.html` | 共享二级导航与配置表单覆盖 | D/M；`platform-protocols` |
| 18 | 代理池 `platform_proxy_pool.html` | 共享清单与弹窗样式覆盖 | D/M/T；`platform-proxies` |
| 19 | 全局日志 `platform_logs.html` | 精简眉题、紧凑筛选、局部滚动 | D/M；`platform-logs` |
| 20 | 账号接入 `channels.html` | 账号状态、接入清单层次 | D/M；`channels` |
| 21 | 渠道接入 `channel_connect.html` | 步骤、二维码与代理选项统一材质 | D/M/T；`channel-connect` |
| 22 | 渠道详情 `channel_detail.html` | 信息分组、紧凑指标和动作 | D/M；`channel-detail` |
| 23 | 接入引导 `account_onboarding.html` | 额度与接入表面简化 | D/M；`onboarding` |
| 24 | 机器人清单 `bots.html` | 共享紧凑表格与局部滚动 | D/M；`bots` |
| 25 | 机器人详情 `bot_detail.html` | 绑定与配置分组、移除重复眉题 | D/M；`bot-detail` |
| 26 | 节点清单 `provider_nodes.html` | 紧凑状态清单 | D/M；`nodes` |
| 27 | 节点详情 `provider_node_detail.html` | 分组层次、时间信息不放大 | D/M；`node-detail` |
| 28 | 插件 `plugins.html` | 目录改条目、警告可见、详情承载技术信息 | D/M/T；`plugins` |
| 29 | 能力详情 `plugin_detail.html` | 共享详情、密钥字段与弹窗样式覆盖 | D/M；`plugin-detail` |
| 30 | 运行状态 `plugin_runtime.html` | 紧凑指标和执行记录 | D/M；`plugin-runtime` |
| 31 | 会话清单 `conversations.html` | 共享紧凑筛选与表格 | D/M；`conversations` |
| 32 | 会话详情 `conversation_detail.html` | 消息阅读层次、精简眉题 | D/M；`conversation` |
| 33 | 消息记录 `messages.html` | 结果优先、移动筛选折叠 | D/M/T；手机筛选开合、主导航键盘开合；`messages` |
| 34 | 群清单 `chatroom_list.html` | 紧凑指标、搜索和状态 | D/M；`chatrooms` |
| 35 | 群详情 `chatroom_detail.html` | 文字指标收小、嵌套表格局部滚动 | D/M；实测根宽由 754px 修正为 390px；`chatroom-detail` |
| 36 | 联系人 `workwechat_contact_list.html` | 结果优先、统一指标与筛选 | D/M；`contacts` |
| 37 | 联系人详情 `workwechat_contact_detail.html` | 主次信息分组 | D/M；`contact-detail` |
| 38 | 套餐与额度 `account_billing.html` | 额度、金额与套餐选择层次 | D/M；`billing` |
| 39 | 企业订单 `account_orders.html` | 共享订单清单样式覆盖 | D/M；`orders` |
| 40 | 企业订单详情 `account_order_detail.html` | 付款信息、字段和二维码材质 | D/M；`order-detail` |
| 41 | 订阅信息 `tenant_subscription.html` | 周期、额度与续订分组 | D/M；`subscription` |
| 42 | 账号安全 `password.html` | 密码表单和通知样式 | D/M；`password` |
| 43 | 宣传首页 `marketing_home.html` | 柔和品牌 Hero，无截图、无旧轴线编号 | D/M/T；减少动态路径；`home` |
| 44 | 套餐价格 `pricing.html` | 网格居中、推荐标记同行、金额与按钮对齐 | D/M/T；居中实测与服务端价格测试；`pricing` |

共享壳：`base.html`、`platform_base.html`、`marketing_base.html`、`auth_base.html`。保留顶部导航、品牌命名、平台/企业身份与请求路径，不新增旧入口或第二套主题。未被当前路由使用的模板不因本轮调整新增入口。

## 实际验证记录

### 定向测试

```bash
APP_ENV=test DATABASE_URL=sqlite:// \
LOG_DIR="$PWD/.run/soft-ui-logs" \
.venv/bin/python -m pytest -q \
  tests/test_soft_ui.py tests/test_public_marketing_pages.py \
  tests/test_public_site_auth.py tests/test_saas_login.py \
  tests/test_generic_channel_dashboard.py tests/test_platform_admin.py \
  tests/test_tenant_admin.py tests/test_tenant_billing_pages.py \
  tests/test_platform_billing_pages.py
```

结果：71 passed。最后补充弹窗焦点返回与趋势明细后，重新执行 `test_soft_ui.py`、`test_generic_channel_dashboard.py`：21 passed。

- `node --check app/static/js/auth.js`、`node --check app/static/js/marketing.js` 通过。
- 56 个 Jinja 模板语法解析通过；`git diff --check` 通过。
- 移除旧 Swiss 专属测试，保留并迁移身份、导航、价格、局部滚动、筛选与可访问性契约。

### 浏览器与密度

本机环回端口 8291，临时 SQLite、隔离账号与生成数据，后台 worker 关闭。Chromium 检查 38 个后台页面的 D/M 共 76 个视口，公开/认证 5 页 D/M/T 共 15 个视口，以及 7 个后台代表页 T，共 98 个页面视口。

- 记录：`backend-report.json`、`public-report.json`；代表页额外人工查看截图。
- 平台员工清单起点约 230px、企业员工约 263px、套餐清单约 209px（1440×900），新增大表单不再占据首屏。
- 手机企业/平台登录提交按钮在 390×844 首屏内；注册允许自然纵向滚动。
- 新增和编辑弹窗：打开、关闭、Escape、焦点进入/返回、手机关闭按钮可达；创建表单保留 CSRF。
- 认证：键盘切换密码、错误反馈位置、账号保留/密码清空、重新提交；关闭脚本时保留原生密码输入。
- 大盘：24 个小时读数通过原生折叠区保留，键盘可展开；数据聚合口径未更改。
- 使用 Web Interface Guidelines 复查触控、局部滚动和焦点，修复了群详情网格溢出与收起菜单后的焦点返回问题。

## 验证边界

没有执行全量测试、完整端到端回归、真实协议扫码登录、实际支付/上传凭证或真实设备验证。首次安装两页仅检查模板与共享样式，未重新初始化系统。发现的企业趋势日志旧变量错误已单独提交修复（`b4470e9`），没有扩大到统计业务重构。

没有部署、推送、重启 201 或 142 上的服务；本轮交付为当前分支的 UI 实现、规约与本机核验记录。
