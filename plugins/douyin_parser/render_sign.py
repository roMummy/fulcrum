"""抖音网页 ``__ac_signature`` 生成器。

签名流程参考 BEelzebub594/DouyinParser 的 ``ac_sign.py``；
``acrawler.js`` 是抖音网页下发的混淆脚本，使用和分发前请确认授权。
"""

from __future__ import annotations

import json
from functools import lru_cache
from pathlib import Path

from py_mini_racer import MiniRacer


_ACRAWLER_JS = Path(__file__).with_name("acrawler.js")

_SHIM = """
var global = globalThis, window = globalThis, self = globalThis;
var noop = function () {};
var mem = {};
globalThis.document = {
    cookie: "", referrer: "",
    createElement: function () { return { getContext: function () { return null; }, style: {} }; },
    addEventListener: noop, removeEventListener: noop,
    documentElement: { style: {} }, body: { appendChild: noop, style: {} },
    getElementById: function () { return null; }, querySelector: function () { return null; }
};
globalThis.navigator = {
    userAgent: %(ua)s, platform: "Win32", language: "zh-CN", languages: ["zh-CN"],
    hardwareConcurrency: 8, webdriver: false, plugins: [], mimeTypes: [], cookieEnabled: true
};
globalThis.location = {
    protocol: "https:", href: "https://www.douyin.com/", host: "www.douyin.com",
    hostname: "www.douyin.com", pathname: "/", origin: "https://www.douyin.com",
    reload: noop, search: "", hash: ""
};
globalThis.localStorage = {
    getItem: function (k) { return mem[k] || null; },
    setItem: function (k, v) { mem[k] = String(v); },
    removeItem: function (k) { delete mem[k]; }
};
globalThis.sessionStorage = { getItem: function () { return null; }, setItem: noop, removeItem: noop };
globalThis.screen = { width: 1536, height: 864 };
globalThis.performance = { timing: { navigationStart: Date.now() }, now: function () { return Date.now(); } };
globalThis.history = { length: 1 };
globalThis.Image = function () {};
globalThis.PluginArray = function () {};
globalThis.indexedDB = {};
globalThis.DOMException = Error;
globalThis.WebSocket = function () {};
globalThis.Request = function () {};
globalThis.Headers = function () {};
"""


@lru_cache(maxsize=1)
def _acrawler_source() -> str:
    try:
        return _ACRAWLER_JS.read_text(encoding="utf-8")
    except OSError as exc:
        raise RuntimeError("抖音页面签名脚本缺失") from exc


def sign_ac(nonce: str, user_agent: str) -> str:
    """按浏览器 UA 和 ``__ac_nonce`` 生成 ``__ac_signature``。"""

    normalized_nonce = str(nonce or "").strip()
    normalized_agent = str(user_agent or "").strip()
    if not normalized_nonce or not normalized_agent:
        raise RuntimeError("抖音页面签名参数缺失")
    try:
        context = MiniRacer()
        context.eval(_SHIM % {"ua": json.dumps(normalized_agent)})
        context.eval(_acrawler_source())
        context.eval("byted_acrawler.init({aid:99999999,dfp:0})")
        signature = context.eval("byted_acrawler.sign('', %s)" % json.dumps(normalized_nonce))
    except RuntimeError:
        raise
    except Exception as exc:  # noqa: BLE001 - 原生库或脚本异常统一转成可读错误
        raise RuntimeError("抖音页面签名组件不可用") from exc
    if not isinstance(signature, str) or len(signature) < 40:
        raise RuntimeError("抖音页面签名生成失败")
    return signature
