# WorkWechat Provider 主动拉取任务设计

## 目标

让云端部署的 `Multi Bot Admin` 可以服务企业内网部署的 `vendors/workwechat_provider`。

当前直连模式要求云端访问企业电脑的 `8181` provider 端口：

```text
云端 server -> 企业内网 WorkWechatProvider:8181
```

企业电脑通常位于 NAT、防火墙或公司内网后面，云端无法直接访问。新版本改为本地 provider 主动连接云端：

```text
企业内网 WorkWechatProvider -> 云端 server
```

企业不需要开放入站端口，只需要允许 provider 访问云端 HTTPS。

## 基线

旧版本已打 tag：

```text
workwechat-direct-provider-v2026.06.07
```

该 tag 作为“云端直连企业 provider”方案的回滚点。

## 推荐方案

采用“授权码注册 + 长期 client_token + WebSocket 实时会话 + HTTP 补偿接口”。

首次连接：

```text
管理员在云端渠道页生成授权码
企业本地启动器只输入云端地址和授权码
provider 调用云端 register 接口
云端通过授权码反查绑定的 WorkWechat 渠道
云端返回 client_token、channel_id、回调配置
provider 保存 client_token 到本地 config.json
```

日常运行：

```text
provider 使用 client_token 连接云端 WebSocket
云端通过 WS 下发实时事件和任务通知
provider 本地判定是否接入、是否处理
provider 本地调用 Hook runtime 发送
provider 回传发送成功或失败
```

授权码只用于首次注册，不再长期使用。

WebSocket 是主链路，HTTP poll 作为补偿链路。这样既能降低任务延迟，也能在 WS 断线、消息丢失或客户端重启后恢复未完成任务。

客户端不需要配置渠道编号、channel_id、bot_id 或 API Base。所有渠道归属都由云端授权码决定。这样同一个客户端安装包可以发给所有企业，现场只需要填云端域名和一次性授权码。

## 授权码策略

新增两类凭证：

- `authorization_code`：一次性注册授权码。
- `client_token`：provider 注册成功后持久使用的客户端凭证。

授权码建议具备这些属性：

- 绑定单个 WorkWechat 渠道。
- 只可兑换一次。
- 有过期时间，默认 24 小时。
- 兑换成功后立即标记为已使用。
- 可由管理员重新生成，旧码失效。

`client_token` 建议具备这些属性：

- 绑定单个 WorkWechat 渠道。
- 用于 provider 轮询任务、回传结果、上传入站文件。
- 可以被管理员吊销。
- 本地保存到 `config.json`。
- 日志中必须脱敏。

## 云端接口

新增 provider 侧云端接口：

```text
POST /api/workwechat/provider/register
GET  /api/workwechat/provider/ws
POST /api/workwechat/provider/poll
POST /api/workwechat/provider/jobs/{job_id}/complete
POST /api/workwechat/provider/jobs/{job_id}/fail
```

### 注册

请求：

```json
{
  "authorization_code": "一次性授权码",
  "provider_name": "客服电脑A",
  "client_version": "5.0.0.6008"
}
```

响应：

```json
{
  "ok": true,
  "channel_id": 9,
  "client_token": "长期客户端凭证",
  "callback_url": "https://cloud.example.com/webhooks/9/inbound",
  "callback_secret": "channel-bot-token",
  "poll_interval_seconds": 3
}
```

注册请求不包含 `channel_id`。如果授权码无效、过期或已使用，云端直接拒绝注册。

### WebSocket 实时会话

连接：

```text
wss://cloud.example.com/api/workwechat/provider/ws?client_token=CLIENT_TOKEN
```

WS 用于两类消息：

- 实时事件：云端持续推送，客户端自行判断是否接入处理。
- 任务指令：云端下发待发送任务或任务可领取通知。

推荐消息结构：

```json
{
  "event_id": "ws-evt-001",
  "event_type": "job",
  "sent_at": "2026-06-07T12:00:00+08:00",
  "payload": {
    "job_id": 123,
    "job_type": "workwechat_cloud_outbound",
    "channel_id": 9,
    "conversation_id": 18,
    "conversation_external_id": "R:room-001",
    "payload": {
      "message_type": "text",
      "content": "你好"
    }
  }
}
```

客户端收到 WS 消息后先做本地幂等判定：

- `event_id` 已处理：直接忽略或回 ack。
- `job_id` 正在处理：忽略重复下发。
- `job_id` 已完成：回传已完成状态，避免云端继续重放。
- 新任务：加本地锁，执行发送，回传 complete/fail。

WS 任务下发可以直接携带完整任务，也可以只携带 `job_available` 通知。初版允许完整任务下发，但 provider 仍保留 HTTP `/poll`，用于断线补偿和启动恢复。

### 补偿拉取任务

请求头：

```text
X-WorkWechat-Client-Token: client_token
```

请求：

```json
{
  "provider_status": {
    "client_version": "5.0.0.6008",
    "hook_status": "online"
  }
}
```

响应：

```json
{
  "ok": true,
  "jobs": [
    {
      "job_id": 123,
      "channel_id": 9,
      "conversation_id": 18,
      "conversation_external_id": "R:room-001",
      "payload": {
        "message_type": "text",
        "content": "你好"
      }
    }
  ],
  "poll_interval_seconds": 3
}
```

### 回传结果

成功：

```json
{
  "ok": true,
  "provider_message_id": "ww-msg-001",
  "adapter_result": {
    "ok": true,
    "status": "sent"
  }
}
```

失败：

```json
{
  "error_code": "hook_send_failed",
  "error_message": "Hook 发送失败",
  "adapter_result": {
    "ok": false,
    "status": "failed"
  }
}
```

## 云端任务状态

复用现有 `TaskJob`，但新增主动拉取专用 job type：

- 旧直连仍使用 `job_type = "outbound_dispatch"`。
- 主动拉取使用 `job_type = "workwechat_cloud_outbound"`。
- 异步 worker 不领取 `workwechat_cloud_outbound`。
- WS 或 provider poll 接口负责领取符合条件的 pending 任务。
- complete/fail 接口负责将任务标记为 completed/failed 或 pending retry。

这样可以避免 worker 抢任务，同时复用现有 `/channels/{channel_id}/outbound`、客户运营、插件自动回复等入队路径。

## 客户端幂等和重放处理

客户端必须承担任务幂等处理，避免 WS 重连、云端重放、客服误点重启造成重复发送。

本地保存轻量状态文件：

```text
data/cloud_jobs/
  locks/
    job-123.lock
  processed.json
```

处理规则：

- 收到任务后先尝试创建 `job-{job_id}.lock`，创建成功才执行。
- lock 文件包含 `job_id`、`event_id`、`started_at`、`payload_hash`。
- 如果 lock 已存在且未超时，说明任务正在处理，忽略重复消息。
- 如果 lock 已超时，允许接管并重试，但必须先向云端 poll/confirm 当前任务状态。
- 发送成功后写入 `processed.json`，记录 `job_id`、`payload_hash`、`completed_at`、`provider_message_id`。
- 再次收到已完成 `job_id` 时不再发送，只补发 complete ack。
- `processed.json` 做容量限制，例如保留最近 5000 条或最近 7 天。

任务幂等键：

```text
job_id + payload_hash
```

如果同一个 `job_id` 的 `payload_hash` 变化，客户端应拒绝执行并回传 fail，避免任务被异常覆盖后重复发送错误内容。

云端也需要配合：

- 对已 completed 的 job 忽略重复 complete。
- 对 failed/pending 的 job 允许重试。
- 对 running 太久的 job 做超时恢复。

## 渠道配置

云端渠道新增或复用 `adapter_config_json` 字段：

```json
{
  "workwechat_delivery_mode": "cloud_poll",
  "client_token_hash": "只保存 token 哈希",
  "authorization_code_hash": "只保存授权码哈希",
  "authorization_code_expires_at": "2026-06-08T00:00:00+08:00",
  "authorization_code_used_at": null,
  "poll_batch_size": 5,
  "poll_interval_seconds": 3
}
```

`api_base` 在 cloud poll 模式下不再要求填写企业 provider 地址。

## Provider 本地配置

`config.json` 新增：

```json
{
  "cloud_poll_enabled": true,
  "cloud_base_url": "https://cloud.example.com",
  "authorization_code": "",
  "client_token": "",
  "cloud_channel_id": null,
  "provider_name": "客服电脑A",
  "poll_interval_seconds": 3,
  "poll_batch_size": 5
}
```

首次注册成功后：

- 清空本地 `authorization_code`。
- 写入 `client_token`。
- 写入云端返回的 `channel_id` 到 `cloud_channel_id`，仅用于展示和排障，不作为客户端配置输入。
- 写入云端返回的 `callback_url`、`callback_secret`。

## Provider 行为

启动流程：

1. 启动本地 provider 和 Hook runtime。
2. 如果 `cloud_poll_enabled` 为 `true`：
   - 有 `client_token`：进入轮询。
   - 没有 `client_token` 但有 `authorization_code`：先注册，再轮询。
   - 两者都没有：窗口提示“请配置授权码”。
3. 建立 WebSocket 长连接。
4. WS 收到实时事件时，本地判断是否接入处理。
5. WS 收到任务时，本地加锁并执行幂等判定。
6. 对新任务复用现有 `send_message` 的 Hook 发送逻辑。
7. 成功调用 complete。
8. 失败调用 fail。
9. WS 断线时自动重连，并使用低频 `/poll` 做补偿。

## 启动器交互

小窗口新增：

- 云端地址输入或配置展示。
- 授权码输入。
- 注册按钮。
- 连接状态：未配置 / 注册中 / 已连接 / 授权失败 / 轮询中断。

日常客服流程：

```text
首次：
1. 管理员在云端生成授权码
2. 客服打开启动器
3. 输入云端地址和授权码并点击注册
4. 显示“已连接”

以后：
1. 打开企业微信并登录
2. 启动器自动连接云端并拉取任务
```

## 安全边界

- 授权码和 client_token 不在日志明文输出。
- 云端只保存 token/code 哈希。
- 注册接口必须校验授权码未过期、未使用、绑定渠道有效。
- 轮询和回传接口必须使用 `client_token`。
- 旧的 provider HTTP `8181` 仍可保留给本地调试，但云端不依赖它。

## 兼容策略

保留旧直连模式：

```json
{
  "workwechat_delivery_mode": "direct_http"
}
```

默认新企业部署使用：

```json
{
  "workwechat_delivery_mode": "cloud_poll"
}
```

这样旧客户和本地开发不会被强制迁移。

## 测试策略

云端测试：

- 授权码生成、过期、重复兑换。
- client_token 鉴权。
- workwechat pending 任务领取。
- complete 标记任务完成。
- fail 标记任务失败或重试。
- direct_http 模式仍走旧 adapter。

Provider 测试：

- 有授权码时注册并保存 client_token。
- 有 client_token 时直接轮询。
- WebSocket 收到任务后调用现有 Hook 发送逻辑。
- 重复 event_id 不重复发送。
- 重复 job_id 不重复发送。
- lock 未超时不重复发送。
- lock 超时后按规则恢复。
- 发送成功回传 complete。
- 发送失败回传 fail。
- 授权错误时窗口显示可理解状态。

## 风险

- WS 长连接需要处理代理断开、心跳和重连。
- 客户端本地锁文件损坏时需要安全降级，不能重复发送。
- 任务被 provider 领取后如果本地断电，需要有超时恢复策略。
- 文件消息里的 `file_url` 必须是 provider 能访问的云端公网 URL。
- 本地 `config.json` 写入 token 时要避免破坏用户已有配置。
