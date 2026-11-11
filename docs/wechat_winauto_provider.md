# WeChat WinAuto Provider 接入方案

> provider 客户端不随本仓库发布；本文描述它与服务端之间的通信契约。

## 目标

`wxauto` 依赖 Windows 桌面微信客户端，不适合直接嵌入当前主服务。推荐采用“双进程”结构：

1. `Fulcrum` 继续作为中心服务，负责消息入库、插件分发、审计与管理。
2. 一台常驻登录微信的 Windows 机器运行 `wxauto provider`。
3. 主服务通过 `wechat_winauto` 适配器调用 provider 的 HTTP API。
4. provider 监听到新消息后，主动回调主服务 `/webhooks/{channel_id}/inbound`。

## 架构

```text
+-------------------------+          HTTP           +---------------------------+
| Fulcrum         |  <------------------>   | Windows wxauto provider   |
|                         |                         |                           |
| - channel adapter       |                         | - wxauto                  |
| - inbound normalize     |                         | - WeChat desktop client   |
| - plugin dispatch       |                         | - listen / send / health  |
+-------------------------+                         +---------------------------+
```

## 渠道配置建议

- `channel_type`: `wechat_winauto`
- `api_base`: Windows provider 地址，例如 `http://10.0.0.12:8060`
- `bot_token_encrypted`: 作为共享密钥，用于 provider 回调主服务时签名
- `adapter_config_json` 建议包含：

```json
{
  "send_path": "/send-message",
  "health_path": "/health",
  "headers": {
    "X-Provider-Token": "replace-me"
  },
  "self_name": "我的微信",
  "listen_chats": ["测试群", "内部通知"],
  "chat_alias_map": {
    "group:测试群": "测试群",
    "user:张三": "张三"
  }
}
```

## 主服务侧协议

### 出站

主服务会向 provider 发送 `POST /send-message`：

```json
{
  "channel_id": 3,
  "channel_type": "wechat_winauto",
  "conversation_id": 12,
  "conversation_external_id": "group:测试群",
  "receiver_id": "group:测试群",
  "message_type": "text",
  "content": "你好",
  "payload": {
    "content": "你好",
    "message_type": "text"
  },
  "conversation_title": "测试群",
  "chat_type": "group"
}
```

建议 provider 返回：

```json
{
  "ok": true,
  "status": "sent",
  "external_message_id": "local-1743320000",
  "message": "sent"
}
```

### 入站

provider 回调主服务 `/webhooks/{channel_id}/inbound` 时，建议直接发送统一结构：

```json
{
  "event_key": "sha1...",
  "event_type": "message",
  "conversation": {
    "external_id": "group:测试群",
    "title": "测试群",
    "chat_type": "group"
  },
  "message": {
    "content": "收到",
    "sender_id": "wxauto:张三",
    "sender_name": "张三",
    "message_type": "text",
    "external_message_id": null
  },
  "details": {
    "provider_payload_type": "friend",
    "provider_message_type": "text",
    "timestamp": "2026-03-30T14:00:00+08:00"
  }
}
```

如果 provider 暂时只能给原始字段，也可以发送简化结构，主服务适配器会兜底识别：

```json
{
  "chat_name": "测试群",
  "chat_id": "group:测试群",
  "sender": "张三",
  "sender_id": "wxauto:张三",
  "content": "收到",
  "message_type": "text",
  "timestamp": "2026-03-30T14:00:00+08:00"
}
```

## provider 最小能力范围

第一版建议只做这些：

- 监听群聊/私聊文本消息
- 发送文本消息
- 发送图片、文件消息
- 接收图片、文件消息并尽量下载到 provider 本机后转成统一事件
- 提供 `/health`
- 提供最近消息样本调试能力
- 默认禁用 `GetAllMessage` 兜底轮询，优先只走监听模式，避免频繁切群

先不要急着做：

- 语音识别
- 引用消息完整还原
- 卡片消息原样透传
- 联系人全量同步
- 多微信实例调度

## provider 实现建议

### 1. 监听

- 启动时读取 `listen_chats`
- 用 `wxauto.WeChat()` 绑定桌面微信
- 对指定会话执行监听
- 定时拉取增量消息

### 2. 消息标准化

把 `wxauto` 的消息对象统一映射为：

- `conversation.external_id`
- `conversation.title`
- `conversation.chat_type`
- `message.sender_id`
- `message.sender_name`
- `message.content`
- `message.message_type`
- `event_key`

### 3. 去重

`wxauto` 未必总能提供稳定消息 ID，建议 provider 生成：

```text
sha1(channel_id + chat_name + sender + content + timestamp + message_type)
```

### 4. 发消息

根据 `message_type` 做最小分发：

- `text` -> `SendMsg`
- `image` / `file` -> 统一走 `SendFiles`
- 如果传入 URL 或 Base64，provider 先落地到本地临时文件，再调用 `SendFiles`

### 5. 图片和文件字段约定

主服务发给 provider 时，推荐按下面三类输入之一提供：

```json
{
  "message_type": "image",
  "payload": {
    "image_path": "D:\\\\tmp\\\\demo.jpg"
  }
}
```

```json
{
  "message_type": "file",
  "payload": {
    "file_url": "https://example.com/report.pdf",
    "file_name": "report.pdf"
  }
}
```

```json
{
  "message_type": "image",
  "payload": {
    "image_base64": "data:image/jpeg;base64,...",
    "file_name": "photo.jpg"
  }
}
```

入站图片和文件回调时，`details` 中建议关注这些字段：

- `file_name`
- `file_path`
- `download_path`
- `download_error`

## 稳定性建议

- Windows 机器保持常亮，不锁屏
- 微信窗口不要频繁人工切换
- provider 作为独立进程由 NSSM / WinSW / 任务计划常驻
- provider 保留本地失败重试和最近事件日志
- 所有 UI 自动化失败都不要阻塞主服务，直接在 provider 本地告警

## 当前已补充的 provider 能力

- 监听线程自动重试
- `listen-status` 可查看最近轮询、回调、错误和失败次数
- `listen-status` 可额外确认当前是否处于 `listener_only` 模式，以及实际使用的监听 API
- `debug/recent-messages` 可查看最近抓到的标准化消息
- 可选把最近消息样本落到本地 `debug_messages/`
- 可选自动下载入站图片/文件到本机
- 新消息去重优先依赖 `Message.hash`
- 群/私聊识别优先依赖 `Message.chat_info()`

## 推荐实施顺序

1. 主服务新增 `wechat_winauto` 渠道适配器
2. Windows 上做最小 provider：`/health`、`/send-message`、消息监听回调
3. 先跑通文本收发
4. 再补图片/文件
5. 最后补联系人缓存、群别名映射和监控
