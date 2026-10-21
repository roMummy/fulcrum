# Soft UI 逐页实施与验收清单

依据：`soft-ui-guidelines.md`。流程：先完成认证页基准，再落实大盘、列表与详情，最后收尾公开站点。每个页面在完成后更新状态；无真实数据的业务页用隔离预览数据核验。

状态初始为“待实施”。此前共享 token 与认证模板改动均为草稿，未验收不标记完成。

| 顺序 | 页面 / 模板 | 本页重点 | 状态与证据 |
| --- | --- | --- | --- |
| 01 | 企业登录 `enterprise_login.html` | 双栏比例、标签分组、密码显示、错误位置 | 待实施 |
| 02 | 平台登录 `platform_login.html` | 平台身份、独立提交、企业入口 | 待实施 |
| 03 | 注册 `register.html` | 紧凑字段网格、验证码、移动单列 | 待实施 |
| 04 | 初始化 `setup.html` / `setup_completed.html` | 首次安装表单、完成状态 | 待实施 |
| 05 | 平台概览 `platform_dashboard.html` | 指标、趋势分区、对齐、首屏密度 | 待实施 |
| 06 | 企业总览 `dashboard.html` | 五组双列表面、图表、空态 | 待实施 |
| 07 | 平台租户 `platform_tenants.html` | 表头、清单密度、开通入口 | 待实施 |
| 08 | 租户详情 `platform_tenant_detail.html` | 主次信息、额度、表单分组 | 待实施 |
| 09 | 平台员工 `platform_users.html` | 清单与编辑弹窗 | 待实施 |
| 10 | 企业员工 `tenant_users.html` | 清单、一次性密码提示、编辑 | 待实施 |
| 11 | 套餐管理 `platform_plans.html` | 套餐清单、编辑弹窗 | 待实施 |
| 12 | 订单审核 `platform_billing_orders.html` | 状态和金额、结果密度 | 待实施 |
| 13 | 审核详情 `platform_billing_order_detail.html` | 付款信息、审核动作 | 待实施 |
| 14 | 平台绑定清单 `platform_bot_bindings.html` | 状态与关联信息 | 待实施 |
| 15 | 绑定详情 `platform_bot_binding_detail.html` | 记录分组与危险操作 | 待实施 |
| 16 | 运行监控 `platform_monitoring.html` | 指标、运维动作、确认弹窗 | 待实施 |
| 17 | 协议服务 `platform_protocol_services.html` | 二级菜单与配置表单 | 待实施 |
| 18 | 代理池 `platform_proxy_pool.html` | 宽数据行与配置弹窗 | 待实施 |
| 19 | 全局日志 `platform_logs.html` | 紧凑筛选和局部代码滚动 | 待实施 |
| 20 | 账号接入 `channels.html` | 接入列表与业务状态 | 待实施 |
| 21 | 渠道接入 `channel_connect.html` | 步骤、二维码、代理选项 | 待实施 |
| 22 | 渠道详情 `channel_detail.html` | 接入状态、动作、配置 | 待实施 |
| 23 | 接入引导 `account_onboarding.html` | 额度与接入入口 | 待实施 |
| 24 | 机器人清单 `bots.html` | 表格局部滚动 | 待实施 |
| 25 | 机器人详情 `bot_detail.html` | 绑定信息与配置分组 | 待实施 |
| 26 | 节点清单 `provider_nodes.html` | 紧凑状态清单 | 待实施 |
| 27 | 节点详情 `provider_node_detail.html` | 运维信息与业务入口 | 待实施 |
| 28 | 运营能力 `plugins.html` | 筛选、能力开关、内容密度 | 待实施 |
| 29 | 能力详情 `plugin_detail.html` | 用户自有配置、密钥字段 | 待实施 |
| 30 | 运行状态 `plugin_runtime.html` | 执行记录、状态和错误 | 待实施 |
| 31 | 会话清单 `conversations.html` | 筛选同行、表格首屏 | 待实施 |
| 32 | 会话详情 `conversation_detail.html` | 消息时间与内容阅读 | 待实施 |
| 33 | 消息记录 `messages.html` | 紧凑筛选、长消息和局部滚动 | 待实施 |
| 34 | 群清单 `chatroom_list.html` | 清单、搜索与状态 | 待实施 |
| 35 | 群详情 `chatroom_detail.html` | 能力配置、业务信息 | 待实施 |
| 36 | 联系人 `workwechat_contact_list.html` | 结果优先、筛选密度 | 待实施 |
| 37 | 联系人详情 `workwechat_contact_detail.html` | 主次字段与动作 | 待实施 |
| 38 | 套餐与额度 `account_billing.html` | 额度、套餐选择、金额 | 待实施 |
| 39 | 企业订单 `account_orders.html` | 订单清单与状态 | 待实施 |
| 40 | 企业订单详情 `account_order_detail.html` | 支付信息、付款码 | 待实施 |
| 41 | 订阅信息 `tenant_subscription.html` | 周期、额度与续订 | 待实施 |
| 42 | 账号安全 `password.html` | 密码表单、错误提示 | 待实施 |
| 43 | 宣传首页 `marketing_home.html` | Soft UI 构图、无截图 Hero、行动 | 待实施 |
| 44 | 套餐价格 `pricing.html` | 网格居中、套餐比较、推荐态 | 待实施 |

共享壳：`base.html`、`platform_base.html`、`marketing_base.html`、`auth_base.html`。共享局部模板随对应业务页面检查。仓库中未被当前路由使用的模板不因本轮 UI 调整新增入口。

## 验证记录

待逐页实施后记录实际执行的命令、视口、截图位置及剩余限制。本轮不自动部署 201。
