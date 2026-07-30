# 企微 Provider 启动前端口清理设计

## 背景

企微 Provider 需要占用本机 `8181`，内置 Hook runtime 需要占用 `8989`。现场排障发现旧的源码版 `vendors/workwechat_provider/app.py` 或旧的 `WorkWechatHookRuntime.exe` 可能残留，占住端口后导致新的 `WorkWechatProvider.exe` 启动器读到正确配置却无法接管服务，进而出现 `401`、`degraded`、`待注册` 等误导性状态。

## 目标

启动器在启动 provider 前自动清理“明确属于本项目企微链路”的旧进程，并在端口被非本项目进程占用时给出前端提示，不做误杀。

## 范围

自动清理对象仅限以下进程：

- `WorkWechatProviderServer.exe`
- `WorkWechatHookRuntime.exe`
- 命令行包含 `vendors\workwechat_provider\app.py`
- 命令行包含 `vendors\workwechat_provider\hook_runtime\http_py.py`

检查端口仅限：

- `8181`：高层 provider 服务
- `8989`：内置 Hook HTTP API

## 设计

在 `ProviderProcessController.start()` 启动子进程前新增端口占用预检。预检通过 `psutil.net_connections()` 找到监听 `8181` / `8989` 的进程，再读取进程名、命令行和 exe 路径。

如果占用进程匹配本项目企微链路，启动器会先终止该进程树，再继续启动新的 provider。终止时复用 `psutil`，先 `terminate()`，短暂等待后仍未退出再 `kill()`。

如果占用进程不匹配本项目企微链路，启动器不关闭它，而是抛出结构化异常。UI 捕获异常后在窗口中显示端口、进程名、PID 和路径，提示用户手动处理。

## 错误处理

无法读取进程信息时按“非本项目占用”处理，不自动关闭。

清理本项目旧进程失败时按启动失败处理，并在 UI 中展示错误。这样避免半清理状态下继续启动，导致多个 provider 抢端口。

## 测试

补充启动器控制器单元测试：

- 本项目旧 provider / hook 进程会被识别为可清理。
- 非本项目端口占用不会被识别为可清理。
- 端口冲突异常包含端口、PID 和进程描述，供 UI 展示。

