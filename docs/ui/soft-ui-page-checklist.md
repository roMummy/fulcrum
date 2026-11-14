# Soft UI 逐页实施与验收清单

依据：`soft-ui-guidelines.md`。更新时间：2026-11-14。先整理调性和规约，再实施认证、大盘、清单、详情与公开站点。

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
| 28 | 插件 `plugins.html`、平台插件 `platform_plugins.html` | 2026-11-14 改为三列功能卡片，展示真实版本、完整介绍和底部操作；平板两列、手机一列 | 两页真实模板本地预览：1440×1000、1024×900、390×844；无横向溢出；截图 `.run/plugin-card-preview/`，未部署验证 |
| 28a | 猜图片题库 `guess_image_bank.html` | 单一缩略图清单、紧凑筛选；添加/编辑、导入预览和资源检查共用后台弹窗风格，删除独立旧页面 | 页面接口 5 项、题库服务 16 项通过；真实模板本地预览 1440×900、1024×768、390×844；错误保留、编辑、导入重试、删除和焦点返回；截图 `.run/guess-image-ui-preview/`，接口模拟与服务端验证分开记录 |
| 29 | 能力详情 `plugin_detail.html` | 共享详情、密钥字段与弹窗样式覆盖 | D/M；`plugin-detail` |
| 30 | 运行状态 `plugin_runtime.html` | 紧凑指标和执行记录 | D/M；`plugin-runtime` |
| 31 | 会话清单 `conversations.html` | 共享紧凑筛选与表格 | D/M；`conversations` |
| 32 | 会话详情 `conversation_detail.html` | 消息阅读层次、精简眉题 | D/M；`conversation` |
| 33 | 消息记录 `messages.html` | 结果优先、移动筛选折叠 | D/M/T；手机筛选开合、主导航键盘开合；`messages` |
| 34 | 群清单 `chatroom_list.html` | 紧凑指标、搜索和状态 | D/M；`chatrooms` |
| 35 | 群详情 `chatroom_detail.html` | 资料展示公告与置顶能力提示；成员按角色排序，邀请关系改为头像卡片与 SVG 图谱，支持缩放、拖动、搜索定位和放大弹窗，未关联成员单列 | 原资料 11 项；图谱在真实临时应用 1440×900、1024×768、390×844 完成 24 项浏览器检查；截图 `.run/chatroom-invitation-graph-preview/`，未验证真实设备 |
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

### 2026-11-14 邀请关系图谱

- `.venv/bin/python -m pytest -q tests/test_chatroom_invitations.py tests/test_chatroom_profile_pages.py tests/test_chatroom_detail_avatar.py --tb=short -o faulthandler_timeout=30` 独立执行结果 `39 passed`。覆盖邀请证据、未知和未关联成员分组、缺失邀请人、退群、同名、循环与 1500 层链；真实详情路由校验图谱空状态、双方入图、安全转义、只读权限及空群。
- 页面测试使用临时数据库，在沙箱外运行。首次将浏览器与测试并行时，其夹具争用本地 9165 端口导致启动失败；浏览器随后改用系统分配端口，再独立重跑上述 39 项全部通过。未将端口占用误记为应用缺陷。
- 使用真实 FastAPI 群详情路由、认证、Jinja 模板和工作区静态资源，Chrome 在 1440×900、1024×768、390×844 完成 24 项检查，页面均单一 h1、身份键唯一、无根节点横向溢出、无 JavaScript 错误。构造样例包含 12 层邀请链、长中文昵称、同名不同 ID、退群成员和缺失邀请人。
- 实际验证展开全部和收起、单分支 Enter/Space、深层成员自动展开祖先、姓名和 ID 搜索及同名区分、关系来源和邀请人详情、缩放/方向键/鼠标拖动/适应画布。放大弹窗移动同一工作区，选择状态保留，Esc 关闭后焦点返回可见入口；未知成员可搜索定位，循环提示与全未知/空群状态保持可用。
- 已查看桌面、手机和放大弹窗截图，补齐独立搜索框的 Soft UI 输入样式并重新完成全部 24 项浏览器检查。当前 Chrome 与 Playwright 的默认截图通道对简单静态页也超时，使用 `PLAYWRIGHT_LEGACY_SCREENSHOT=1` 后实际截图成功；截图与 `report.json` 位于本机忽略目录 `.run/chatroom-invitation-graph-preview/`。
- 本次改动的 Python AST、群详情和图谱 Jinja 加载、`node --check app/static/js/chatroom-invitations.js`、`git diff --check` 通过。旧缩进树的模板标记、样式和调用已替换，不保留双入口。
- 邀请数据全部为临时库构造数据，没有触发真实设备同步或写入线上关系，也不代表微桥已补齐邀请人。未执行全量测试、完整端到端回归或真实手机操作；201 发布后的只读验收与备份另外记录于服务器维护档案。

### 2026-11-14 群资料、成员顺序与邀请关系

- `tests/test_chatroom_profile.py`、`tests/test_chatroom_invitations.py` 最终执行结果合计 `22 passed`：群主身份优先与退群置底、公告正文提取和转义、缓存/摘要/失败状态、旧回报保护、TEXT 字节容量、邀请来源优先、明确成员 ID、扫码来源、空快照保留、重复和乱序邀请、历史过滤、同名与退群、循环关系及 1500 层邀请链。另直接验证 iPad、企微和咕嘎的普通成员发言不会清空邀请关系。
- `tests/test_chatroom_profile_pages.py` 最终执行结果 `16 passed`：四个渠道的成员顺序和邀请视图、公告状态及置顶提示、打开资料页不下发任务、同步成员和公告两条任务及当前标签保留、真实设备回报处理入口的幂等与失败 ACK、历史补查、只读权限、CSRF、跨租户隔离，以及关闭成员通知后仍记录邀请关系。
- `tests/test_operation_feedback_routes.py` 执行结果 `22 passed`：同步进度必须等待成员和公告两个任务结束，部分成功、失败及普通表单跳转均按真实任务状态反馈。`tests/test_group_features.py`、`tests/test_chatroom_tab_redirects.py` 执行结果合计 `13 passed`。
- 相关回归分组 `tests/test_wechat_gugatool_chatrooms.py`、`tests/test_webridge_group_health.py`、`tests/test_webridge_member_directory.py`、`tests/test_wechat_webridge_capabilities.py`、`tests/test_operation_feedback.py` 执行结果合计 `126 passed, 2 failed`。两项失败为设备面板和实时摘要的旧测试夹具没有创建 `raw_webhook_events` 表；已核对测试文件和 `wechat_webridge/dashboard.py` 均与 HEAD 相同，本轮未修改。另一次包含 `tests/test_workwechat_chatrooms.py` 的定向分组结果为 `32 passed, 6 failed`，六项均在创建缺少租户 ID 的 `Bot` 夹具时失败，尚未进入业务代码；该测试及模型也未被本轮修改。按研发阶段策略记录这些历史夹具问题，不扩大修改范围。
- 上述 Python 测试使用项目 `.venv/bin/python`。TestClient 在沙箱内挂起的运行已中止，页面、任务反馈及包含 TestClient 的相关分组在沙箱外完成；所有数据库均为隔离测试数据，没有连接生产库。
- 使用真实群详情路由、认证、临时数据库、Jinja 模板和工作区 CSS/JS，在 Chrome 的 1440×900、1024×768、390×844 完成 11 项浏览器检查。资料页单一 h1，长公告与长 URL 自然换行，脚本字符安全显示，根节点和邀请树没有横向溢出；成员表保留自身横向滚动与角色列宽。
- 实际点击邀请树逐支展开、展开全部、收起分支，执行 Enter/Space 键盘操作；手机提交历史补查后出现新增关系并保留成员标签。实际点击同步后向两条任务提交构造的手机回报，确认公告更新且仍停留资料标签。浏览器没有 JavaScript 错误；12 层长昵称链仅在树容器内滚动。样例成员、公告和设备回报均为明确构造数据，没有将其当作生产结果。
- 14 个本轮 Python 文件通过 AST 检查，`base.html`、`chatroom_detail.html`、`_chatroom_invitations.html` 通过 Jinja 加载检查，`node --check app/static/js/chatroom-invitations.js` 和 `git diff --check` 通过。截图及 `report.json` 保存在本机忽略目录 `.run/chatroom-profile-preview/`。
- 公告来自手机已有缓存，不能据此保证微信当前公告已更新；缺少明确邀请证据时显示「未知」，历史补查只检查本群已保存的最近 2000 条系统消息。未执行真实设备验证、全量测试、完整端到端回归、推送或远程部署。

### 2026-11-14 猜图片题库整合

- `tests/test_guess_image_pages.py` 最终执行结果 `5 passed`：统一清单与弹窗入口、只读权限、CSRF、跨租户详情和媒体隔离、保存错误回滚、原样保存答案、导入预览/确认、ZIP 导出、资源检查、平台停用和操作审计。
- `tests/test_guess_image_bank.py` 执行结果 `16 passed`：图片校验、租户资源隔离、不可变图片、导入会话绑定/有效期、幂等确认和 ZIP 校验。两个测试文件使用项目 `.venv/bin/python` 分别运行；页面测试使用临时 SQLite，在已确认可运行 TestClient 的沙箱外执行。
- 额外执行共享后台壳和账号/套餐弹窗结构两项检查，均通过。另执行的 `test_query_pages_share_compact_filter_toolbar_and_mobile_collapse` 失败：当前 `messages.html` 已不含该历史测试要求的 `compact-filter`。已核对消息模板与 HEAD 内容一致，消息模板和该测试均未被本次修改；按当前研发策略记录，不扩大修改范围。
- 使用仓库真实 Jinja 模板、CSS 和页面脚本，以及明确标识的本地题目和模拟接口响应，在 1440×900、1024×768、390×844 检查主清单与新增弹窗。单一 h1，根节点与弹窗内部均无横向溢出，浏览器无 JavaScript 错误。
- 实际操作验证图片预览、启用时来源/授权必填、提交锁定、保存失败保留字段与文件、答案空格原样提交、关闭后重置、详情失败/重试、编辑回填与保留原图、筛选和页码保留、删除取消/确认、导入失败留文件、样例安全转义、预览过期后重新校验、确认导入，以及资源检查失败/重试和焦点返回。
- 浏览器检查发现并修复共享弹窗的一处误关闭：内部按钮切换步骤使弹窗高度改变后，旧坐标被当作遮罩点击。最小复现记录确认修复前返回导入选择会关闭，修复后弹窗保持打开；遮罩关闭仍使用原边界判断。
- `node --check app/static/js/guess-image.js`、两个 Python 文件 AST、`base.html` 和 `guess_image_bank.html` 的 Jinja 加载检查、`git diff --check` 均通过。
- 截图和浏览器结果位于本机忽略目录 `.run/guess-image-ui-preview/`。截图使用减少动态模式避免截取入场动画中间帧；预览数据和接口为构造样例，不是生产题库。未执行全量测试、完整端到端回归或远程部署。

### 2026-11-14 插件功能卡片

- 使用 `.venv/bin/python` 和 `scripts/run_tests_isolated.sh` 分文件隔离运行 `tests/test_plugin_detail_page.py`、`tests/test_plugin_governance.py`，两个文件均通过。系统 Python 缺少 SQLAlchemy；沙箱中的 TestClient 超时，改用项目环境在沙箱外完成测试。
- 另行执行 `tests/test_guess_image_pages.py::test_generic_second_button_tenant_navigation_and_readonly_permissions`，结果 `1 passed`，确认配置、管理题库入口及只读权限。最终筛选栏调整后重跑插件列表展示用例，结果 `1 passed`。
- 使用仓库真实 Jinja 模板、插件清单和静态资源做本地 Chrome 预览，两页在 1440×1000、1024×900、390×844 下分别为三列、两列、一列；根节点无横向溢出，浏览器无 JavaScript 错误。
- 使用明确标识的构造数据补查长名称、长版本、长介绍、模板转义、缺失版本与介绍、清单错误与警告、平台停用及空结果；未把构造数据当作线上数据。
- 使用现有平台脚本实际检查按功能介绍搜索、空搜索结果、可见范围和历史版本弹窗，以及启停后的卡片状态同步。浏览器中的启停接口为本地模拟响应；服务端认证、CSRF 和审计由上述定向测试覆盖。
- 三个修改的 Python 文件通过 AST 语法检查，两份页面模板通过 Jinja 加载检查，`git diff --check` 通过。
- 截图与预览结果保存在本机忽略目录 `.run/plugin-card-preview/`。本次未执行全量测试，也未部署到远程服务器。

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
