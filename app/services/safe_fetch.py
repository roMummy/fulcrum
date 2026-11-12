"""向外部地址下载媒体时的 SSRF 防护:只允许访问公网地址。

出站消息的图片、文件、视频地址来自插件回复或手动发送,是不可信输入。如果直接 urlopen,租户可以让服务器去访问
`http://127.0.0.1:...`、`http://192.168.x.x/...`、`http://169.254.169.254/`(云元数据)等内网地址,
再把抓到的内容当成图片/文件发出去。这里在**每一次请求(包括重定向的每一跳)**之前解析主机名,
只要解析结果里有任何一个回环、私网、链路本地、保留、组播或未指定地址就拒绝。

已知边界:校验与真正连接之间存在 DNS 重绑定的时间窗口(校验通过后域名再解析到内网)。完全消除需要把连接钉在
已校验的 IP 上;当前对"租户手填一个内网地址"这个主要场景足够,更强的防护另行评估。
"""

from __future__ import annotations

import ipaddress
import socket
from urllib.error import URLError
from urllib.parse import urlsplit
from urllib.request import HTTPRedirectHandler, Request, build_opener

MAX_REDIRECTS = 3


class UnsafeUrlError(ValueError):
    """地址不是允许访问的公网 http(s) 地址。"""


def _is_public(address: str) -> bool:
    ip = ipaddress.ip_address(address)
    if getattr(ip, "ipv4_mapped", None):
        ip = ip.ipv4_mapped
    return not (ip.is_private or ip.is_loopback or ip.is_link_local or ip.is_multicast or ip.is_reserved or ip.is_unspecified)


def ensure_public_http_url(url: str) -> str:
    """校验地址:只允许 http/https、无内嵌凭证、主机名解析出的所有地址都是公网地址。"""

    parsed = urlsplit(str(url or "").strip())
    if parsed.scheme.lower() not in {"http", "https"} or not parsed.hostname:
        raise UnsafeUrlError("只支持 http/https 地址")
    if parsed.username is not None or parsed.password is not None:
        raise UnsafeUrlError("地址不能内嵌账号密码")
    try:
        port = parsed.port
    except ValueError as exc:
        raise UnsafeUrlError("端口不合法") from exc
    try:
        infos = socket.getaddrinfo(parsed.hostname, port or (443 if parsed.scheme.lower() == "https" else 80), type=socket.SOCK_STREAM)
    except OSError as exc:
        raise UnsafeUrlError("域名无法解析") from exc
    addresses = {info[4][0].split("%", 1)[0] for info in infos}
    if not addresses or not all(_is_public(address) for address in addresses):
        raise UnsafeUrlError("不允许访问内网或保留地址")
    return parsed.geturl()


class _PublicOnlyRedirects(HTTPRedirectHandler):
    """每一跳重定向都重新校验目标地址,并限制跳数。"""

    def __init__(self) -> None:
        self.count = 0

    def redirect_request(self, req, fp, code, msg, headers, newurl):  # noqa: ANN001
        self.count += 1
        if self.count > MAX_REDIRECTS:
            raise URLError("重定向次数超过上限")
        try:
            ensure_public_http_url(newurl)
        except UnsafeUrlError as exc:
            raise URLError(f"重定向目标不安全: {exc}") from exc
        return super().redirect_request(req, fp, code, msg, headers, newurl)


def open_public_url(url: str, *, timeout: float = 20.0):
    """校验后打开地址;返回可读的响应对象,调用方负责限制读取长度。"""

    safe = ensure_public_http_url(url)
    request = Request(safe, method="GET")
    request.add_header("Expect", "")
    return build_opener(_PublicOnlyRedirects()).open(request, timeout=timeout)
