# WorkWechat Provider 接入说明

`workwechat` 是独立的企业微信 Hook 协议通道，不复用 `wechat_winauto`。

当前采用单入口 Windows provider：

1. 内置 Hook runtime：`hook_runtime/http_py.py` 负责 DLL 注入、socket 回调和 `/api` 指令。
2. 高层 `workwechat_provider`：负责自动拉起 Hook runtime，把主服务出站消息翻译成 Hook 指令，并把 Hook 回调翻译成主服务入站事件。
3. 云端 WS/拉取会话：企业本地 provider 主动连接云端，避免云端访问企业内网 provider。

## 说明

provider 客户端和它内置的 Hook runtime 是独立分发的 Windows 程序，**不在本仓库中**；
本文描述它与服务端之间的通信契约和配置方式。Hook 组件来自第三方，需自行获取并遵守其许可。

## 启动 workwechat provider

只需要启动 provider，provider 会自动拉起内置 `hook_runtime/http_py.py`：

```powershell
cd <provider 安装目录>
.\setup-provider.bat
.\open-config.bat
.\start-provider.bat
```

默认端口：

- provider：`http://127.0.0.1:8181`
- 内置 Hook API：`POST http://127.0.0.1:8989/api`

如果 `8989` 已经有 Hook 在监听，provider 会复用现有 Hook，不会重复启动。

注意：当前 Hook 资料包里的 DLL 是 32 位，内置 Hook runtime 需要使用 32 位 Python。provider 会优先查找常见的 Python 3.10 32 位安装路径；如果没有找到，会退回当前 Python，但这时 `/health` 可能显示 Hook degraded，日志会提示 Python 和 DLL 位数不匹配。

`config.json` 重点字段：

```json
{
  "hook_api_base": "http://127.0.0.1:8989",
  "auto_start_hook": true,
  "hook_runtime_dir": "hook_runtime",
  "hook_python_executable": "",
  "callback_url": "http://主服务地址:8180/webhooks/CHANNEL_ID/inbound",
  "callback_secret": "和渠道 Bot Token 一致",
  "provider_token": "和渠道 headers.X-Provider-Token 一致",
  "outbound_file_cache_dir": "data/outbound_file_cache"
}
```

## 主服务渠道配置

### 推荐：云端 WS/拉取模式

当主服务部署在云端、企业微信客户端和 provider 部署在企业本地时，云端一般无法直接访问企业内网服务。推荐流程：

1. 在云端后台创建 `workwechat` 渠道。
2. 进入渠道详情，展开 Provider 专项操作，点击“生成云端授权码”。
3. 企业现场只拿通用 `WorkWechatProvider.exe`，在启动器小窗口填写云端地址和授权码。
4. provider 用授权码向 `/api/workwechat/provider/register` 注册，云端按授权码匹配渠道并返回 `client_token`。
5. provider 通过 `/api/workwechat/provider/ws` 保持低延迟任务会话；断线或漏通知时通过 `/api/workwechat/provider/poll` 补偿领取任务。

客户端不需要配置渠道编码、机器人编码、Provider API Base、回调地址或回调密钥。授权码只显示一次、24 小时内使用一次；云端数据库只保存授权码哈希和 `client_token` 哈希。

主服务渠道页里的 `API Base` 是兼容旧直连 HTTP 模式的字段。使用云端 WS/拉取模式时可以留空；生成云端授权码后，渠道会自动标记为 `workwechat_delivery_mode=cloud_poll`，健康检查也不再要求 `API Base`。

本地 `config.json` 首次只需要：

```json
{
  "cloud_poll_enabled": true,
  "cloud_base_url": "https://cloud.example.com",
  "authorization_code": "wwauth_xxx"
}
```

注册成功后 provider 会保存 `client_token`，并清空一次性 `authorization_code`。客户端用本地任务锁和已处理缓存处理 WebSocket 重连、HTTP 补偿和云端重放，保证同一个任务不会重复执行。

### 兼容：直连 HTTP 模式

新增渠道：

- `channel_type`: `workwechat`
- `api_base`: `http://<provider机器IP>:8181`
- `bot_token`: 与 provider 的 `callback_secret` 一致
- `adapter_config_json`：

```json
{
  "send_path": "/send-message",
  "health_path": "/health",
  "headers": {
    "X-Provider-Token": "replace-with-provider-token"
  }
}
```

## 一期能力

- 文本发送：Hook type `11029`
- 图片发送：Hook type `11030`，支持本机可访问的 `image_path` / `file_path`，也支持主服务传入 `image_url` 后由 provider 下载到本机缓存目录再发送
- 文件发送：Hook type `11031`，支持本机可访问的 `file_path`，也支持主服务传入 `file_url` / `download_url` 后由 provider 下载到本机缓存目录再发送
- 入站文本、图片、文件、语音、视频、位置等基础消息归一化
- 入站图片、文件、视频会在主服务侧整理为 `MessageAttachment`，并把 `attachments` 快照注入插件上下文
- Webhook 签名回调
- `/health`、`/reload`、`/debug/recent-messages`

## 注意

provider 会以子进程方式启动 `hook_runtime/http_py.py`，避免 DLL 注入、`os.chdir()`、Flask 开发服务器等行为污染 provider 主进程。如果需要指定 Hook 使用的 Python，可以设置 `hook_python_executable`。
