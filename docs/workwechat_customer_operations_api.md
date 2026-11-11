# 企微客户运营 API

本文档面向外部业务系统。业务系统只需要判断“是否要推送”和“推送什么内容”，不要直接调用企微底层 Hook。

## 接口

`POST /api/workwechat/customer-operations`

示例：

```bash
curl -X POST "http://MBA_HOST/api/workwechat/customer-operations" \
  -H "Content-Type: application/json" \
  -H "X-Customer-Operation-Token: YOUR_TOKEN" \
  -d '{
    "request_id": "order-123-send-1",
    "channel_id": 3,
    "target": {
      "external_userid": "external-001"
    },
    "scene": "order_followup",
    "content": {
      "type": "text",
      "text": "您好，您的订单已经处理完成"
    },
    "biz_payload": {
      "order_id": "123"
    }
  }'
```

## 鉴权

请求头必须带：

```text
X-Customer-Operation-Token: YOUR_TOKEN
```

Token 配置在渠道的 `adapter_config_json` 中：

```json
{
  "customer_operation_token": "YOUR_TOKEN"
}
```

## 请求字段

| 字段 | 类型 | 必填 | 说明 |
| --- | --- | --- | --- |
| `request_id` | string | 是 | 业务侧唯一请求号。同一个 `channel_id + request_id` 只会入队一次。 |
| `channel_id` | number | 是 | Fulcrum 中的企微渠道 ID。 |
| `target` | object | 是 | 目标联系人标识。 |
| `target.contact_external_id` | string | 否 | Fulcrum 联系人外部 ID。 |
| `target.external_userid` | string | 否 | 企微外部联系人 ID。 |
| `target.userid` | string | 否 | 企微用户 ID。 |
| `scene` | string | 否 | 业务场景，例如 `order_followup`。 |
| `operation_type` | string | 否 | 默认 `send_message`。 |
| `content` | object/string | 是 | 推送内容。字符串会按文本消息处理。 |
| `biz_payload` | object | 否 | 业务透传信息，只记录，不参与发送判断。 |

`target` 至少提供一个有效标识。Fulcrum 会在当前渠道联系人表中查找联系人；如果联系人不存在、已删除、或缺少直聊 `conversation_id`，请求会被拒绝。

## 内容类型

### 文本

```json
{
  "type": "text",
  "text": "您好，您的订单已经处理完成"
}
```

### 图片

```json
{
  "type": "image",
  "image_url": "https://biz.example.com/assets/demo.jpg",
  "file_name": "demo.jpg"
}
```

也支持 `image_path`、`image_base64`、`file_url`、`file_path`。

### 文件

```json
{
  "type": "file",
  "file_url": "https://biz.example.com/files/order.xlsx",
  "file_name": "order.xlsx"
}
```

也支持 `file_path`、`file_base64`。

### 链接卡片

```json
{
  "type": "link_card",
  "url": "https://biz.example.com/order/123",
  "image_url": "https://biz.example.com/assets/order-card.jpg",
  "title": "查看订单详情",
  "desc": "点击查看订单处理进度"
}
```

底层映射到企微 Hook `11159`。

### 小程序卡片

```json
{
  "type": "mini_program",
  "username": "gh_demo@app",
  "appid": "wx-demo",
  "appname": "业务小程序",
  "appicon": "https://biz.example.com/assets/icon.png",
  "title": "查看订单",
  "page_path": "pages/order/detail?id=123",
  "file_id": "cdn-file-id",
  "aes_key": "cdn-aes-key",
  "md5": "cdn-md5",
  "size": 12345
}
```

底层映射到企微 Hook `11162`。小程序封面依赖 C2cCDN 参数，当前版本由业务系统提供 `file_id/aes_key/md5/size`。

## 成功返回

```json
{
  "ok": true,
  "duplicate": false,
  "request_id": "order-123-send-1",
  "operation_id": 10,
  "channel_id": 3,
  "contact_id": 22,
  "conversation_id": 31,
  "job_id": 55,
  "message_id": null,
  "status": "queued",
  "status_reason": "queued",
  "content_type": "text"
}
```

`status=queued` 表示请求已进入 Fulcrum 出站队列，不代表企微已经发送成功。后台 worker 发送后会回填运营请求状态为 `sent` 或 `failed`。

## 幂等返回

同一个 `channel_id + request_id` 重复提交，不会重复入队：

```json
{
  "ok": true,
  "duplicate": true,
  "request_id": "order-123-send-1",
  "operation_id": 10,
  "status": "queued",
  "job_id": 55
}
```

## 拒绝返回

```json
{
  "ok": false,
  "duplicate": false,
  "request_id": "order-123-send-1",
  "operation_id": 10,
  "status": "rejected",
  "status_reason": "contact_deleted"
}
```

常见 `status_reason`：

| 原因 | 说明 |
| --- | --- |
| `channel_disabled` | 渠道已禁用。 |
| `contact_not_found` | 当前渠道下找不到联系人。 |
| `contact_deleted` | 联系人已标记删除，通常是客户删除了我方账号。 |
| `contact_conversation_missing` | 联系人缺少直聊 `conversation_id`，无法发送一对一消息。 |

## HTTP 错误

| HTTP 状态 | 说明 |
| --- | --- |
| `400` | 请求字段错误、内容类型不支持、必填字段缺失。 |
| `401` | `X-Customer-Operation-Token` 错误。 |
| `404` | 渠道不存在。 |
| `503` | 渠道未配置 `customer_operation_token`。 |

## 业务边界

业务系统负责：

- 判断用户能不能推送；
- 维护活动、订单、用户状态、黑名单、频控等业务规则；
- 组装文案、链接、小程序参数和媒资参数。

Fulcrum 负责：

- 企微渠道和联系人状态校验；
- 请求幂等；
- 技术发送、失败记录和队列重试；
- 不暴露企微底层 Hook 给业务系统。
