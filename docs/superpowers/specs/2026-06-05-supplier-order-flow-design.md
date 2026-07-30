# 供应商订单收发子系统设计

> 历史归档：该设计描述的是 2026-06-05 曾经计划放在 MBA 内的 `supplier_order_flow` 子系统。2026-06-09 起收发单已经由独立 `supplier-order-admin` 系统承载，MBA 不再维护旧重业务插件、`app/api/supplier_order_flow.py`、`app/services/supplier_order_flow`、`app/templates/supplier_order_flow` 或 `SupplierOrder*` 模型。当前 `plugins/supplier_order_flow` 仅允许作为轻量桥接插件存在，后续不要按本文在 MBA 内继续新增或修改收发单业务逻辑。

## 背景

`supplier_order_flow` 是一个高度定制化的订单收单、发单、回传发货插件。它运行在 `workwechat` 供应商群场景下，每天从现有业务系统获取供应商订单，导出 Excel 发送给对应企微群；供应商发货后回传 Excel、文本或其他可解析信息，系统再调用业务系统完成发货导入。

该能力已经接近一个小型业务系统，因此不能只依赖普通插件配置页和 `config_json`。本设计选择“架构层面通用，业务层面定制”：核心项目只提供受控的插件后台入口、登录保护和页面壳，供应商订单收发的页面、数据表、接口和业务流程都按本业务单独实现。

## 设计目标

1. 支持从业务系统 hook/API 同步最新供应商清单。
2. 支持在后台维护供应商和企微群的绑定关系、手工编码、启停状态和供应商特殊策略。
3. 支持手动或定时按供应商拉取待发订单，生成 Excel 并发送到对应企微群。
4. 支持记录发单批次、订单明细、发送结果、回传记录、解析结果和发货导入结果。
5. 支持供应商回传标准 Excel 后自动解析 `分单ID`、`快递公司`、`快递单号` 并导入业务系统。
6. 支持对非标准文本、粘贴表格等内容做有限解析，低置信度内容进入人工确认。
7. 保留后续拆成独立项目的可能性：插件入口薄，业务 service 层边界清晰。

## 非目标

1. 不做通用插件市场或动态后台组件系统。
2. 不允许插件任意注册 FastAPI 路由。
3. 不把供应商映射、批次进度、回传状态长期塞进 `BotPlugin.config_json`。
4. 不照搬旧项目 `D:\trgf\trgf_order_robot` 的插件结构、硬编码 token、线程式定时任务和混合命令处理方式。
5. 第一阶段不做 OCR 图片识别，图片和截图先进入人工确认或后续阶段。

## 总体架构

```text
------------------+       +--------------------------+       +------------------+
| 业务系统          | <---> | Multi Bot Admin 子系统     | <---> | WorkWechat 群     |
| 供应商/订单/发货  |       | supplier_order_flow       |       | 文件/消息/命令     |
+------------------+       +--------------------------+       +------------------+
```

项目内模块划分：

```text
plugins/supplier_order_flow/
  config.toml
  plugin.py

app/services/supplier_order_flow/
  supplier_sync.py
  order_api.py
  excel_service.py
  batch_service.py
  return_service.py
  parser.py
  express.py

app/api/supplier_order_flow.py

app/templates/supplier_order_flow/
  dashboard.html
  suppliers.html
  batches.html
  returns.html
  exceptions.html
  settings.html
```

核心项目只新增薄入口：

```text
GET /plugin-admin/{plugin_code}
GET /plugin-admin/{plugin_code}/{section}
```

该入口负责校验插件存在、后台入口已启用、当前用户已登录，然后把请求导向供应商订单收发后台。复杂业务写接口仍显式写在 `app/api/supplier_order_flow.py`，不做动态路由注册。

## 模块职责

### 插件入口

`plugins/supplier_order_flow/plugin.py` 只负责接收事件并转发：

- 匹配企微群文本命令，例如发单、查批次、查进度。
- 匹配企微群入站文件，尤其是 Excel 文件。
- 从消息上下文提取 `channel_id`、`conversation_external_id`、发送人、文件路径、文件名等信息。
- 调用 `app/services/supplier_order_flow` 中的业务服务。

插件入口不直接实现复杂业务流程，不持久化业务状态，不直接拼业务系统 HTTP 请求。

### 供应商同步

`supplier_sync.py` 负责从业务系统 hook/API 获取供应商清单，并写入本地供应商主数据表。

同步字段建议：

- `external_supplier_id`：业务系统供应商 ID。
- `supplier_name`：业务系统供应商名称。
- `manual_code`：发单文件名使用的人工编码，可本地覆盖。
- `source_status`：业务系统状态。
- `local_status`：本地启停状态。
- `last_synced_at`：最后同步时间。
- `raw_payload_json`：原始返回摘要，便于排查接口字段变更。

供应商列表由业务系统提供，本地后台只维护群绑定和策略，不手工维护全量供应商名单。

### 业务系统接口

`order_api.py` 负责封装业务系统接口。

旧项目已验证的接口能力可迁移：

- 获取订单拆分列表：`GET /m-order/m/ordersplit/ordersplitlist`
- 更新订单为已打印或已处理：`PUT /m-order/m/ordersplit/hairstic`
- 上传发货 Excel：`POST /m-order/m/ordersplit/hairinport`

配置原则：

- `base_url`、`channel`、`tag` 可通过后台配置。
- `authorization`、token、密码类字段按敏感配置处理，不提交真实值。
- 请求和响应统一记录摘要，敏感 header 不写入明文日志。
- 响应成功条件沿用旧项目经验：业务响应 `code == "0"`。

### Excel 服务

`excel_service.py` 负责订单导出、回传读取、发货导入文件生成。

首版发单 Excel 字段沿用旧项目成熟字段：

| 字段 | 说明 |
| --- | --- |
| 快递单号 | 供应商回填 |
| 快递公司 | 供应商回填 |
| 快递网点 | 系统映射或供应商回填 |
| 订单号 | 业务系统订单号 |
| 会员编号 | 业务系统会员编号 |
| 订单时间 | 下单时间 |
| 订单支付时间 | 支付时间 |
| 姓名 | 收货人 |
| 联系电话 | 收货电话 |
| 省 | 省份 |
| 市 | 城市 |
| 县/区 | 区县 |
| 收货地址 | 详细地址 |
| 商品名称 | 商品名称 |
| SKU名称 | SKU 名称 |
| SKU货号 | SKU 编码 |
| 69码 | 商品条码 |
| 数量 | 分单数量 |
| 分单ID | 发货回传主匹配键 |
| 备注 | 供应商备注 |
| 出库单号 | 供应商出库单 |

文件名沿用旧项目业务习惯：

```text
[订单数]单-供应商编码-MMddHHmm.xls
```

首版优先保持 `.xls` 兼容性，后续可按供应商能力增加 `.xlsx` 模板。

### 批次服务

`batch_service.py` 负责发单批次生命周期：

- 创建批次。
- 保存订单明细快照。
- 记录 Excel 文件路径、文件 MD5、发送群和发送时间。
- 调用企微发送文件。
- 更新业务系统订单状态。
- 统计批次进度。
- 支持失败重试和人工补发。

批次状态建议：

- `draft`：已生成但未发送。
- `sent`：已发送到群。
- `partial_returned`：部分回传。
- `importing`：发货导入中。
- `completed`：全部完成。
- `failed`：关键步骤失败。
- `cancelled`：人工取消。

### 回传服务

`return_service.py` 负责处理供应商回传：

- 根据群 ID 找到供应商和最近未完成批次。
- 保存回传文件记录。
- 解析标准 Excel。
- 校验分单 ID 是否属于当前供应商和当前批次。
- 去重快递单号和分单 ID。
- 生成发货导入 Excel。
- 上传业务系统发货接口。
- 记录每一行成功、失败、跳过或待确认原因。

标准 Excel 首版必须包含或可模糊识别：

- `分单ID`
- `快递公司`
- `快递单号`

如果只提供订单号、出库单号或混合文本，先走非标准解析和人工确认流程。

### 非标准解析

`parser.py` 负责低风险规则解析：

- `分单ID 快递公司 快递单号`
- `订单号 快递公司 快递单号`
- 粘贴表格中的列名匹配。
- 多行文本批量解析。

解析结果按置信度分级：

- `high`：分单 ID 命中本地未完成订单，快递公司可识别，快递单号非空。
- `medium`：订单号命中本地未完成订单，但分单 ID 不明确。
- `low`：只识别到快递单号或疑似订单号。

只有 `high` 默认允许自动进入导入流程，`medium` 和 `low` 默认进入人工确认。

### 快递映射

`express.py` 迁移旧项目快递公司和网点映射能力：

- 将简称映射到标准快递公司，例如 `圆通` -> `圆通速递`。
- 将标准快递公司映射到业务系统需要的网点名，例如 `圆通速递` -> `限时购【CN|圆通】`。
- 支持在后台维护供应商级别覆盖规则。

## 数据模型

首版建议新增独立业务表，表名前缀使用 `supplier_order_`，避免和现有插件表混淆。

### supplier_order_suppliers

保存业务系统同步来的供应商主数据和本地覆盖信息。

关键字段：

- `id`
- `external_supplier_id`
- `supplier_name`
- `manual_code`
- `source_status`
- `local_status`
- `last_synced_at`
- `raw_payload_json`
- `created_at`
- `updated_at`

### supplier_order_group_bindings

保存供应商与企微群绑定。

关键字段：

- `id`
- `supplier_id`
- `channel_id`
- `chatroom_id`
- `conversation_external_id`
- `group_name_snapshot`
- `enabled`
- `send_enabled`
- `return_enabled`
- `created_at`
- `updated_at`

一个供应商可以绑定多个群，一个群首版默认只绑定一个供应商；如后续确有多供应商群，再通过策略扩展。

### supplier_order_batches

保存每次发单批次。

关键字段：

- `id`
- `batch_no`
- `supplier_id`
- `group_binding_id`
- `date_start`
- `date_end`
- `order_status_id`
- `file_path`
- `file_md5`
- `total_orders`
- `returned_orders`
- `imported_orders`
- `status`
- `send_message_id`
- `created_by`
- `sent_at`
- `completed_at`
- `error_summary`
- `created_at`
- `updated_at`

### supplier_order_items

保存订单明细快照和发货状态。

关键字段：

- `id`
- `batch_id`
- `split_id`
- `order_id`
- `order_no`
- `product_name`
- `sku_code`
- `barcode`
- `quantity`
- `ship_to`
- `mobile_masked`
- `province`
- `city`
- `district`
- `address_snapshot`
- `tracking_number`
- `express_company`
- `express_branch`
- `status`
- `raw_order_json`
- `created_at`
- `updated_at`

### supplier_order_returns

保存供应商每次回传记录。

关键字段：

- `id`
- `supplier_id`
- `group_binding_id`
- `batch_id`
- `source_type`
- `source_file_path`
- `source_file_name`
- `source_message_id`
- `sender_external_id`
- `parse_status`
- `import_status`
- `total_rows`
- `valid_rows`
- `invalid_rows`
- `duplicate_rows`
- `error_summary`
- `created_at`
- `updated_at`

### supplier_order_return_items

保存回传逐行解析结果。

关键字段：

- `id`
- `return_id`
- `batch_item_id`
- `split_id`
- `order_no`
- `tracking_number`
- `express_company`
- `express_branch`
- `confidence`
- `status`
- `failure_reason`
- `raw_row_json`
- `created_at`
- `updated_at`

### supplier_order_operation_logs

保存关键操作审计。

关键字段：

- `id`
- `operation_type`
- `operator_type`
- `operator_id`
- `related_type`
- `related_id`
- `content`
- `created_at`

## 核心流程

### 供应商同步

1. 管理员在后台配置供应商 hook/API。
2. 点击“同步供应商”或由定时任务触发。
3. 系统拉取业务系统供应商清单。
4. 根据 `external_supplier_id` upsert 本地供应商。
5. 保留本地 `manual_code`、`local_status` 和群绑定，不被同步覆盖。
6. 后台显示新增、更新、停用和异常数量。

### 手动发单

1. 管理员在后台选择供应商、企微群和日期范围。
2. 系统调用业务系统订单接口获取待发订单。
3. 系统生成发单 Excel，并计算文件 MD5。
4. 系统创建批次和订单明细快照。
5. 系统发送 Excel 到对应企微群。
6. 发送成功后调用业务系统状态接口更新为已打印或对应业务状态。
7. 后台和群消息显示批次号、订单数和处理结果。

### 标准回传 Excel

1. 供应商在企微群上传 Excel。
2. `workwechat` 入站文件能力保存文件，并把 `download_path`、`download_url`、`file_name` 写入消息详情。
3. 插件入口把文件事件转给回传服务。
4. 回传服务根据群绑定找到供应商和最近未完成批次。
5. 系统解析 `分单ID`、`快递公司`、`快递单号`。
6. 系统校验分单 ID 是否属于当前供应商、当前或可匹配批次。
7. 高置信度行生成发货导入 Excel 并上传业务系统。
8. 系统记录成功、失败、重复和待确认行。
9. 系统在群内回复处理摘要，并在后台展示明细。

### 非标准文本回传

1. 插件入口收到群文本消息。
2. 解析器尝试识别订单号、分单 ID、快递公司和快递单号。
3. 高置信度结果可自动导入。
4. 中低置信度结果进入异常处理页面，等待人工确认。
5. 人工确认后再生成发货导入文件并调用业务系统。

## 后台页面

### 总览

展示今日发单批次、订单数、回传数、导入成功数、失败数和待确认数。提供同步供应商、手动发单、查看异常的快捷入口。

### 供应商映射

展示业务系统供应商清单、本地启停、手工编码、绑定群、最后同步时间。支持绑定企微群、解绑、修改手工编码、启停供应商。

### 发单批次

展示批次号、供应商、群、订单数、回传进度、导入进度、状态、文件路径和错误摘要。支持查看明细、补发文件、重试状态更新。

### 回传记录

展示回传文件、发送人、解析状态、导入状态、有效行、失败行和关联批次。支持查看逐行结果、重新解析、重试导入。

### 异常处理

集中处理解析失败、订单不属于当前供应商、重复回传、快递公司无法映射、业务系统导入失败、低置信度文本解析等情况。

### 系统配置

配置业务系统 API、供应商同步 hook、订单查询默认时间窗口、Excel 模板、快递映射策略、自动导入策略和人工确认策略。

## 旧项目迁移清单

来自 `D:\trgf\trgf_order_robot` 的可迁移能力：

- `services/api_service.py` 中的订单列表、状态更新、发货文件上传接口。
- `services/excel_service.py` 中的发单 Excel 字段、文件命名、回传文件读取、按 `分单ID` 更新快递信息的思路。
- `plugins/order_handle/express_network.py` 中的快递公司和网点映射。
- `db/scripts/order_system_init.sql` 中的批次、订单明细、回传进度、操作日志模型思路。
- `plugins/order_handle/main.py` 中已验证的命令词和业务流程顺序。

不迁移或需要重写的部分：

- 旧项目硬编码 token 和测试环境配置。
- 旧项目插件入口里直接处理所有业务逻辑的结构。
- 旧项目数据表命名不一致和部分 SQL 拼接方式。
- 旧项目线程式定时任务。
- 旧项目未实现或仅占位的供应商管理命令。

## 实施阶段

### 阶段一：后台入口和供应商基础数据

- 增加插件后台最小入口。
- 新增供应商订单收发后台壳。
- 新增供应商同步配置和手动同步。
- 新增供应商与企微群绑定。

### 阶段二：手动发单 MVP

- 封装业务系统订单查询接口。
- 生成发单 Excel。
- 创建批次和订单明细。
- 发送 Excel 到企微群。
- 更新业务系统订单状态。

### 阶段三：标准 Excel 回传 MVP

- 识别企微入站 Excel 文件。
- 解析 `分单ID`、`快递公司`、`快递单号`。
- 校验供应商、批次和重复数据。
- 生成发货导入文件。
- 上传业务系统并记录结果。

### 阶段四：进度、异常和重试

- 完善批次进度页。
- 完善回传记录页。
- 增加异常处理页。
- 增加重新解析、重试导入、人工确认。

### 阶段五：自动化和非标准解析

- 增加定时发单。
- 增加未回传提醒。
- 增加文本和粘贴表格解析。
- 视业务需要再评估 OCR。

## 测试策略

1. 单元测试：快递映射、Excel 表头识别、回传行校验、文本解析。
2. 服务测试：供应商同步 upsert、批次创建、回传记录状态流转。
3. 接口测试：后台页面 GET、表单 POST、错误提示。
4. 集成测试：使用 mock 业务系统验证拉单、发单、回传、导入闭环。
5. 回归测试：企微入站文件 `download_path` / `download_url` / `file_name` 兼容已有逻辑。

## 风险和约束

1. 业务系统 token 可能过期，需要后台明显提示认证失败。
2. 供应商群绑定错误会导致发错群，发单前需要显示供应商和群确认信息。
3. 回传文件可能来自错误群，导入前必须校验分单 ID 属于当前供应商。
4. 同一分单 ID 可能重复回传，默认不覆盖已成功导入结果，除非人工确认。
5. Excel `.xls` 兼容性优先，但后续可能需要 `.xlsx` 支持。
6. 非标准文本解析不能过度自动化，避免误发货。

## 验收标准

1. 管理员可以同步供应商清单并绑定企微群。
2. 管理员可以从后台手动选择供应商发单，并在群里收到 Excel。
3. 后台可以查看发单批次、订单数、发送状态和文件记录。
4. 供应商回传标准 Excel 后，系统可以解析并调用业务系统发货导入。
5. 后台可以查看每次回传的成功、失败、重复和待确认明细。
6. 敏感配置不出现在提交文件中，日志不输出明文 token。
7. 旧项目已验证的核心接口和 Excel 字段被保留，但实现结构符合当前项目插件架构。
