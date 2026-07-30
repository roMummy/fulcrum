# WeChat Windows UIA Provider

独立研发的 Windows 微信 UIA Provider 骨架。

当前目标：

- 先把 provider 作为独立服务跑起来
- 明确 Agent / API / State / Transport 分层
- 为后续接入 pywinauto 与 watcher 工程化预留稳定接口
- 一期范围聚焦“左侧会话 watcher + 当前会话采集”

## 目录

```text
vendors/wchat_win_uia_provider/
  app/
    api/
    agent/
    core/
    state/
    transport/
    main.py
  requirements.txt
  run-provider.bat
  wechat-ui-agent-design.md
```

## 快速启动

```powershell
cd vendors\wchat_win_uia_provider
py -3.11 -m pip install -r requirements.txt
py -3.11 -m uvicorn app.main:app --host 127.0.0.1 --port 8070 --reload
```

或直接执行：

```powershell
.\run-provider.bat
```

如果是在微信所在桌面会话里本地联调，建议执行：

```powershell
.\run-provider-local.bat
```

这两个启动脚本现在都带 `--reload`，代码变更后会自动重启 provider。

如果希望从局域网其他机器直接访问 `212:8070`，执行：

```powershell
.\run-provider-lan.bat
```

这个脚本会绑定 `0.0.0.0:8070`。注意 212 上需要放行 Windows 防火墙的 `8070` 端口。

如果希望同时启用 provider token 鉴权，可以执行：

```powershell
.\run-provider-lan-token.bat your-uia-token
```

启用后，请求需要带：

```text
Authorization: Bearer your-uia-token
```

或者：

```text
X-Provider-Token: your-uia-token
```

如果希望直接双击启动，也可以执行：

```powershell
.\run-provider-lan-fixed-token.bat
```

当前这个脚本内置的固定 token 是：

```text
uia-212-test
```

## UIAutomation 探针

如果要单独验证 `uiautomation` 对右侧消息区的可见性，可以执行：

```powershell
.\run-probe-message-uiautomation.bat
```

探针会把结果写到：

```text
vendors/wchat_win_uia_provider/output/uiautomation_message_probe.json
```

这个文件会包含：

- `message_page_tree`
- `message_list_tree`
- `message_contexts`

适合用来核对群聊 sender 是否在右侧消息区以别的 UIA 形态暴露。

如果要按“每一条右侧消息行”做结构分析，可以执行：

```powershell
.\run-probe-message-rows-uiautomation.bat
```

结果会写到：

```text
vendors/wchat_win_uia_provider/output/uiautomation_message_rows_probe.json
```

如果要单独验证“聊天记录”入口点击后的 UIA 变化，可以执行：

```powershell
.\run-probe-chat-history-uiautomation.bat
```

结果会写到：

```text
vendors/wchat_win_uia_provider/output/uiautomation_chat_history_probe.json
```

如果聊天记录窗口已经打开，还可以继续执行：

```powershell
.\run-probe-chat-history-window-uiautomation.bat
```

结果会写到：

```text
vendors/wchat_win_uia_provider/output/uiautomation_chat_history_window_probe.json
```

如果要验证“点击右侧消息头像后，是否会出现可读的资料浮层”，可以执行：

```powershell
.\run-probe-message-avatar-click.bat
```

如果想只测试某一个候选点击点，可以传一个编号，例如：

```powershell
.\run-probe-message-avatar-click.bat 1
.\run-probe-message-avatar-click.bat 2
```

结果会写到：

```text
vendors/wchat_win_uia_provider/output/uiautomation_message_avatar_probe.json
```

## 当前接口

- `GET /health`
- `GET /runtime`
- `GET /snapshot`
- `GET /sessions`
- `GET /sessions/unread`
- `GET /current-chat`
- `GET /current-chat/incremental`
- `GET /events/recent`
- `GET /events/provider-log`
- `GET /events/session-targets?session_title=...`
- `GET /watcher/status`
- `POST /watcher/start`
- `POST /watcher/stop`
- `POST /actions/switch-session`
- `POST /actions/switch-first-unread`
- `POST /actions/fetch-first-unread-incremental`
- `POST /actions/send-text`
- `POST /actions/send-file`

## 当前状态

当前版本已经接入一期真实能力：

- 微信主窗口发现
- 左侧会话列表采集
- 左侧 watcher 事件落盘
- 当前聊天标题与当前可见消息采集
- 指定会话切换与未读会话切换调试接口
- 当前会话增量消息识别接口
- 未读发现、切换、增量读取的一体化动作接口

当前限制：

- 微信隐藏到托盘后无法继续采集
- 远程 SSH 启动的 provider 无法直接访问桌面 UI，需在微信桌面会话中本地启动
- 发送文本、发送文件仍未实现

## 一期方案

一期范围、验收标准和测试清单见：

- [phase1-plan.md](/d:/learn/multi-bot-admin/vendors/wchat_win_uia_provider/phase1-plan.md)
