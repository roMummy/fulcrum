# WeBridge 微信渠道

项目通过新增的 `wechat_webridge` 渠道接入微桥 WeBridge 8080 的服务端长轮询模式。

在渠道详情中把 WeBridge 的服务端地址配置为渠道专属基础地址，例如：

```text
https://your-domain.example/api/wechat-webridge/12
```

插件会自动访问：

- `POST /agent/register`
- `POST /agent/state`
- `POST /agent/event`
- `POST /agent/poll`
- `POST /agent/command_result`

`register` 成功后，平台保存设备、会话、微信账号和能力快照。`event` 会复用统一渠道事件入库和插件分发链路；文本出站消息进入渠道任务队列，由 `poll` 响应下发 `send_message` 命令，插件完成后通过 `command_result` 回报。

普通出站接口已映射文本、图片、表情、语音、视频、文件、链接卡片、联系人名片、位置和 XML/app 消息；朋友圈、红包、收藏、联系人高级操作等通过通用命令桥执行并保留原始结果。WebSocket 仍未接入。

入站图片、语音和视频会先按统一 `MessageAttachment` 模型落库，再通过手机执行 `upload_message_media` 回传文件。手机回传地址为 `POST /agent/upload_media`，需要携带 `X-WeBridge-Device-Id`、`X-WeBridge-Session-Id` 和 `X-WeBridge-Account-Id`，并以 multipart 字段 `file` 上传；附件完成后状态变为 `downloaded`，后续开放事件会读取服务端保存路径。

真实手机验收需要在 WeBridge 插件中启用长轮询并填写上述地址，然后验证注册、状态、测试事件、文本发送和断线恢复。当前代码验证只覆盖协议服务、目录注册、路由编译和任务边界，未代替真机验收。

## 通用命令桥

租户操作员可以通过以下接口提交微桥文档中的任意命令，命令会按渠道进入手机长轮询队列：

调用方可以先读取能力目录，按业务域生成操作菜单或自动化参数表：

```http
GET /api/wechat-webridge/{channel_id}/capabilities
```

返回值同时包含服务端稳定命令目录、手机最近一次 `register` 上报的 `capabilities` 和当前在线状态。最终是否可执行仍以手机插件实际回报为准。

```http
POST /api/wechat-webridge/{channel_id}/commands
Content-Type: application/json

{
  "command_name": "friends",
  "args": {"limit": 100},
  "request_id": "friends-20261108-001"
}
```

手机执行后使用 `command_result` 回报，平台通过下列接口查询结果：

```http
GET /api/wechat-webridge/{channel_id}/commands/{request_id}
```

该桥覆盖微桥协议中的消息、媒体、联系人、群聊、资料、红包转账、朋友圈、收藏、公众号和诊断命令。平台只对已建模的业务结果做本地落库，其余命令保留原始结果摘要，避免把插件层成功误报成 SaaS 业务完成。
