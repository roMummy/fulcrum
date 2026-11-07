# Windows 本地代理扫码连接器

这个连接器只在微信 iPad 扫码登录期间工作。首次运行时在 SaaS 的租户设置页创建连接器并输入一次性激活码，随后填写本机 SOCKS5 地址。连接器会主动连接 SaaS，不需要用户在局域网开放入站端口。

874 返回登录成功后，SaaS 会先清除 874 登录数据里的代理，再发送 `session_end`。看到登录完成后即可关闭本程序；后续心跳、消息同步和账号运行不依赖本地代理连接器。

## 运行

```powershell
py -3.11 -m venv .venv
.venv\Scripts\python.exe -m pip install -r requirements.txt
.venv\Scripts\python.exe main.py --server https://your-saas.example.com --activation-code 123456789-ABCD1234 --socks-host 127.0.0.1 --socks-port 1080
```

设备密钥保存在 `%APPDATA%\MultiBotSaas\local-proxy-connector.json`。文件中保存的是本机受保护后的设备密钥，不会把 SOCKS5 密码上传到 SaaS。

## 打包

执行 `build.bat` 生成单文件 Windows 程序。真实微信扫码、真实 SOCKS5 和生产 SaaS 地址仍需要在 Windows 设备上单独验收。
