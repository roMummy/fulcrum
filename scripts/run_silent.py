"""供 pythonw 静默启动服务，控制台输出统一写入日志。"""

import os
import socket
import sys
from pathlib import Path


root = Path(__file__).resolve().parents[1]
os.chdir(root)
sys.path.insert(0, str(root))
(root / ".run").mkdir(exist_ok=True)

# pythonw 默认没有标准输出；先绑定文件，再导入依赖输出流的日志组件。
log = (root / ".run/uvicorn-silent.log").open("a", encoding="utf-8", buffering=1)
sys.stdout = sys.stderr = log

with socket.socket() as probe:
    probe.settimeout(1)
    if probe.connect_ex(("127.0.0.1", 8280)) == 0:
        print("8280 端口已有服务运行，本次不重复启动。")
        sys.exit(0)

import uvicorn


# 默认只监听本机:这个脚本面向单机桌面使用,不应在用户不知情时对局域网开放服务;
# 需要对外时请改用反向代理或自行显式修改监听地址。
uvicorn.run("app.main:app", host="127.0.0.1", port=8280, use_colors=False)
