# WeBridge 微信渠道

项目通过 `wechat_webridge` 渠道接入微桥 WeBridge 8080（插件基础版本 8.0.80）的服务端长轮询和 WebSocket 模式。

企业管理员在「账号接入 → 微桥 WeBridge」填写账号名称，点击「生成接入地址」，再选择通信模式。系统自动生成渠道编号和随机 Token，无需手填地址或适配配置 JSON。默认长轮询地址如下（结构示意，不可直接使用）：

```text
https://your-domain.example/api/wechat-webridge/12/<随机Token>
```

点击「复制地址」，在手机插件中选择「长轮询」，将完整地址粘贴到「服务端地址」，并打开总开关。Token 在路径中，不能改为查询参数，现有插件会移除查询参数。插件会自动在完整基础地址后追加：

- `POST /agent/register`
- `POST /agent/state`
- `POST /agent/event`
- `POST /agent/poll`
- `POST /agent/command_result`

`register` 成功后，平台只保存设备、会话、微信账号和能力快照，不自动启用或占用额度。网页自动显示账号 ID、插件版本，并通过已有 `account_status` 命令补取昵称、头像；手机暂未返回资料时如实显示待获取。企业管理员核对后点击「确认信息并接入」，系统才绑定额度、启用机器人。确认前不处理业务事件，只下发账号和设备资料查询。

每个渠道的 Token 独立生成，七个手机接口（上述五个接口，以及媒体、朋友圈上传）均检查 Token；缺失或错误的 Token 无法调用，其他账号或设备不能覆盖已登记身份。通用 `/webhooks/{channel_id}/inbound` 不接受微桥消息。地址仅企业管理员可查看，离开后可从账号详情的「继续接入／查看连接地址」找回。旧的不带 Token 地址不再接受手机请求，需要由企业管理员重新复制新地址；媒体上传同步使用新地址，旧适配配置中的 `upload_url` 不再覆盖它。已删除的接入草稿不能通过旧确认页重新启用或占用额度。

地址基址优先使用 `PUBLIC_BASE_URL`，未配置时使用当前网页请求地址。局域网使用应配置为手机能访问的电脑 IP 和端口，例如 `PUBLIC_BASE_URL=http://192.168.1.10:8280`；公网部署使用自己的 HTTPS 域名。`127.0.0.1` 和 `localhost` 仅适合电脑本机访问。地址包含连接凭据，不应公开转发；服务端日志会隐藏该路径 Token，现有手机插件的地址设置、本地状态接口和部分诊断日志仍可能显示完整地址。

完成确认后，`event` 复用统一渠道事件入库和插件分发链路；文本出站消息进入渠道任务队列，由 `poll` 响应下发 `send_message` 命令，插件完成后通过 `command_result` 回报。

普通出站接口已映射文本、图片、表情、语音、视频、文件、链接卡片、联系人名片、位置和 XML/app 消息；朋友圈、红包、收藏、联系人高级操作等通过通用命令桥执行并保留原始结果。两种模式共用业务队列、账号确认和租户隔离。

入站图片、语音和视频会先按统一 `MessageAttachment` 模型落库，再通过手机执行 `upload_message_media` 回传文件。手机回传地址为带 Token 基础地址下的 `POST /agent/upload_media`，以 multipart 字段 `device_id`、`session_id`、`account_id` 提交设备身份，并以字段 `file` 上传；附件完成后状态变为 `downloaded`，后续开放事件会读取服务端保存路径。朋友圈媒体使用 `/agent/moments_upload`，除渠道 Token 外继续校验原有的一次性媒体令牌。

真实手机验收需分别选择长轮询和 WebSocket，验证注册／握手、资料查询、事件、文本发送、媒体上传和断线恢复。自动测试覆盖协议和账号流程，不代替插件真机验收。

## WebSocket 模式

接入页选择「WebSocket」并复制完整地址，在手机微桥设置中选择「WebSocket」，填入「WebSocket 服务端地址」并打开总开关。地址结构为 `wss://your-domain.example/api/wechat-webridge/12/<随机Token>/ws`。

HTTP 基址生成 `ws://`，HTTPS 基址生成 `wss://`。保留 `/ws`，不要追加 `/agent`、`/agent/poll` 或查询参数，插件会移除查询参数并剥离 `/agent` 后缀。插件没有可配置的 Authorization 请求头或 `hello.token`，本平台使用地址路径里的渠道凭据；设备、会话和账号只用于关联与核对。反向代理需透传 WebSocket Upgrade 并允许持久连接。

手机主动连接后发送 `hello`，平台回复 `hello_ack`。首次握手仅保存身份，仍须在网页确认接入；确认前只下发基础资料查询。握手期间补发的事件和结果在身份核对后处理，不要求等待 hello_ack 才上报。下行严格使用 `type:command`、`request_id`、`command_name`、`args`，不使用长轮询的 type/params 业务别名。

事件和结果分别使用 `event`、`command_result` 帧在同一连接回传，平台不发业务 ACK。完整 event_id 用于去重，结果按 request_id 关联；外层 ok 决定执行结果，失败时保留业务诊断。已无法关联的迟到历史结果不会关闭连接。同设备、同微信账号重连可更新 session_id，旧会话补发仍按设备和账号核对。

协议层 Ping/Pong 由 WebSocket 库处理。平台另外每 25 秒发送协议允许的应用层 ping，手机回 pong；超过 60 秒未收到应用层 pong 时关闭连接，手机按插件退避规则重连。iOS 挂起期间不保证持续在线。10 秒未收到 hello 或身份不匹配时使用 Close 帧关闭，不构造插件协议没有的错误帧。

单条下行帧限制为 10 MiB，超限任务在本地标为失败；聊天媒体仍通过独立 HTTP 上传字节，不把文件 Base64 塞入命令帧。断线或发送失败不会重新入队已领取命令：业务可能已经生效，应先核实未知结果。手机端长轮询与 WebSocket 互斥。本次未接入本地 HTTP 直连模式。

## 通用命令桥

租户操作员可以通过以下接口提交微桥文档中的任意命令，命令按渠道进入任务队列，由长轮询或 WebSocket 下发：

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
