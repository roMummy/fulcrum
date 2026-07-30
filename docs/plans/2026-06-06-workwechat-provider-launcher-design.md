# WorkWechat Provider 小窗口启动器设计

## 目标

为 `vendors/workwechat_provider` 增加一个面向客服同事的 Windows 小窗口启动器，让日常操作简化为：

1. 打开并登录企业微信。
2. 双击 `WorkWechatProvider.exe`。
3. 启动器自动探测已运行的企业微信并启动 provider/hook。
4. 小窗口显示“运行正常”后最小化到托盘。

启动器需要同时解决两个问题：

- 降低客服操作复杂度，不要求客服手动安装 Python、执行 bat、查看命令行日志。
- 在启动 Hook 前检查企业微信客户端版本，避免 DLL 对不上版本时继续注入导致异常。

## 边界

启动器可以把 32 位 Python runtime 和 provider 依赖一起打包，客服机器不需要单独安装 Python。

启动器不能保证 `Loader.dll`、`WeChat_5.0.0.6008.dll` 完全不可见。Windows 加载 DLL 时仍需要真实模块，打包只能降低误操作和随手复制的概率，不能形成强防逆向能力。

## 推荐方案

采用“小窗口 + 托盘 + 可选开机自启”的单文件 UI 打包方式。

- 小窗口负责展示状态和提供常用按钮。
- 托盘负责长期后台运行，避免客服误关窗口。
- `WorkWechatProvider.exe` 内置 provider server、Hook runtime、32 位 Python runtime、依赖和 Hook 文件。
- 客服侧只能交付一个入口文件 `WorkWechatProvider.exe`，不要再暴露 `WorkWechatProviderServer.exe` 或 `WorkWechatHookRuntime.exe` 给客服操作；内部 exe 只作为 PyInstaller 解包后的运行资源。
- 首次运行时在 `%LOCALAPPDATA%\WorkWechatProvider` 生成 `config.json`、`logs`、`data`；UI 提供“打开配置/打开日志”，客服不需要进 exe 目录找文件。
- 默认不启用开机自启，后续运行稳定后再让客服勾选。

`onefile` 仍会解压 DLL 和内部 exe 到临时目录，不能作为防逆向方案；这里采用它的主要原因是客服交付和日常操作只暴露一个 UI 入口。

## 已确认调优策略

以下策略是本轮 Windows 现场验证后的最终约束，后续改打包、启动流程或 UI 时不要回退：

- 单文件交付：客服只使用 `WorkWechatProvider.exe`。内部 provider server、Hook runtime、`Loader.dll`、`WeChat_5.0.0.6008.dll`、`config.example.json` 都打进 UI exe；运行时 PyInstaller 解包到 `_MEI...` 只是资源来源，启动器必须再复制到 `%LOCALAPPDATA%\WorkWechatProvider\runtime\<build>` 后运行，不能直接从 `_MEI...` 拉起子进程。
- 稳定运行目录：`config.json`、`logs`、`data` 统一放到 `%LOCALAPPDATA%\WorkWechatProvider`；内部 server/runtime/DLL 放到 `%LOCALAPPDATA%\WorkWechatProvider\runtime\<build>`。如果旧版本在 exe 同级已有 `config.json`，首次启动要迁移一份到稳定目录。
- 退出清理：provider 和 Hook runtime 从稳定 runtime 运行，避免占用 PyInstaller `_MEI...` 导致“临时目录删除失败”；退出时先正常停止 provider，再兜底回收 provider 子进程树。
- 单实例约束：当前阶段只支持一个启动器/provider 实例，启动器必须加 Windows 单实例锁，避免多个 UI 抢同一个 provider 端口、Hook 端口和企业微信客户端。多企微/多 provider 实例后续再设计独立端口和 PID 绑定策略。
- 不再要求客服手动启动 runtime：UI 启动 provider，provider 自动拉起 Hook runtime；如果 `8989` 已经有 Hook API 监听，则复用现有 Hook，不重复启动。
- 企业微信已启动时必须直接进入流程：启动器打开后先探测 `WXWork.exe`，若进程存在且版本符合要求，自动启动 provider/hook，不应卡在“请启动企业微信”或要求再点一次启动。
- 避免重复拉起企业微信：Hook runtime 内部调用 `InjectWeChat` 失败后，必须先检测 `WXWork.exe` 是否已存在；如果已存在，不允许继续调用会启动新客户端的 `InjectWeChatMultiOpen`，而是返回注入失败并提示检查权限/重启企业微信。
- 版本显示要有兜底：UI 先从进程 `exe` 路径读取版本；如果权限或 psutil 导致路径为空，再从常见安装目录如 `C:\Program Files (x86)\WXWork\WXWork.exe` 读取。健康轮询刷新 UI 时必须保留并刷新 `current_version / required_version`，不能被“启动中/运行中”状态覆盖回“未知”。
- UI 字体和品牌：Windows UI 全局使用 `Microsoft YaHei UI`，标题区、窗口图标和托盘图标使用内置 Qt 绘制 LOGO，不依赖额外图片文件，避免打包漏资源。
- 桌面 exe 图标：构建时用 `generate_launcher_icon.py` 生成 `assets\workwechat-provider.ico`，最终 UI PyInstaller 命令必须带 `--icon "assets\workwechat-provider.ico"`，避免客服桌面看到默认 PyInstaller 图标。
- Uvicorn/窗口化日志：provider server 是 windowed 子进程时，不能依赖 `sys.stdout.isatty()`；服务端启动应使用适配窗口化环境的日志配置，避免 `NoneType has no attribute isatty`。
- 主服务连接配置：主服务 channel 的 `api_base` 必须指向 provider 真实端口，默认 `http://127.0.0.1:8181`；provider token 必须放在 `adapter_config_json.headers.X-Provider-Token`，不能只写平铺 `provider_token`。
- 回调密钥一致：provider `config.json.callback_secret` 必须和主服务 channel 的 `bot_token` 一致；`callback_url` 指向 `/webhooks/{channel_id}/inbound`。
- 依赖警告要保留跟踪：当前 PyInstaller 会提示 `WeChat_5.0.0.6008.dll` 依赖 `tencent.mm.pbc.dll` 未解析；如果后续目标机器 Hook 注入不稳定，优先补齐该 DLL 到 `hook_runtime` 并加入打包命令。

## 交付文件

```text
WorkWechatProvider.exe
```

`config.json` 继续作为现场可编辑配置，但不作为交付文件强制携带；启动器首次运行时自动在 `%LOCALAPPDATA%\WorkWechatProvider\config.json` 生成默认配置。启动器只做打开配置、校验配置和展示关键字段，不把业务配置写死到 exe 里。

## 小窗口交互

窗口标题为 `企业微信 Provider`。主界面展示五组状态：

- 企业微信：未打开 / 已打开 / 已登录待确认 / 版本不匹配
- 客户端版本：当前版本 / 要求版本
- Provider：未启动 / 启动中 / 运行中 / 异常
- Hook：未启动 / 注入中 / 运行中 / 注入失败
- 回调：未配置 / 检测中 / 正常 / 异常

按钮：

- 启动：执行完整检测和启动流程。
- 停止：停止启动器持有的 provider/hook 子进程。
- 重启：先停止再启动。
- 打开日志：打开 `%LOCALAPPDATA%\WorkWechatProvider\logs` 目录。
- 打开配置：打开 `%LOCALAPPDATA%\WorkWechatProvider\config.json`。
- 最小化到托盘：隐藏窗口，托盘图标继续显示状态。

托盘菜单：

- 显示窗口
- 重启
- 打开日志
- 退出

## 启动流程

1. 显示小窗口，避免启动探测阻塞 UI。
2. 读取 `config.json`。
3. 检查企业微信进程是否存在，优先匹配 `WXWork.exe`。
4. 如果企业微信已经启动，直接进入版本检查和后续启动流程，不要求客服再点“启动”。
5. 读取企业微信 exe 的文件版本。
6. 与配置里的 `required_workwechat_version` 比对。
7. 如果版本不匹配，停止启动并提示安装指定版本。
8. 检查 provider 端口 `8181` 和 Hook 端口 `8989`。
9. 启动 provider。
10. provider 自动拉起内置 Hook runtime。
11. 等待 `/health` 返回正常或 degraded 状态。
12. 调用 Hook 健康探测，确认 `8989/api` 可用。
13. 更新小窗口状态为“运行正常”或展示具体失败原因。

Hook runtime 内部注入流程必须遵守：

1. 先调用 `InjectWeChat` 注入已运行企业微信。
2. 如果返回 0，先检测本机是否已有 `WXWork.exe`。
3. 如果已有 `WXWork.exe`，停止本次注入并返回明确错误，不调用 `InjectWeChatMultiOpen`。
4. 只有完全没有 `WXWork.exe` 时，才允许调用 `InjectWeChatMultiOpen` 启动并注入企业微信。

## 版本检测

新增配置项：

```json
{
  "required_workwechat_version": "5.0.0.6008",
  "workwechat_process_name": "WXWork.exe"
}
```

检测逻辑：

- 找不到企业微信进程：提示“请先打开并登录企业微信”。
- 找到多个进程：选择主进程路径能读取版本的进程。
- 版本号为空：先用企业微信常见安装目录兜底读取；仍为空时才显示“未知”，但只要企业微信进程已经启动，就继续进入启动流程，避免客服卡在启动器界面。
- 版本不等于要求版本：提示当前版本和要求版本，不继续注入。
- 版本一致：进入 provider/hook 启动流程。

## 错误提示

所有错误提示必须面向客服可理解：

- 企业微信未打开：`请先打开企业微信并登录，然后点击“启动”。`
- 版本不匹配：`当前企业微信版本是 X，需要 Y。请联系管理员安装指定版本。`
- Provider 端口占用：`Provider 端口被占用，请点击“重启”或联系管理员。`
- Hook 注入失败：`企业微信 Hook 启动失败，请确认版本正确并以普通用户权限重新打开企业微信。`
- 回调失败：`Provider 已启动，但无法连接主服务，请联系管理员检查网络或配置。`

详细错误写入日志，窗口只展示摘要。

## 打包策略

使用 32 位 Python 环境构建单文件 UI：

- provider server 先打包成内部 `WorkWechatProviderServer.exe`。
- Hook runtime 先打包成内部 `WorkWechatHookRuntime.exe`。
- 最终 UI 启动器打包成唯一交付文件 `WorkWechatProvider.exe`，并内置 server、runtime、Hook DLL 和 `config.example.json`。
- `config.json` 首次运行时从内置默认配置生成到 `%LOCALAPPDATA%\WorkWechatProvider`；如果 exe 同级存在旧配置，需要迁移到该目录。
- UI 启动时必须把 `_MEI...` 中的内部 server/runtime/DLL 复制到 `%LOCALAPPDATA%\WorkWechatProvider\runtime\<build>`，并通过 `WORKWECHAT_PROVIDER_BUNDLED_ROOT` 指向该稳定 runtime，provider/hook 不允许直接从 `_MEI...` 运行。
- 打包脚本必须保持“一行 PyInstaller 命令 + 显式 `--add-binary`”的形式，至少包含：
- `dist\WorkWechatProviderServer.exe;.`
- `dist\WorkWechatHookRuntime.exe;hook_runtime`
- `hook_runtime\Loader.dll;hook_runtime`
- `hook_runtime\WeChat_5.0.0.6008.dll;hook_runtime`
- `hook_runtime\API.docx;hook_runtime`
- `config.example.json;.`
- 打包完成后 `dist` 目录只保留客服入口 `WorkWechatProvider.exe`，临时内部 exe 要删除，避免交付时误拿错文件。

窗口技术建议优先选择 `PySide2`，因为 `hook_runtime/requirements.txt` 已经出现该依赖。若打包体积或兼容性不理想，再降级为 Tkinter。

## 测试策略

单元测试：

- 版本号解析。
- 企业微信进程路径选择。
- 状态机转换。
- 端口占用判断。
- 配置默认值。

人工验证：

- 企业微信未打开时启动。
- 企业微信版本正确时启动。
- 企业微信版本错误时启动。
- `8181` 被占用时启动。
- `8989` 已有 Hook 监听时启动。
- 最小化到托盘、托盘恢复、退出。
- 修改配置后重启。

## 后续实现顺序

1. 增加启动器配置字段和检测函数。
2. 增加版本检测测试。
3. 增加小窗口状态模型。
4. 增加 PySide2 小窗口入口。
5. 接入 provider/hook 启停。
6. 增加打包脚本。
7. 在 Windows 32 位 Python 环境执行人工验证。
