# Wechat iPad Provider 部署说明

本文档用于说明如何单独部署 `wechat_ipad provider`，并接入 `Multi Bot Admin`。

当前项目不负责自动托管这个 provider。推荐方式是：

- 你自己单独启动 provider
- 在后台渠道管理中填写 provider 的 `API Base`

项目内附带的 provider 目录位置：

- [vendors/wechat_ipad_provider](/d:/learn/multi-bot-admin/vendors/wechat_ipad_provider)

## 目录说明

当前目录内主要文件如下：

- [vendors/wechat_ipad_provider/main.exe](/d:/learn/multi-bot-admin/vendors/wechat_ipad_provider/main.exe)：Windows 可执行文件
- [vendors/wechat_ipad_provider/ipad859go](/d:/learn/multi-bot-admin/vendors/wechat_ipad_provider/ipad859go)：另一份可执行文件
- [vendors/wechat_ipad_provider/conf/app.conf](/d:/learn/multi-bot-admin/vendors/wechat_ipad_provider/conf/app.conf)：主配置文件
- [vendors/wechat_ipad_provider/conf/app-http.conf](/d:/learn/multi-bot-admin/vendors/wechat_ipad_provider/conf/app-http.conf)：HTTP 配置样例
- [vendors/wechat_ipad_provider/conf/app-tcp.conf](/d:/learn/multi-bot-admin/vendors/wechat_ipad_provider/conf/app-tcp.conf)：TCP 配置样例

建议优先使用：

- Windows：`main.exe`

## 前置要求

部署前请确认：

- 机器上已经准备好 Redis
- provider 监听端口可访问，默认建议 `8059`
- `Multi Bot Admin` 所在机器能访问到 provider 的地址

如果你把 provider 和后台部署在同一台机器上，常见填写方式是：

- `http://127.0.0.1:8059`

如果是局域网其他机器：

- `http://<provider机器IP>:8059`

## Redis 要求

provider 依赖 Redis。

你需要提前准备一个可用 Redis，然后在 [app.conf](/d:/learn/multi-bot-admin/vendors/wechat_ipad_provider/conf/app.conf) 中配置：

```conf
redislink = "127.0.0.1:6379"
redispass = ""
redisdbnum = 1
```

说明：

- `redislink`：Redis 地址，格式通常为 `host:port`
- `redispass`：Redis 密码，没有就留空
- `redisdbnum`：Redis DB 编号，建议单独分配一个库

不要直接使用仓库里现成的默认值，先改成你自己的环境。

## app.conf 推荐配置

建议把 [app.conf](/d:/learn/multi-bot-admin/vendors/wechat_ipad_provider/conf/app.conf) 改成类似下面这样：

```conf
appname = "wxapi"
httpaddr = "0.0.0.0"
httpport = 8059
runmode = "prod"
viewspath = "template"
autorender = false
copyrequestbody = true
EnableDocs = true
redislink = "127.0.0.1:6379"
redispass = ""
redisdbnum = 1
ocrurl = ""
syncmessagebusinessuri = ""
logoutbusinessuri = ""

sessionon = true

SessionName = "wxapi"
ServerName = "wxapi"

longlinkenabled = true
longlinkconnecttimeout = "1m"
```

建议项：

- `httpaddr` 使用 `0.0.0.0`，方便其他机器访问
- `httpport` 使用 `8059`
- `runmode` 生产环境建议 `prod`
- 如果你当前没有 OCR 或外部业务回调，`ocrurl`、`syncmessagebusinessuri`、`logoutbusinessuri` 可以先留空

## Windows 启动方式

在 Windows PowerShell 中进入 provider 目录：

```powershell
cd D:\learn\multi-bot-admin\vendors\wechat_ipad_provider
```

启动：

```powershell
.\main.exe
```

如果你的环境里是另一份可执行文件可用，也可以尝试：

```powershell
.\ipad859go
```

如果希望后台常驻，建议你自行使用以下任一方式托管：

- Windows 任务计划程序
- NSSM
- 自己的守护脚本

## 启动后检查

启动成功后，优先检查这几项：

- 控制台是否报错
- `8059` 端口是否已经监听
- 打开 Swagger 页面是否正常

常见地址：

- `http://127.0.0.1:8059/swagger/index.html`

如果能打开接口文档页面，通常说明 provider 已经起来了。

## 在后台中接入

在 `Multi Bot Admin` 后台新增或编辑 `wechat_ipad` 渠道时：

- 渠道类型选择 `wechat_ipad`
- `API Base` 填写 provider 地址，例如 `http://127.0.0.1:8059`
- `Adapter Config JSON` 中至少补齐 `wxid`

示例：

```json
{
  "wxid": "wxid_xxx",
  "device_id": "your-device-id"
}
```

说明：

- `wxid` 是当前登录微信号对应的标识
- `device_id` 没有强制统一格式，但建议固定使用，避免登录态混乱

## 后台里的验证步骤

渠道配置完成后，建议按这个顺序检查：

1. 在渠道详情页确认 `API Base` 已填写正确。
2. 点击“健康检查”，确认接口可达。
3. 再执行“初始化登录”或“刷新二维码”。
4. 观察登录状态、二维码状态、心跳和最近拉取时间。

## 常见问题

### 1. 健康检查失败

优先检查：

- provider 是否真的启动了
- `API Base` 是否填写正确
- 防火墙是否放行端口
- provider 所在机器的 Redis 是否可用

### 2. 二维码拉不出来

优先检查：

- `wxid`、`device_id` 是否合理
- provider 控制台日志是否有报错
- Redis 是否正常

### 3. Docker 里的后台访问不到 provider

如果后台跑在 Docker、provider 跑在宿主机，不要直接填 `127.0.0.1`，因为容器内的 `127.0.0.1` 指向容器自己。

你应该填写：

- 宿主机实际 IP
- 或 Docker 可访问的宿主机地址

## 建议

推荐把 provider 看成独立服务：

- 单独部署
- 单独观察日志
- 单独管理 Redis

这样出问题时，边界最清晰，也最方便排查。
