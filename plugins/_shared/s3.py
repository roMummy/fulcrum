"""最小化的 S3 兼容客户端(MinIO 可用):只支持"按前缀列对象"和"取单个对象"。

只用标准库实现 AWS Signature V4,不引入 boto3/minio 等第三方依赖
(线上镜像采用"旧镜像 + 复制代码"的方式部署,不执行 pip install)。
使用路径风格地址 http(s)://endpoint/bucket/key,MinIO 默认就是这种形式。
"""

from __future__ import annotations

import hashlib
import hmac
import ipaddress
import socket
import urllib.error
import urllib.request
import xml.etree.ElementTree as ET
from dataclasses import dataclass
from datetime import datetime, timezone
from urllib.parse import quote

EMPTY_PAYLOAD_SHA256 = hashlib.sha256(b"").hexdigest()
# ListObjectsV2 单页上限。
LIST_PAGE_SIZE = 1000


class S3Error(Exception):
    """S3 请求失败:网络错误、鉴权失败、对象不存在或响应不合法。"""


@dataclass(frozen=True)
class S3Config:
    endpoint: str  # host 或 host:port,不带协议
    bucket: str
    access_key: str
    secret_key: str
    secure: bool = False
    region: str = "us-east-1"
    timeout_seconds: int = 20

    def is_complete(self) -> bool:
        return bool(self.endpoint and self.bucket and self.access_key and self.secret_key)


def _hmac(key: bytes, message: str) -> bytes:
    return hmac.new(key, message.encode("utf-8"), hashlib.sha256).digest()


def _uri_encode(value: str, *, keep_slash: bool) -> str:
    """按 SigV4 规则编码:只保留 A-Z a-z 0-9 - _ . ~(路径里额外保留 /)。"""

    return quote(value, safe="/-_.~" if keep_slash else "-_.~")


def canonical_request(method: str, path: str, query: dict[str, str], headers: dict[str, str], payload_hash: str) -> tuple[str, str]:
    """返回 (规范请求, 已签名头列表)。headers 的键需为小写。"""

    canonical_query = "&".join(f"{_uri_encode(k, keep_slash=False)}={_uri_encode(v, keep_slash=False)}" for k, v in sorted(query.items()))
    names = sorted(headers)
    canonical_headers = "".join(f"{name}:{' '.join(headers[name].split())}\n" for name in names)
    signed = ";".join(names)
    request = "\n".join([method, _uri_encode(path, keep_slash=True), canonical_query, canonical_headers, signed, payload_hash])
    return request, signed


def signature(secret_key: str, canonical: str, *, amz_date: str, region: str, service: str) -> str:
    """计算 SigV4 签名(十六进制)。"""

    day = amz_date[:8]
    scope = f"{day}/{region}/{service}/aws4_request"
    to_sign = "\n".join(["AWS4-HMAC-SHA256", amz_date, scope, hashlib.sha256(canonical.encode("utf-8")).hexdigest()])
    key = _hmac(("AWS4" + secret_key).encode("utf-8"), day)
    for part in (region, service, "aws4_request"):
        key = _hmac(key, part)
    return hmac.new(key, to_sign.encode("utf-8"), hashlib.sha256).hexdigest()


def reject_unsafe_endpoint(endpoint: str) -> None:
    """拒绝指向本机、链路本地(云元数据)等地址的端点,防止租户借此访问内部服务。

    局域网地址允许(MinIO 通常就部署在内网)。
    """

    host = endpoint.rsplit(":", 1)[0] if ":" in endpoint and not endpoint.endswith("]") else endpoint
    host = host.strip("[]")
    if not host or any(ch in host for ch in "/@ "):
        raise S3Error("MinIO 地址格式不正确,应为 主机:端口")
    try:
        infos = socket.getaddrinfo(host, None)
    except OSError as exc:
        raise S3Error("MinIO 地址无法解析") from exc
    for info in infos:
        address = ipaddress.ip_address(info[4][0])
        if address.is_loopback or address.is_link_local or address.is_unspecified or address.is_multicast:
            raise S3Error("MinIO 地址不能指向本机或链路本地地址")


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *args, **kwargs):  # noqa: D401
        return None


class S3Client:
    """按配置访问一个桶。每次请求单独签名,不保持连接。"""

    def __init__(self, config: S3Config) -> None:
        self.config = config
        self._opener = urllib.request.build_opener(_NoRedirect)

    def _request(self, path: str, query: dict[str, str], *, max_bytes: int) -> bytes:
        cfg = self.config
        now = datetime.now(timezone.utc)
        amz_date = now.strftime("%Y%m%dT%H%M%SZ")
        headers = {"host": cfg.endpoint, "x-amz-content-sha256": EMPTY_PAYLOAD_SHA256, "x-amz-date": amz_date}
        canonical, signed = canonical_request("GET", path, query, headers, EMPTY_PAYLOAD_SHA256)
        sig = signature(cfg.secret_key, canonical, amz_date=amz_date, region=cfg.region, service="s3")
        scope = f"{amz_date[:8]}/{cfg.region}/s3/aws4_request"
        authorization = f"AWS4-HMAC-SHA256 Credential={cfg.access_key}/{scope}, SignedHeaders={signed}, Signature={sig}"
        query_string = "&".join(f"{_uri_encode(k, keep_slash=False)}={_uri_encode(v, keep_slash=False)}" for k, v in sorted(query.items()))
        url = f"{'https' if cfg.secure else 'http'}://{cfg.endpoint}{_uri_encode(path, keep_slash=True)}" + (f"?{query_string}" if query_string else "")
        request = urllib.request.Request(
            url,
            headers={"x-amz-date": amz_date, "x-amz-content-sha256": EMPTY_PAYLOAD_SHA256, "Authorization": authorization},
            method="GET",
        )
        try:
            with self._opener.open(request, timeout=cfg.timeout_seconds) as response:
                declared = response.headers.get("Content-Length")
                if declared and declared.isdigit() and int(declared) > max_bytes:
                    raise S3Error("对象超过大小限制")
                body = response.read(max_bytes + 1)
        except urllib.error.HTTPError as exc:
            raise S3Error(f"S3 返回 {exc.code}") from exc
        except (urllib.error.URLError, OSError) as exc:
            raise S3Error("无法连接 MinIO") from exc
        if len(body) > max_bytes:
            raise S3Error("对象超过大小限制")
        return body

    def list_objects(self, prefix: str = "", *, max_keys: int = 5000) -> list[tuple[str, int]]:
        """列出前缀下的对象 (key, size),最多 max_keys 个;只取前面若干页,不追求完整。"""

        found: list[tuple[str, int]] = []
        token = ""
        while len(found) < max_keys:
            query = {"list-type": "2", "max-keys": str(LIST_PAGE_SIZE)}
            if prefix:
                query["prefix"] = prefix
            if token:
                query["continuation-token"] = token
            body = self._request(f"/{self.config.bucket}", query, max_bytes=8 * 1024 * 1024)
            try:
                root = ET.fromstring(body)
            except ET.ParseError as exc:
                raise S3Error("MinIO 返回的列表格式不正确") from exc
            for node in root.iter():
                tag = node.tag.rsplit("}", 1)[-1]
                if tag != "Contents":
                    continue
                key = size = None
                for child in node:
                    name = child.tag.rsplit("}", 1)[-1]
                    if name == "Key":
                        key = child.text or ""
                    elif name == "Size":
                        size = int(child.text or 0)
                if key and not key.endswith("/"):
                    found.append((key, size or 0))
            truncated = next((n.text for n in root.iter() if n.tag.rsplit("}", 1)[-1] == "IsTruncated"), "false")
            token = next((n.text or "" for n in root.iter() if n.tag.rsplit("}", 1)[-1] == "NextContinuationToken"), "")
            if (truncated or "").lower() != "true" or not token:
                break
        return found[:max_keys]

    def get_object(self, key: str, *, max_bytes: int) -> bytes:
        """读取单个对象,超过 max_bytes 直接失败。"""

        return self._request(f"/{self.config.bucket}/{key}", {}, max_bytes=max_bytes)
