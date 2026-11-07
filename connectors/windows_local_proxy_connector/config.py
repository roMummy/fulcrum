"""Windows 连接器配置和本地受保护凭证存储。"""

from __future__ import annotations

import base64
import binascii
import ctypes
import json
import os
import platform
from pathlib import Path
from typing import Any


class ConnectorConfigStore:
    """保存 SaaS 地址、设备编号和设备密钥，不保存 SaaS 可用的明文 SOCKS 密码。"""

    def __init__(self, path: Path | None = None) -> None:
        default_path = Path(os.environ.get("APPDATA", Path.home())) / "MultiBotSaas" / "local-proxy-connector.json"
        self.path = path or default_path

    def load(self) -> dict[str, Any]:
        if not self.path.exists():
            return {}
        try:
            payload = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return {}
        return payload if isinstance(payload, dict) else {}

    def save(self, payload: dict[str, Any]) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        temporary = self.path.with_suffix(".tmp")
        temporary.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
        temporary.replace(self.path)

    def save_device_key(self, *, server: str, connector_id: int, device_key: str, device_id: str) -> None:
        """Windows 使用 DPAPI 保护设备密钥，开发机则使用明确的本地编码回退。"""

        self.save(
            {
                "server": server.rstrip("/"),
                "connector_id": int(connector_id),
                "device_id": device_id,
                "device_key": _protect_secret(device_key),
            }
        )

    def device_key(self) -> str:
        return _unprotect_secret(str(self.load().get("device_key") or ""))


def _protect_secret(value: str) -> str:
    """通过 Windows CryptProtectData 绑定当前用户，避免设备密钥裸写磁盘。"""

    raw = str(value or "").encode("utf-8")
    if platform.system().lower() != "windows":
        return "plain:" + base64.urlsafe_b64encode(raw).decode("ascii")
    class Blob(ctypes.Structure):
        _fields_ = [("cbData", ctypes.c_uint32), ("pbData", ctypes.POINTER(ctypes.c_ubyte))]
    source = (ctypes.c_ubyte * len(raw))(*raw)
    in_blob = Blob(len(raw), source)
    out_blob = Blob()
    crypt = ctypes.windll.crypt32.CryptProtectData
    if not crypt(ctypes.byref(in_blob), None, None, None, None, 0, ctypes.byref(out_blob)):
        raise OSError("CryptProtectData failed")
    try:
        protected = ctypes.string_at(out_blob.pbData, out_blob.cbData)
    finally:
        ctypes.windll.kernel32.LocalFree(out_blob.pbData)
    return "dpapi:" + base64.urlsafe_b64encode(protected).decode("ascii")


def _unprotect_secret(value: str) -> str:
    prefix, _, encoded = str(value or "").partition(":")
    try:
        raw = base64.urlsafe_b64decode(encoded.encode("ascii"))
    except (ValueError, binascii.Error):
        return ""
    if prefix != "dpapi:" or platform.system().lower() != "windows":
        return raw.decode("utf-8", errors="ignore")
    class Blob(ctypes.Structure):
        _fields_ = [("cbData", ctypes.c_uint32), ("pbData", ctypes.POINTER(ctypes.c_ubyte))]
    source = (ctypes.c_ubyte * len(raw))(*raw)
    in_blob = Blob(len(raw), source)
    out_blob = Blob()
    if not ctypes.windll.crypt32.CryptUnprotectData(ctypes.byref(in_blob), None, None, None, None, 0, ctypes.byref(out_blob)):
        return ""
    try:
        return ctypes.string_at(out_blob.pbData, out_blob.cbData).decode("utf-8", errors="ignore")
    finally:
        ctypes.windll.kernel32.LocalFree(out_blob.pbData)
