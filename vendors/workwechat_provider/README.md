# WorkWechat Provider

这是 `Multi Bot SaaS` 的企业微信 Hook 高层 Provider。

它不复用 `wechat_winauto`，对应主服务中的独立渠道类型：

```text
workwechat
```

## 职责

- 接收主服务 `POST /send-message`
- 转换成 Hook 文档里的 `type + data` 指令
- 自动拉起内置 Hook runtime，并调用低层 Hook HTTP：`POST /api`
- 接收低层 Hook 回调：`POST /hook-callback`
- 归一化后签名回调主服务 `/webhooks/{channel_id}/inbound`

## 运行

只需要启动本 provider，provider 会自动拉起 `hook_runtime/http_py.py`：

```powershell
cd D:\learn\multi-bot-admin\vendors\workwechat_provider
.\setup-provider.bat
.\open-config.bat
.\start-provider.bat
```

如果 `8989` 已经有 Hook 在监听，provider 会复用现有 Hook；否则会自动启动内置 Hook。

注意：Hook DLL 是 32 位，内置 Hook runtime 需要 32 位 Python。没有 32 位 Python 时 provider 仍可启动，但 Hook 注入会失败，`/health` 会显示 degraded。

## 小窗口启动器

面向客服同事的推荐交付方式是打包 `WorkWechatProvider.exe` 小窗口启动器。

客服每天上班只需要：

```text
1. 打开企业微信并登录
2. 双击 WorkWechatProvider.exe
3. 首次使用时在小窗口填写云端地址和节点激活码，点击“保存激活码”
4. 启动器会自动检测企业微信并启动 provider/hook
5. 小窗口显示“运行正常”后最小化到托盘
```

启动器会在 Hook 启动前检查：

- 企业微信进程是否存在，默认进程名是 `WXWork.exe`
- 企业微信客户端文件版本是否等于 `required_workwechat_version`
- provider 健康接口 `/health` 是否正常
- Hook runtime 是否正常响应

默认要求企业微信版本：

```json
{
  "required_workwechat_version": "5.0.0.6008",
  "workwechat_process_name": "WXWork.exe"
}
```

如果版本不匹配，小窗口会提示当前版本和要求版本，并阻止 Hook 注入。客服只需要运行 `WorkWechatProvider.exe`；provider server、Hook runtime 和 Hook DLL 都内置在这个 exe 里，启动时先从 PyInstaller 临时资源复制到 `%LOCALAPPDATA%\WorkWechatProvider\runtime\<build>`，再由启动器自动管理。

### 已确认的启动器策略

这些策略是 Windows 现场调优后的约束，后续打包或重构不要回退：

- 客服侧只交付一个 `WorkWechatProvider.exe`；`WorkWechatProviderServer.exe`、`WorkWechatHookRuntime.exe` 只能作为内置资源被 UI exe 解包运行，不能作为客服入口。
- 启动器检测到 `WXWork.exe` 已运行且版本正确时，必须自动进入 provider/hook 启动流程，不要求客服再点一次启动。
- Hook runtime 调用 `InjectWeChat` 失败后，必须先检测是否已有 `WXWork.exe`；如果已有企业微信进程，不允许继续调用会拉起新客户端的 `InjectWeChatMultiOpen`，避免重复打开企业微信。
- 客户端版本显示先读进程 `exe` 路径；读不到时必须从常见安装目录兜底，例如 `C:\Program Files (x86)\WXWork\WXWork.exe`。UI 健康轮询刷新时要保留并刷新版本字段，不能把版本覆盖回“未知”。
- UI 全局字体使用 `Microsoft YaHei UI`；窗口图标、托盘图标和标题区 LOGO 使用内置 Qt 绘制，不依赖额外图片资源。
- UI 只允许客服填写云端地址和节点激活码；不要再让客服填写渠道编号、机器人编号、Provider API Base 或回调密钥。
- 桌面 exe 图标由 `generate_launcher_icon.py` 生成到 `assets\workwechat-provider.ico`，最终 UI 打包命令必须带 `--icon`，避免桌面仍显示 PyInstaller 默认图标。
- PyInstaller onefile 解包到 `_MEI...` 临时目录是正常行为，不代表交付了多个文件；交付目录仍只应暴露 `WorkWechatProvider.exe`。启动器会把内部 server/runtime 复制到 `%LOCALAPPDATA%\WorkWechatProvider\runtime\<build>`，并把 `config.json`、`logs`、`data` 放到 `%LOCALAPPDATA%\WorkWechatProvider`。
- 若打包日志继续提示 `tencent.mm.pbc.dll` 缺失，要保留为 Hook 注入稳定性的待排查项；目标机器注入异常时优先补齐该 DLL 并加入打包命令。

### 打包

打包机需要安装 Python 3.10 32 位。客服机器不需要安装 Python。

在 Windows 打包机执行：

```bat
cd vendors\workwechat_provider
build-provider-launcher.bat
```

产物位置：

```text
dist\WorkWechatProvider.exe
```

交付文件：

```text
WorkWechatProvider.exe
```

首次运行时会在 `%LOCALAPPDATA%\WorkWechatProvider` 生成 `config.json`、`logs` 和 `data`，UI 的“打开配置/打开日志”会直接定位到该目录；如果旧版本已经在 `WorkWechatProvider.exe` 同级生成过 `config.json`，新版本会自动迁移一份到稳定目录。

注意：打包可以简化操作，也可以避免客服安装 Python；但 DLL 和内部 exe 运行时仍会被 PyInstaller 解包到临时目录供 Windows 加载，不能承诺完全不可见或不可逆向。

打包脚本需要保持最终 `dist` 目录只暴露 `WorkWechatProvider.exe`。内部 server/runtime exe 在最终 UI exe 打包完成后要删除，避免交付时误拿错入口。

## 主服务配置

### 推荐：节点心跳和长轮询模式

企业本地部署 provider、主服务部署在云端时，云端通常不能直接访问企业内网的 `127.0.0.1:8181` 或局域网 provider 地址。推荐使用云端 WS/拉取模式：

```text
1. 租户管理员在 Provider 节点页面创建节点并生成一次性激活码
2. 把云端域名和激活码交给企业现场客服
3. 客服在 WorkWechatProvider.exe 小窗口里填写云端地址和激活码
4. Provider 首次连接云端后用激活码换取节点设备密钥
5. Provider 持续上报心跳，并通过 HTTPS 长轮询领取该节点下全部已绑定企微账号任务
```

这个模式下客户端不需要配置租户编号、渠道编号、机器人编号、Provider API Base、callback_url 或 callback_secret。激活码只使用一次，云端只保存激活码摘要；设备密钥只保存在客户电脑，日志和界面不会显示原文。

主服务渠道里的 `API Base` 只是兼容旧直连 HTTP Provider 的字段；节点模式下渠道改为绑定本租户节点及其已发现企微账号。

本地 `config.json` 只需要关注：

```json
{
  "cloud_poll_enabled": true,
  "cloud_base_url": "https://cloud.example.com",
  "provider_node_name": "客服电脑A",
  "activation_code": "pact_xxx",
  "device_token": "",
  "device_id": "",
  "heartbeat_interval_seconds": 20,
  "poll_timeout_seconds": 25
}
```

激活成功后 Provider 会自动清空 `activation_code`，原子保存 `device_token/device_id`。任务由云端固化 `provider_account_key`，客户端只能分派到唯一匹配的在线 Hook；本地使用 `job_id + payload_hash` 防重，云端重放时只补结果回报，不重复发送。

旧版本的 `authorization_code/client_token/cloud_channel_id` 不能直接升级为节点设备身份。新版本会识别并清理这些字段，窗口显示“需要重新激活”，管理员需要在 SaaS 节点页面重新生成一次性激活码。

同一台电脑运行多个企微 Hook 端点时，可在高级配置中声明本地账号端点：

```json
{
  "provider_accounts": [
    {"account_key": "zhangsan", "hook_api_base": "http://127.0.0.1:8989"},
    {"account_key": "lisi", "hook_api_base": "http://127.0.0.1:8990"}
  ]
}
```

`account_key` 必须与对应 Hook `/status` 返回的 `current_user_id` 一致。账号缺失、离线或出现重复匹配时，Provider 会拒绝执行任务并向云端回报错误，不会自动选择其他账号。

### 兼容：直连 HTTP 模式

```json
{
  "send_path": "/send-message",
  "health_path": "/health",
  "headers": {
    "X-Provider-Token": "replace-with-provider-token"
  }
}
```

- `channel_type`: `workwechat`
- `api_base`: `http://<provider机器IP>:8181`
- `bot_token`: 与 `config.json.callback_secret` 一致
- `adapter_config_json.headers.X-Provider-Token`: 与 `config.json.provider_token` 一致，不能只写平铺的 `provider_token`
