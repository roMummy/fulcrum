# Tool API For OSM

这组接口给第三方业务系统使用，例如 OSM。`Fulcrum` 只作为微信工具层，不承接业务逻辑。

## 鉴权

所有接口都使用：

```text
X-API-Key: <TOOL_API_KEY>
```

服务端需要在 `.env` 中配置：

```env
TOOL_API_KEY=change-this-tool-api-key
```

如果没有配置，会返回：

```json
{"detail":"tool_api_key_not_configured"}
```

## 1. 上传并发送文件到指定群

`POST /api/tool/channels/{channel_id}/upload-and-send`

使用 `multipart/form-data`

字段：

- `file`: 必填，上传的文件，例如 `report.xlsx`
- `external_id`: 必填，目标群或会话标识，例如 `group:测试群`
- `conversation_title`: 可选，会话标题，例如 `测试群`
- `chat_type`: 可选，默认 `group`
- `message_type`: 可选，默认 `file`，可选 `file` 或 `image`

示例：

```bash
curl -X POST "https://fulcrum.example.com/api/tool/channels/3/upload-and-send" \
  -H "X-API-Key: your-tool-api-key" \
  -F "external_id=group:测试群" \
  -F "conversation_title=测试群" \
  -F "chat_type=group" \
  -F "message_type=file" \
  -F "file=@report.xlsx"
```

成功返回：

```json
{
  "ok": true,
  "queued": true,
  "job_id": 123,
  "channel_id": 3,
  "conversation_id": 56,
  "external_id": "group:测试群",
  "message_type": "file",
  "file_name": "report.xlsx",
  "file_size": 10240,
  "download_url": "https://fulcrum.example.com/outbound-files/3/abc123/report.xlsx",
  "payload": {
    "external_id": "group:测试群",
    "message_type": "file",
    "file_name": "report.xlsx",
    "chat_type": "group",
    "title": "测试群",
    "file_url": "https://fulcrum.example.com/outbound-files/3/abc123/report.xlsx"
  }
}
```

## 2. 查询发送任务状态

`GET /api/tool/jobs/{job_id}`

示例：

```bash
curl "https://fulcrum.example.com/api/tool/jobs/123" \
  -H "X-API-Key: your-tool-api-key"
```

返回示例：

```json
{
  "ok": true,
  "job": {
    "id": 123,
    "job_type": "outbound_dispatch",
    "status": "completed",
    "payload": {},
    "result": {
      "message_id": 999,
      "adapter_result": {
        "ok": true,
        "status": "sent"
      }
    },
    "error_message": null
  }
}
```

重点字段：

- `status`: `pending` / `running` / `completed` / `failed`
- `result.adapter_result.ok`: 是否真正发送成功
- `error_message`: 失败原因

## 3. 查询群消息

`GET /api/tool/channels/{channel_id}/messages`

查询参数：

- `external_id`: 可选，会话标识，例如 `group:测试群`
- `after_id`: 可选，只返回大于该消息 ID 的消息
- `limit`: 可选，默认 `50`，最大 `200`

示例：

```bash
curl "https://fulcrum.example.com/api/tool/channels/3/messages?external_id=group:测试群&after_id=1000&limit=100" \
  -H "X-API-Key: your-tool-api-key"
```

返回示例：

```json
{
  "ok": true,
  "channel_id": 3,
  "external_id": "group:测试群",
  "after_id": 1000,
  "last_id": 1012,
  "count": 2,
  "messages": [
    {
      "id": 1011,
      "conversation_id": 56,
      "conversation_external_id": "group:测试群",
      "conversation_title": "测试群",
      "chat_type": "group",
      "direction": "in",
      "sender_id": "wxauto:张三",
      "sender_name": "张三",
      "content": "订单123 对应单号 SF123456",
      "message_type": "text",
      "status": "received",
      "created_at": "2026-03-30T08:00:00+00:00"
    }
  ]
}
```

建议 OSM 使用方式：

1. 记录自己最后消费到的 `last_id`
2. 下次查询时带 `after_id`
3. 只拉取指定群 `external_id`
4. 自己在 OSM 内部解析单号并回写业务系统

## 推荐对接流程

1. OSM 定时导出订单 Excel
2. 调用 `upload-and-send` 发到指定群
3. 记录返回的 `job_id`
4. 调用 `jobs/{job_id}` 检查是否真正发送成功
5. OSM 定时轮询 `messages`
6. 解析群回执单号
7. 回写 OSM 自己的订单系统

## 说明

- `Fulcrum` 不理解 OSM 订单语义
- `Fulcrum` 只负责文件发送、消息记录和查询
- 业务状态、重试、对账、单号解析都建议由 OSM 自己维护
