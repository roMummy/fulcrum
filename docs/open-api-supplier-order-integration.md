# Fulcrum 与收发单系统接入说明

## 目标

Fulcrum 作为消息网关，负责渠道、企业微信 Provider、任务队列和消息事件；`supplier-order-admin` 作为独立业务系统，负责供应商、批次、发单、回传解析和业务系统导入。

## Fulcrum 侧配置

开放应用属于平台方管理的机器凭据，不在企业租户后台提供创建、查看或修改入口。
平台运维人员需要在目标租户范围内通过受控初始化流程调用
`app.services.open_api_apps.upsert_open_api_app()` 创建开放应用，并向对接系统单独交付密钥：

- 应用名称：例如 `收发单系统`
- `app_key`：例如 `supplier-order`
- `app_secret`：给收发单系统调用 Fulcrum Open API 使用
- 主渠道：选择一个常用企业微信渠道，保存时会自动纳入授权渠道
- 授权渠道：勾选该业务系统可管理的所有企业微信渠道
- Webhook URL：`http://<supplier-order-host>:8190/api/mba/webhooks`
- Webhook Secret：给 Fulcrum 推事件到收发单系统时签名使用

## 可选桥接插件

Fulcrum 的 `plugins/supplier_order_flow` 现在只作为轻量桥接插件存在：

- 让插件列表和 bot 绑定里保留“供应商订单收发”这个入口。
- 识别企微群里的收发单命令、Excel 文件和文本回传候选。
- 不解析 Excel，不写供应商/批次业务表，不调用业务系统导入。
- 普通回传候选不自动回复群消息，避免和 `supplier-order-admin` 的回传结果通知重复。

单群关闭优先使用群详情页的插件作用域开关，把“供应商订单收发”设为“群内强制停用”。如果群还没进入群管理表，可以在该 bot 的插件绑定配置中增加：

```json
{
  "disabled_conversation_external_ids": ["R:需要关闭的企微群ID"]
}
```

## 收发单系统配置

`supplier-order-admin/.env` 推荐配置：

```env
MBA_BASE_URL=http://<mba-host>:8180
MBA_APP_KEY=supplier-order
MBA_APP_SECRET=<平台方单独交付的app_secret>
MBA_WEBHOOK_SECRET=<平台方单独交付的Webhook Secret>
```

## 调用 Fulcrum 发送文件

收发单系统调用：

```text
POST /api/open/files
```

请求头：

```text
X-App-Key: supplier-order
X-Timestamp: 1780804800
X-Signature: HMAC_SHA256(app_secret, timestamp + "\n" + raw_body)
```

核心请求体：

```json
{
  "request_id": "supplier-order-batch-1",
  "channel_id": 2,
  "conversation_external_id": "R:room-001",
  "conversation_title": "供应商群",
  "chat_type": "group",
  "file_name": "10单-供应商-0607.xls",
  "file_content_base64": "...",
  "content": "供应商发单批次，共10单"
}
```

`request_id` 是幂等键，重复提交不会重复入队。
`channel_id` 必填，且必须在当前 `app_key` 的授权渠道范围内。

## 调用 Fulcrum 发送普通消息

```text
POST /api/open/messages
```

核心请求体：

```json
{
  "request_id": "notice-001",
  "channel_id": 2,
  "conversation_external_id": "R:room-001",
  "conversation_title": "供应商群",
  "chat_type": "group",
  "message_type": "text",
  "content": "回传处理完成"
}
```

首版支持：

- `text`
- `image`
- `link`
- `mini_program`

文件统一走 `/api/open/files`。

## 查询任务

```text
GET /api/open/jobs/{request_id}
```

查询同样需要开放接口签名。GET 请求签名的 raw body 使用空字节。

## 查询 Fulcrum 群候选项

收发单系统绑定供应商群时可以调用：

```text
GET /api/open/chatrooms?limit=500
```

该接口只返回开放应用授权渠道下的群，字段包含：

```json
{
  "bot_id": 2,
  "channel_id": 2,
  "channel_name": "企业微信渠道",
  "chatroom_id": 8,
  "chatroom_external_id": "R:room-001",
  "chatroom_name": "供应商群",
  "chatroom_status": "active",
  "member_count": 23
}
```

## Fulcrum 推送事件到收发单系统

Fulcrum 会向 Webhook URL 推送：

- `message.received`
- `file.received`
- `message.sent`
- `message.failed`

请求头：

```text
X-MBA-Event-ID: evt_xxx
X-MBA-Timestamp: 1780804800
X-MBA-Signature: HMAC_SHA256(webhook_secret, timestamp + "\n" + raw_body)
```

`file.received` 会携带附件信息：

```json
{
  "event_type": "file.received",
  "channel": {"id": 2, "type": "workwechat"},
  "conversation": {"external_id": "R:room-001", "title": "供应商群", "chat_type": "group"},
  "sender": {"external_id": "supplier-user"},
  "message": {"id": 100, "type": "file", "external_message_id": "MSG-001"},
  "attachments": [
    {
      "file_name": "supplier-return.xls",
      "storage_path": "/srv/fulcrum/data/inbound_files/...",
      "download_url": "http://mba.example.com/inbound-files/..."
    }
  ]
}
```

同机部署时收发单系统会优先读取 `storage_path`；分机部署时使用 `download_url` 下载。

## 部署顺序

1. 启动 Fulcrum，并确认企业微信渠道可用。
2. 在 Fulcrum 为企业微信渠道生成 Provider 授权码，完成客户端注册。
3. 由平台运维人员在目标租户范围内创建收发单系统开放应用，并单独交付机器凭据。
4. 启动 `supplier-order-admin`，配置 `MBA_BASE_URL`、`MBA_APP_KEY`、`MBA_APP_SECRET`、`MBA_WEBHOOK_SECRET`。
5. 在收发单系统后台 `/admin/supplier-order/settings` 配置业务系统接口。
6. 同步供应商，维护 Fulcrum 会话绑定。
7. 创建发单批次，观察 Fulcrum 任务和收发单批次状态。
