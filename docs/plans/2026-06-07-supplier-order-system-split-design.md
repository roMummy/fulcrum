# 收发单系统拆分设计

> 当前状态：2026-06-10 已确认新收发单系统位于 `http://192.168.2.32:3000/openclaw/supplier-order-admin`，MBA 内旧 `supplier_order_flow` 业务实现应删除，不再保留业务表、业务服务或业务页面；`plugins/supplier_order_flow` 只保留轻量桥接和单群停用入口。

## 目标

把现有 `supplier_order_flow` 从 MBA 中拆成独立业务系统。MBA 继续作为机器人管理平台和消息网关，负责渠道接入、消息收发、文件收发、任务执行和事件回调；新收发单系统负责供应商、批次、订单、回传、导入、异常和客户交付后台。

## 当前问题

`supplier_order_flow` 已经不是普通插件。它包含供应商主数据、群绑定、订单拉取、Excel 生成、发单批次、回传文件解析、发货导入和后台页面。如果继续留在 MBA，会让 MBA 同时承担渠道平台和业务系统职责，后续交付客户、扩展业务规则、接 OMS/ERP 时都会变复杂。

## 系统边界

### MBA 保留

- Bot、Channel、Plugin 绑定和基础运营后台。
- 多渠道消息接入：企业微信、微信 iPad、Telegram 等。
- 企业微信 Provider 云端 WS/HTTP 任务会话。
- 入站消息、入站文件、出站消息、出站文件。
- 统一发送任务、任务状态、发送结果。
- Open API：给业务系统发消息、发文件、查任务。
- Webhook：把入站消息、入站文件、发送结果推给业务系统。
- 轻量桥接插件或事件转发配置。

### 新收发单系统承载

- 供应商同步和本地覆盖。
- 供应商与群/会话绑定。
- 发单批次、订单明细、批次进度。
- 订单拉取和业务系统状态更新。
- Excel 发单文件生成。
- 回传文件保存、解析、校验、导入。
- 回传逐行结果和异常处理。
- 业务后台页面、权限、操作日志。
- 客户交付配置，例如业务 API、MBA API、Webhook secret。

## 通信方式

### 收发单系统调用 MBA

收发单系统通过 MBA Open API 发送消息和文件，不直接调用企业微信客户端或 Provider。

建议第一版 API：

```text
POST /api/open/messages
POST /api/open/files
GET  /api/open/jobs/{request_id}
GET  /api/open/messages
```

调用方使用 `app_key/app_secret` 或固定业务 token 鉴权。第一版可以先按客户实例配置一个 MBA 接入凭证，后续再扩展多应用。

### MBA 回调收发单系统

MBA 在收到消息、文件、发送结果时，通过 Webhook 推给收发单系统。

建议事件：

```text
message.received
file.received
message.sent
message.failed
```

事件统一使用平台 envelope，避免把企业微信 Hook 原始结构直接暴露给收发单系统。

```json
{
  "event_id": "evt_xxx",
  "event_type": "file.received",
  "occurred_at": "2026-06-07T10:30:00+08:00",
  "bot": {"id": 1, "code": "company-a"},
  "channel": {"id": 1, "type": "workwechat"},
  "conversation": {
    "external_id": "R:room-001",
    "title": "供应商群",
    "chat_type": "group"
  },
  "sender": {
    "external_id": "user-001",
    "name": "张三"
  },
  "message": {
    "id": 123,
    "type": "file",
    "content": ""
  },
  "attachments": [
    {
      "file_name": "回传.xls",
      "download_url": "https://mba.example.com/inbound-files/..."
    }
  ]
}
```

## 新系统项目建议

项目名暂定：

```text
supplier-order-admin
```

建议沿用 MBA 的轻量技术栈：

- FastAPI
- SQLAlchemy
- Jinja2
- SQLite/MariaDB
- `.env` 配置
- 本地后台登录

这样可以直接迁移现有服务、模板和测试，减少重写成本。

## MBA 中的 `supplier_order_flow` 处理

拆分后，MBA 中不再保留供应商订单业务表和重业务页面。当前落地选择是彻底移除旧 `supplier_order_flow` 业务实现，只保留轻量桥接插件，避免后续维护误改 MBA 内的历史代码。

如果未来确实需要由 MBA 主动转发收发单事件，应通过通用 Open API/Webhook 或明确命名的新桥接能力实现，而不是恢复 `supplier_order_flow` 旧服务目录。

桥接插件不能做：

- 供应商绑定。
- 发单批次。
- 回传解析。
- 业务系统导入。
- 业务状态流转。

## 首版业务闭环

1. 收发单系统后台同步供应商。
2. 收发单系统后台维护供应商与 MBA 会话/群的绑定。
3. 运营在收发单系统创建发单批次。
4. 收发单系统拉取业务订单，生成 Excel。
5. 收发单系统调用 MBA Open API 发送文件到指定群。
6. MBA 通过底层渠道发送，企业微信则走云端 WS Provider。
7. MBA 把发送成功/失败回调给收发单系统。
8. 供应商在群里回传 Excel。
9. MBA 收到入站文件并回调收发单系统。
10. 收发单系统下载文件、解析、校验、导入业务系统。

## 迁移原则

- 先新增新系统和 MBA Open API，不立即删除 MBA 旧功能。
- 跑通新系统闭环后，再删除或禁用 MBA 旧收发单后台。
- 历史数据不迁移作为首版强要求；客户交付优先使用新系统新库。
- MBA 保持渠道平台定位，不继续增加收发单业务字段。
- 收发单系统不依赖企业微信细节，只依赖 MBA 统一消息 API 和事件回调。

## 风险

- MBA 现有 Open API 还偏内部，需要补通用发送和文件接口。
- 入站文件需要保证业务系统可下载，需提供签名下载或短期有效链接。
- 新系统需要重新建后台登录和客户交付配置。
- 旧插件删除前需要确认没有线上客户仍在使用旧入口。
