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

首期支持文本消息、在线状态、事件去重和发送结果幂等。图片、语音、视频、文件、朋友圈、红包、收藏、联系人高级操作和 WebSocket 暂未接入，调用这些能力会返回明确的不支持结果。

真实手机验收需要在 WeBridge 插件中启用长轮询并填写上述地址，然后验证注册、状态、测试事件、文本发送和断线恢复。当前代码验证只覆盖协议服务、目录注册、路由编译和任务边界，未代替真机验收。
