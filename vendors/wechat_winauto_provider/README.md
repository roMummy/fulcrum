# WeChat WinAuto Provider

这是给 `Multi Bot Admin` 配套的 Windows provider。

它的职责很简单：

- 在 Windows 上调用 `wxauto`
- 当前优先适配 `wxautox4`，最低兼容 `wxauto4`
- 监听指定聊天
- 通过 HTTP 回调主服务入站消息
- 对外暴露一个轻量发送接口
- 支持图片和文件的本地发送、URL 下载后发送、Base64 落地后发送
- 入站图片和文件会尽量调用 `Message.download()` 下载到 provider 本机
- 监听失败会自动重试
- 保留最近收到的消息调试样本，方便联调
- 默认关闭 `GetAllMessage` 轮询兜底，优先只走监听模式，避免频繁切群
- 如果当前运行环境只提供 `GetAllMessage`，那么纯监听模式不可用，`/health` 和 `/listen-status` 会明确显示 `listener_unavailable`

## 文件

- `app.py`: FastAPI 入口
- `config.example.json`: 配置样例
- `requirements.txt`: provider 依赖
- `setup-provider.bat`: 初始化虚拟环境并安装依赖
- `start-provider.bat`: 启动 provider
- `reload-provider.bat`: 重载配置
- `open-config.bat`: 打开配置文件
- `install-and-start-provider.bat`: 一键安装并启动
- `one-click-provider.bat`: Windows 上最省事的一键安装并启动入口

## 环境要求

- Windows
- 已登录的 PC 微信
- Python 3.11+
- 建议保持桌面会话常驻，不锁屏

## 依赖说明

当前 provider 默认仍安装：

```powershell
pip install -i https://mirrors.tuna.tsinghua.edu.cn/pypi/web/simple wxauto4
```

如果你后面切换到 4.0 文档对应的付费版 `wxautox4`，provider 会优先使用它的 `AddListenChat + StartListening` 监听链路；未安装时则退回 `wxauto4`。

## 安装

```powershell
cd D:\learn\multi-bot-admin\vendors\wechat_winauto_provider
.\setup-provider.bat
```

然后编辑 `config.json`，至少改这几项：

- `callback_url`
- `callback_secret`
- `provider_token`
- `listen_chats`
- `chat_alias_map`

现在建议把 `listen_chats` 当作“种子群”而不是完整白名单：

- provider 启动时会先注册这些种子群
- 如果 `auto_expand_listen=true`，provider 还会定期从可枚举的最近会话里自动补充监听目标
- 新补充进来的群只负责“发现与上报”，真正是否纳管、是否启用由主服务后台决定

## 启动

```powershell
.\start-provider.bat
```

如果你想第一次就一把跑完：

```powershell
.\install-and-start-provider.bat
```

或者直接双击：

```powershell
.\one-click-provider.bat
```

如果 Windows 机器上还没有拉取项目代码，也可以直接运行仓库根目录的：

```powershell
.\bootstrap_wxauto_provider_from_git.bat
```

这个脚本会自动：

- `git clone` / `git pull` 项目
- 进入 `vendors\wechat_winauto_provider`
- 调用 `one-click-provider.bat`

如果你改了 `config.json`，可以执行：

```powershell
.\reload-provider.bat
```

## 调试接口

如果你配置了 `provider_token`，可以调用：

- `GET /health`
- `GET /listen-status`
- `GET /debug/recent-messages`

其中 `debug/recent-messages` 会返回最近抓到的标准化消息样本，适合联调时确认：

- 群名是否识别正确
- 发送人是否识别正确
- 图片/文件消息类型是否识别正确
- `chat_descriptor` / `chat_info` 到底返回了什么
- 回调前的 payload 长什么样

## 主服务配置

在 `Multi Bot Admin` 里新增渠道：

- `channel_type`: `wechat_winauto`
- `api_base`: `http://<windows-ip>:8060`
- `bot_token_encrypted`: 和 `callback_secret` 保持一致

`adapter_config_json` 推荐：

```json
{
  "send_path": "/send-message",
  "health_path": "/health",
  "headers": {
    "X-Provider-Token": "replace-with-provider-token"
  },
  "self_name": "我的微信",
  "listen_chats": ["测试群"],
  "chat_alias_map": {
    "group:测试群": "测试群"
  }
}
```

如果你的主服务地址是 `http://192.168.2.142:8180`，那么 `config.json` 可以先写成：

```json
{
  "provider_host": "0.0.0.0",
  "provider_port": 8060,
  "callback_url": "http://192.168.2.142:8180/webhooks/CHANNEL_ID/inbound",
  "callback_secret": "和渠道 Bot Token 一致",
  "callback_timeout_seconds": 10,
  "poll_interval_seconds": 2.0,
  "listen_chats": ["测试群"],
  "auto_expand_listen": true,
  "auto_expand_scan_interval_seconds": 30.0,
  "auto_expand_max_chats": 200,
  "chat_alias_map": {
    "group:测试群": "测试群"
  },
  "self_name": "我的微信",
  "provider_token": "replace-with-provider-token",
  "fail_on_missing_wxauto": false,
  "download_media": true,
  "media_download_dir": "downloads",
  "startup_retry_seconds": 5.0,
  "poll_error_retry_seconds": 3.0,
  "debug_dump_raw_messages": true,
  "debug_dump_dir": "debug_messages",
  "debug_recent_message_limit": 50,
  "fallback_poll_strategy": "disabled",
  "fallback_poll_chat": ""
}
```

说明：

- `CHANNEL_ID` 要换成你后台里这个 `wechat_winauto` 渠道的真实 ID
- `callback_secret` 要和该渠道的 `Bot Token` 一致
- `provider_token` 要和渠道 `adapter_config_json.headers.X-Provider-Token` 一致
- `self_name` 建议填写当前登录微信昵称，方便标记 `is_self`
- `download_media` 控制是否自动把入站图片/文件下载到本机
- `listen_chats` 现在更适合放“启动时一定要先监听的种子群”
- `auto_expand_listen=true` 时，provider 会尝试从最近会话自动扩展监听集合，不再要求每加一个群都手工改配置
- `auto_expand_scan_interval_seconds` 控制自动扫描最近会话的频率
- `auto_expand_max_chats` 控制 provider 最多维护多少个活动监听目标
- `debug_dump_raw_messages` 会把最近抓到的消息写到 `debug_messages/`
- `fallback_poll_strategy` 默认是 `disabled`，表示默认禁用 `GetAllMessage` 轮询兜底，只走监听模式
- 只有你显式改成 `pinned` 或 `rotate` 时，provider 才会启用 `GetAllMessage`
- `fallback_poll_chat` 仅在 `fallback_poll_strategy=pinned` 时生效；为空时默认取 `listen_chats` 第一个

## 说明

- 第一版只保证文本收发链路
- 当前默认优先顺序：
  - `from wxautox4 import WeChat`
  - `from wxauto4 import WeChat`
  - `ChatWith`
  - `SendMsg`
  - `SendFiles`
  - 优先 `AddListenChat + StartListening`
  - 其次 `GetListenMessage` / `GetNextNewMessage`
- `/listen-status` 现在会显示当前 `receive_mode`、实际使用的 `listener_api` 和 `last_receive_source`
- `/listen-status` 还会显示 `seed_chats`、`auto_discovered_chats` 和 `listen_failures`
- 入站标准化会优先使用 `Message` 的 `chat_info()`、`hash`、`type`、`sender` 字段
- 现在已经支持图片和文件发送，支持三种输入：
  - 本地路径：`image_path` / `file_path`
  - 远程 URL：`image_url` / `file_url` / `download_url`
  - Base64：`image_base64` / `file_base64` / `base64`
- 入站图片和文件会把 `download_path`、`file_name` 等信息放进 webhook 的 `details`
- provider 启动后如果微信暂时没准备好，会自动重试，不需要手动重启脚本
- 语音、视频仍建议后续按你的实际消息结构逐步扩展
- 如果当前环境还没安装 `wxautox4` / `wxauto4`，服务仍能起来，但发消息和监听会返回明确错误
