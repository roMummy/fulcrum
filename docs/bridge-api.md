# 桥接接口开发文档

> 文档版本：2026-11-08。本文按当前 `main` 分支的开放接口实现维护。

## 接入边界和能力清单

开放接口的目标是让业务系统直接使用已经接入平台的渠道能力。业务系统不需要实现微信扫码、代理配置、874 登录、心跳、登出或设备管理；这些动作由平台连接器和渠道服务负责。企业管理员只需要在后台购买有效套餐、创建开放应用并授权渠道，之后把应用密钥配置到自己的服务端。

当前已实现的业务接口如下：

| 能力 | 接口 | 说明 |
| --- | --- | --- |
| 账号 | `GET /api/open/accounts` | 查询应用已授权账号及其能力 |
| 账号详情 | `GET /api/open/accounts/{account_id}` | 查询单个渠道账号状态 |
| 能力发现 | `GET /api/open/accounts/{account_id}/capabilities` | 根据账号返回可用能力 |
| 联系人 | `GET /api/open/contacts`、`GET /api/open/contacts/{contact_external_id}` | 游标分页和详情查询 |
| 群 | `GET /api/open/groups` | 查询授权渠道的群快照 |
| 群成员 | `GET /api/open/groups/{external_id}/members` | 查询群成员快照 |
| 群二维码 | `GET /api/open/groups/{external_id}/qrcode` | 读取已经缓存的二维码引用 |
| 会话和群候选 | `GET /api/open/conversations`、`GET /api/open/chatrooms` | 绑定业务目标时使用 |
| 消息发送 | `POST /api/open/messages` | 文本、图片、语音、视频、表情、链接、名片、小程序和应用消息 |
| 文件发送 | `POST /api/open/files` | 通过 URL 或 Base64 创建文件发送任务 |
| 消息同步 | `GET /api/open/messages/sync` | 按消息主键游标读取增量 |
| 媒体 | `POST /api/open/media`、`GET /api/open/media/{media_id}` | 上传媒体并取得受控下载入口 |
| 任务 | `GET /api/open/jobs/{request_id}` | 查询异步发送任务 |
| 事件 | Webhook | 接收消息、文件、账号和任务事件 |

好友申请接受、群成员写入和消息撤回已经纳入能力目录，但当前版本只提供能力声明，尚未形成完整的外部异步写入路由。调用方应先读取账号能力，不要自行拼接 874 原始路径。

这组接口把 Fulcrum 已经接入的渠道能力桥接给你的业务系统。平台负责渠道连接、消息投递和任务状态；你的系统负责订单、客服、通知、分单、对账和业务重试。

文档面向已经完成企业账号、渠道和开放应用配置的开发者。这里不包含微信扫码登录、874 协议、Windows 本地代理连接器或 Provider 的实现方式。你的业务系统不需要实现登录流程，只需要调用已经授权的接口。

## 你可以用接口完成什么

- 查询已经授权给业务应用的群和会话。
- 发送文本、图片、链接、小程序和文件消息。
- 查询异步发送任务的执行状态。
- 接收入站消息事件和发送结果事件。
- 在自己的系统中保存订单、客户、处理状态和业务日志。

推荐的业务链路是：

```text
你的业务系统
    |
    | HMAC 签名请求
    v
Fulcrum /api/open/*
    |
    +--> 已授权渠道 --> 微信/企业微信等渠道
    |
    +<-- Webhook：入站消息、发送结果
```

平台不会理解你的订单状态，也不会替你实现业务规则。插件可以用来演示能力，正式业务建议由你的系统直接调用本协议。

## 前置条件

开始调用前，需要由企业管理员完成以下配置：

1. 在企业工作台接入并验证可用渠道。
2. 创建开放应用，获得 `app_key` 和 `app_secret`。
3. 把需要使用的渠道授权给该开放应用。
4. 如果需要接收事件，配置 Webhook 地址和 Webhook 密钥。

开放应用的权限边界是“授权渠道”。请求中的 `channel_id` 必须属于该应用的授权范围；业务系统不能通过修改请求字段访问其他渠道。

## 基础约定

### 服务地址

把下面的 `BASE_URL` 替换成你的 Fulcrum 服务地址：

```text
BASE_URL=https://your-saas.example.com
```

接口路径都以 `/api/open` 开头。例如：

```text
GET https://your-saas.example.com/api/open/chatrooms
```

### 常用资源

| 字段 | 作用 |
| --- | --- |
| `app_key` | 业务应用的公开标识，放在请求头中 |
| `app_secret` | 业务应用签名密钥，只保存在你的服务端 |
| `channel_id` | 已授权的渠道 ID，发送时必须明确指定 |
| `conversation_external_id` | 渠道中的群或会话外部 ID，发送时作为目标 |
| `chat_type` | 会话类型，通常使用 `group` 或 `private` |
| `request_id` | 你的业务请求幂等键，同一业务操作重试时必须保持不变 |
| `job` | 平台异步任务及其执行状态 |

`channel_id` 和 `conversation_external_id` 可以通过群查询接口获得。不要把数据库自增 ID 当成业务系统的订单号或外部会话标识。

## 鉴权：HMAC-SHA256

每个 `/api/open/*` 请求都必须携带以下请求头：

```text
X-App-Key: your-app-key
X-Timestamp: 1762560000
X-Signature: <hex encoded HMAC-SHA256>
```

如果同一个 `app_key` 在多个租户中重复使用，还需要携带：

```text
X-Tenant-Code: your-tenant-code
```

签名原文固定为：

```text
timestamp + "\n" + 原始请求体
```

其中：

- `timestamp` 使用 Unix 秒级时间戳。
- GET 请求没有请求体，签名原文就是 `timestamp + "\n"`。
- POST 请求必须对最终实际发送的原始 JSON 字节签名；签名后不要再次改变 JSON 空格、换行或字段顺序。
- 服务端只接受约 5 分钟内的时间戳。
- `X-Signature` 是 HMAC-SHA256 的十六进制小写字符串。

### Python 签名函数

```python
import hashlib
import hmac
import time


def sign_request(app_secret: str, raw_body: bytes) -> dict[str, str]:
    timestamp = str(int(time.time()))
    message = timestamp.encode("utf-8") + b"\n" + raw_body
    signature = hmac.new(
        app_secret.encode("utf-8"),
        message,
        hashlib.sha256,
    ).hexdigest()
    return {
        "X-App-Key": "your-app-key",
        "X-Timestamp": timestamp,
        "X-Signature": signature,
    }
```

### Python 请求示例

```python
import json
import requests


BASE_URL = "https://your-saas.example.com"
APP_SECRET = "只放在你的服务端"
payload = {
    "channel_id": 3,
    "conversation_external_id": "group:供应商群",
    "conversation_title": "供应商群",
    "chat_type": "group",
    "message_type": "text",
    "content": "订单已创建",
    "request_id": "order-20261108-0001",
}
raw_body = json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
headers = sign_request(APP_SECRET, raw_body)
response = requests.post(
    f"{BASE_URL}/api/open/messages",
    data=raw_body,
    headers={**headers, "Content-Type": "application/json"},
    timeout=15,
)
response.raise_for_status()
print(response.json())
```

## 第一步：查询可用群

### 查询群列表

```http
GET /api/open/chatrooms?q=供应商&limit=100
```

查询参数：

- `q`：可选，按群外部 ID、群名称、渠道名称或渠道 ID 模糊过滤。
- `limit`：可选，默认 `500`，最大 `1000`。

响应示例：

```json
{
  "ok": true,
  "items": [
    {
      "bot_id": 2,
      "channel_id": 3,
      "channel_name": "企微客服号",
      "channel_type": "workwechat",
      "chatroom_id": 56,
      "chatroom_external_id": "group:供应商群",
      "chatroom_name": "供应商群",
      "chatroom_remark": "供应商对接",
      "chatroom_status": "active",
      "management_status": "managed",
      "is_active": true,
      "member_count": 18
    }
  ]
}
```

发送消息时使用 `channel_id` 和 `chatroom_external_id`，不要依赖 `chatroom_id` 拼接目标。

### 查询会话列表

```http
GET /api/open/conversations?chat_type=group&q=供应商&limit=100
```

该接口把消息会话和群快照合并返回，适合需要同时处理私聊和群聊的业务系统。返回字段包括：

- `channel_id`、`channel_name`、`channel_type`
- `conversation_id`、`conversation_external_id`、`conversation_title`
- `chat_type`
- 如果能匹配群快照，还会返回 `chatroom_id`、`chatroom_name` 和 `chatroom_status`

## 发送文本、图片、链接和小程序

### 请求

```http
POST /api/open/messages
Content-Type: application/json
```

请求体：

```json
{
  "request_id": "order-20261108-0001",
  "channel_id": 3,
  "conversation_external_id": "group:供应商群",
  "conversation_title": "供应商群",
  "chat_type": "group",
  "message_type": "text",
  "content": "订单已创建，请查收。"
}
```

`message_type` 支持：

| 类型 | 必填字段 | 可选字段 |
| --- | --- | --- |
| `text` | `content` | 无 |
| `image` | `image_url` | `content` |
| `link` | `link_url` | `link_title`、`link_description`、`thumb_url`、`content` |
| `mini_program` | 由渠道适配器支持的 `mini_program_*` 字段 | `content` |

发送接口会立即创建异步任务，不代表消息已经到达目标会话。请保存返回的 `request_id` 和 `job.id`，再通过任务接口或 Webhook 确认结果。

成功响应示例：

```json
{
  "ok": true,
  "queued": true,
  "idempotent": false,
  "request_id": "order-20261108-0001",
  "channel_id": 3,
  "conversation_id": 56,
  "job": {
    "id": 123,
    "job_type": "workwechat_cloud_outbound",
    "status": "pending",
    "attempts": 0,
    "max_attempts": 3,
    "available_at": "2026-11-08T10:00:00+08:00",
    "started_at": null,
    "finished_at": null,
    "error_message": null,
    "result": null
  }
}
```

### 幂等重试

同一个开放应用下，如果再次提交相同的 `request_id`，服务端会返回原任务：

```json
{
  "ok": true,
  "idempotent": true,
  "request_id": "order-20261108-0001",
  "job": {"id": 123, "status": "completed"}
}
```

网络超时后请复用原来的 `request_id`，不要随机生成新值，否则可能造成重复发送。

## 发送文件

### 请求

```http
POST /api/open/files
Content-Type: application/json
```

接口接受以下三种文件来源之一：

- `file_url`：平台可以访问的 HTTP/HTTPS 地址。
- `file_content_base64`：文件内容的 Base64 字符串，适合小文件。
- `file_path`：运行 Fulcrum 服务的机器上可读的本地路径；这不是用户开发机上的路径。

请求示例：

```json
{
  "request_id": "order-file-20261108-0001",
  "channel_id": 3,
  "conversation_external_id": "group:供应商群",
  "conversation_title": "供应商群",
  "chat_type": "group",
  "file_name": "order.xlsx",
  "file_url": "https://files.example.com/order.xlsx",
  "content": "订单文件，请查收。"
}
```

如果使用 Base64：

```json
{
  "request_id": "order-file-20261108-0002",
  "channel_id": 3,
  "conversation_external_id": "group:供应商群",
  "file_name": "order.xlsx",
  "content_type": "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
  "file_content_base64": "UEsDB..."
}
```

平台会把文件保存为受控下载地址，再创建异步出站任务。响应中会返回 `file_url` 和 `job` 信息。

## 查询任务状态

可以使用业务 `request_id` 查询：

```http
GET /api/open/jobs/order-20261108-0001
```

响应示例：

```json
{
  "ok": true,
  "request_id": "order-20261108-0001",
  "job": {
    "id": 123,
    "job_type": "workwechat_cloud_outbound",
    "status": "completed",
    "attempts": 1,
    "max_attempts": 3,
    "available_at": "2026-11-08T10:00:00+08:00",
    "started_at": "2026-11-08T10:00:01+08:00",
    "finished_at": "2026-11-08T10:00:03+08:00",
    "error_message": null,
    "result": {
      "adapter_result": {"ok": true, "status": "sent"}
    }
  }
}
```

常见状态：

- `pending`：任务已经创建，等待执行。
- `running`：任务正在交给渠道执行器。
- `completed`：平台任务完成；仍应结合 `result.adapter_result` 或发送结果事件判断渠道是否确认送达。
- `failed`：平台执行失败，查看 `error_message` 和 `result`。

任务查询按 `app_key + request_id` 隔离。不存在时返回 `404 job_not_found`。

## Webhook 事件

如果开放应用配置了 Webhook，平台会异步投递入站消息和开放接口发送结果。

请求头：

```text
Content-Type: application/json
X-MBA-Event-ID: evt_message_received_123_1762560000000
X-MBA-Timestamp: 1762560000
X-MBA-Signature: <hex encoded HMAC-SHA256>
```

Webhook 签名同样使用：

```text
timestamp + "\n" + 原始请求体
```

签名密钥使用开放应用配置的 Webhook 密钥。业务系统应该先校验时间戳和签名，再按 `X-MBA-Event-ID` 做幂等处理。

### 入站消息事件

```json
{
  "event_id": "evt_message_received_101_1762560000000",
  "event_type": "message.received",
  "occurred_at": "2026-11-08T10:00:00+08:00",
  "bot": {"id": 2},
  "channel": {"id": 3, "type": "workwechat"},
  "conversation": {
    "id": 56,
    "external_id": "group:供应商群",
    "title": "供应商群",
    "chat_type": "group"
  },
  "sender": {"external_id": "wxid:zhangsan", "name": "张三"},
  "message": {
    "id": 101,
    "type": "text",
    "content": "订单已发出",
    "external_message_id": "msg-001",
    "status": "received"
  },
  "attachments": []
}
```

### 发送结果事件

发送结果事件会带上原始业务 `request_id`：

```json
{
  "event_id": "evt_message_sent_123_1762560000000",
  "event_type": "message.sent",
  "occurred_at": "2026-11-08T10:00:03+08:00",
  "open_api": {
    "app_key": "order-system",
    "request_id": "order-20261108-0001",
    "job_id": 123
  },
  "channel": {"id": 3, "type": "workwechat"},
  "conversation": {
    "id": 56,
    "external_id": "group:供应商群",
    "title": "供应商群",
    "chat_type": "group"
  },
  "message": {
    "id": 102,
    "type": "file",
    "content": "",
    "external_message_id": "msg-002",
    "status": "sent"
  },
  "delivery": {
    "status": "sent",
    "confirmed": true,
    "external_message_id": "msg-002",
    "error_code": "",
    "error_message": "",
    "error_summary": "",
    "requires_attention": false
  },
  "job": {
    "id": 123,
    "status": "completed",
    "error_message": "",
    "error_summary": "",
    "delivery_status": "sent",
    "delivery_confirmed": true
  }
}
```

平台 Webhook 投递失败会进入后台重试。你的接口应尽快返回 `2xx`，不要在回调请求里执行长时间订单处理；收到事件后先落库，再异步执行自己的业务流程。

## 常见错误

| HTTP 状态 | `detail` | 处理方式 |
| --- | --- | --- |
| `400` | `request_id_required` | 提供稳定的业务幂等键 |
| `400` | `channel_id_required` | 指定发送渠道 |
| `400` | `conversation_external_id_required` | 指定目标会话外部 ID |
| `400` | `unsupported_message_type` | 使用当前支持的消息类型 |
| `401` | `missing_app_key` / `invalid_signature` | 检查请求头、密钥和原始请求体 |
| `401` | `timestamp_expired` | 使用当前时间重新签名 |
| `403` | `channel_not_allowed` | 让管理员把渠道授权给当前开放应用 |
| `404` | `channel_not_found` / `job_not_found` | 检查渠道状态或业务请求 ID |
| `503` | `tool_api_key_not_configured` | 仅适用于兼容工具接口，联系平台管理员 |

错误响应通常是：

```json
{"detail": "invalid_signature"}
```

不要根据错误文案推断平台内部数据库结构；业务系统应按 HTTP 状态和稳定 `detail` 处理失败。

## 兼容工具接口

已有内部工具可以继续使用 `/api/tool/*`：

- `POST /api/tool/channels/{channel_id}/upload-and-send`
- `GET /api/tool/channels/{channel_id}/messages`
- `GET /api/tool/jobs/{job_id}`

这组接口使用全局 `X-API-Key`，并要求 `X-Tenant-Code` 指定租户。它适合已经接入的内部工具；新开发项目推荐使用按开放应用隔离的 `/api/open/*`，避免多个业务系统共享全局密钥。

## 推荐的业务系统结构

```text
业务 API / 管理后台
    |
    +-- 订单表、客户表、业务状态表
    +-- outbox 表：保存待发送 request_id 和消息内容
    +-- Webhook 接收器：验签、落库、快速返回 2xx
    +-- 发送 worker：调用 /api/open/messages 或 /api/open/files
    +-- 状态 worker：处理 Webhook，必要时查询 /api/open/jobs/{request_id}
```

建议：

1. 每个业务动作生成稳定的 `request_id`，重试时复用。
2. 发送请求和你的业务状态写入 outbox，避免用户请求线程直接承担渠道耗时。
3. Webhook 先验签和落库，再异步更新订单或客服状态。
4. 对 `message.sent` 中 `delivery.confirmed=false` 的事件进行人工关注，不要自动无限重发。
5. 自己保存 `channel_id` 和 `conversation_external_id` 的映射，群名变化时重新查询群列表。

## 账号、联系人和群资源

所有资源查询都只返回开放应用已经授权的渠道。资源接口使用数据库快照，返回的是稳定业务字段，不会返回设备指纹、874 原始响应、登录票据或代理信息。

### 账号和能力

```http
GET /api/open/accounts
GET /api/open/accounts/{account_id}
GET /api/open/accounts/{account_id}/capabilities
```

账号对象示例：

```json
{
  "id": 3,
  "name": "企微客服号",
  "channel_type": "workwechat",
  "status": "active",
  "login_state": "online",
  "last_login_at": "2026-11-08T10:00:00+08:00",
  "last_message_sync_at": "2026-11-08T10:00:03+08:00",
  "capabilities": [
    {
      "code": "message.send.text",
      "title": "发送文本消息",
      "operation": "write",
      "status": "available"
    }
  ]
}
```

### 联系人

```http
GET /api/open/contacts?channel_id=3&cursor=0&limit=50&q=张
GET /api/open/contacts/wxid:zhangsan?channel_id=3
```

列表响应使用 `next_cursor`。当没有下一页时返回 `null`。`contact_external_id` 是渠道外部标识，业务系统应保存它，不要使用内部 `id` 作为跨系统主键。

### 群和群成员

```http
GET /api/open/groups?channel_id=3&cursor=0&limit=50&q=供应商
GET /api/open/groups/group:供应商群/members?channel_id=3&cursor=0&limit=100
GET /api/open/groups/group:供应商群/qrcode?channel_id=3
```

群二维码接口只读取已经缓存的二维码。如果平台没有缓存数据，会返回 `503 group_qrcode_unavailable`；这不表示账号离线，也不应被业务系统当作登录入口。

## 消息同步

```http
GET /api/open/messages/sync?channel_id=3&cursor=1000&limit=100
```

响应中的 `next_cursor` 是本次返回最后一条消息的内部游标，下一次请求直接传回即可。消息对象包含 `id`、`external_id`、`channel_id`、`conversation_external_id`、`chat_type`、`direction`、`message_type`、`content`、`status` 和 `created_at`。游标同步只读取开放应用授权渠道范围内的数据。

## 媒体上传和下载

### 上传媒体

```http
POST /api/open/media
Content-Type: application/json
```

请求体使用 `file_content_base64` 提交内容：

```json
{
  "request_id": "media-20261108-0001",
  "channel_id": 3,
  "file_name": "报价单.pdf",
  "content_type": "application/pdf",
  "media_type": "file",
  "file_content_base64": "JVBERi0xLjQK..."
}
```

成功响应会返回 `media_id`、文件大小和 `url`。公共媒体接口明确拒绝 `file_path`，不能借此读取网关服务器任意本地文件。需要发送文件时，可以直接使用 `/api/open/files` 的 `file_url` 或 Base64 字段，也可以先上传媒体再把返回 URL 作为消息内容的一部分。

### 取得受控下载入口

```http
GET /api/open/media/{media_id}?channel_id=3&file_name=报价单.pdf
```

响应：

```json
{
  "ok": true,
  "media_id": "a1b2c3...",
  "download_url": "https://your-saas.example.com/outbound-files/3/a1b2c3.../%E6%8A%A5%E4%BB%B7%E5%8D%95.pdf"
}
```

业务系统应保存上传响应中的完整文件名和 `media_id`，不要自行改写下载路径。下载入口仍受租户和渠道目录约束。

## 套餐、额度和幂等

开放 API 使用当前租户的有效订阅。套餐包含以下独立额度：

| 字段 | 含义 |
| --- | --- |
| `open_api_app_limit` | 可启用的开放应用数量 |
| `open_api_channel_limit` | 所有开放应用合计授权渠道数量 |
| `monthly_api_request_limit` | 自然月 API 请求次数 |
| `monthly_message_limit` | 自然月消息发送次数 |
| `monthly_media_bytes_limit` | 自然月媒体字节数 |

额度按北京时间自然月统计。配置为 `0` 表示该项不设上限。发送消息、发送文件和上传媒体会在请求事务内预扣额度；任务明确失败时由任务链路返还，成功则确认使用。相同开放应用、同一自然月、同一 `request_id` 的重复请求只会命中原预扣记录，不会重复扣费。

需要重试时必须复用原 `request_id`。查询类接口如果没有业务幂等键，可以使用 `X-Request-ID`；发送和上传接口必须在 JSON 中提交 `request_id`。

额度或订阅失败示例：

```json
{"detail": "subscription_required"}
```

```json
{"detail": "quota_exceeded"}
```

常见门禁状态码：`402` 表示需要有效订阅或额度不足，`403` 表示应用、能力或渠道不允许，`409` 表示幂等预扣冲突，`429` 仅在网关频率策略启用时使用。不要解析 874 原始错误文本。

## Webhook 事件目录

应用配置 Webhook 后，平台异步投递以下稳定事件：

`message.received`、`message.sent`、`message.failed`、`file.received`、`account.online`、`account.offline`、`account.reconnected`、`job.completed`、`job.failed`。

每个事件都有 `event_id`、`event_type`、`occurred_at`、`channel`、`conversation` 等统一字段。`event_id` 是确定性的，接收方必须先验签、落库并按 `event_id` 去重，再异步执行业务处理。投递失败由平台后台重试，不应阻塞你的 HTTP 接收线程。

Webhook 请求头：

```text
X-MBA-Event-ID: evt_message.sent_3_102
X-MBA-Timestamp: 1762560000
X-MBA-Signature: <hex encoded HMAC-SHA256>
```

签名原文仍然是 `timestamp + "\n" + 原始请求体`，密钥是开放应用的 Webhook 密钥。发送结果事件的 `open_api.request_id` 用来关联你的订单或 outbox 记录。

## 支付成功自动履约

套餐购买可以通过平台现有订单流程完成。若接入第三方支付自动回调，支付平台向以下地址发送 JSON：

```http
POST /api/payment/webhook
X-Payment-Timestamp: 1762560000
X-Payment-Signature: <hex encoded HMAC-SHA256>
Content-Type: application/json
```

签名原文是 `timestamp + "\n" + 原始请求体`，服务端只接受约 5 分钟内的时间戳。回调体至少包含：

```json
{
  "status": "paid",
  "order_no": "ORD-20261108-0001",
  "amount_cents": 9900,
  "transaction_id": "provider-tx-001"
}
```

平台会校验订单号、订单金额和支付状态，并在同一业务事务内创建或续期订阅。重复回调返回原订单履约结果，不会重复延长订阅。服务端通过 `PAYMENT_WEBHOOK_SECRET` 配置回调密钥；密钥只放在部署环境，不写入开放应用配置和业务日志。

## 不开放的内部接口

以下接口不属于对外桥接协议，业务系统不要调用：

- `/api/open` 之外的 874 原始端点、扫码登录、代理、心跳和设备控制接口。
- `/api/tool/*` 全局工具接口。它仍用于历史内部工具，使用全局 `X-API-Key`，新项目应使用按开放应用隔离的 `/api/open/*`。
- 本地代理连接器的激活、心跳和 WebSocket 接口。连接器登录完成后即可退出，后续业务由平台已建立的渠道连接执行。

