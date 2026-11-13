"""Daily fortunes and cards adapted from MIT-licensed astrbot_plugin_jrys.

Upstream: fiatlux2333/astrbot_plugin_jrys, see LICENSE.upstream and README.md.
Business records and the points ledger remain owned by the existing plugin.
"""
from __future__ import annotations

import asyncio
import threading
import base64
import hashlib
import html
import io
import json
import logging
import os
import random
import shutil
from datetime import datetime
from functools import lru_cache
from pathlib import Path
from typing import Any, Mapping

from PIL import Image

from app.core.time import now_shanghai
from app.services.plugin_runtime.http_client import PluginHttpClient
from app.services.safe_fetch import open_public_url


ASSETS = Path(__file__).resolve().parent / "assets"
MAX_AVATAR_BYTES = 2 * 1024 * 1024
MAX_AVATAR_PIXELS = 4_000_000
MAX_BACKGROUND_BYTES = 5 * 1024 * 1024
MAX_BACKGROUND_PIXELS = 12_000_000
logger = logging.getLogger(__name__)
QUOTES = (
    "愿你今天的每一份认真，都能遇见小小的惊喜。",
    "不必赶路，也可以感受路。",
    "把平凡的日子，过成自己喜欢的样子。",
    "今天也要留一点时间，给喜欢的人和事。",
    "好运藏在行动里，快乐藏在日常里。",
)


@lru_cache(maxsize=1)
def fortune_tables() -> dict[str, Any]:
    return json.loads((ASSETS / "fortune_data.json").read_text(encoding="utf-8"))


def daily_fortune(tenant_id: int, group_id: str, user_id: str, current: datetime) -> dict[str, Any]:
    """Use an explicit business date and local RNG; retries and restarts cannot reroll."""
    day = current.date().isoformat()
    seed = json.dumps([tenant_id, group_id, user_id, day], ensure_ascii=False)
    rng = random.Random(hashlib.sha256(seed.encode("utf-8")).digest())
    luck = rng.randrange(100)
    tables = fortune_tables()
    description = tables["fortunes"][0]["desc"]
    for item in tables["fortunes"]:
        if luck >= item["luck"]:
            description = item["desc"]
    events = rng.sample(tables["events"], 4)
    return {
        "date": day,
        "luck": luck,
        "description": description,
        "good_events": [{"name": item["name"], "text": item["good"]} for item in events[:2]],
        "bad_events": [{"name": item["name"], "text": item["bad"]} for item in events[2:]],
        "hitokoto": rng.choice(QUOTES),
    }


def experience_reward(fortune: Mapping[str, Any], low: int, high: int, identity: str) -> int:
    """Keep upstream's luck-weighted experience, independently of existing points."""
    seed = json.dumps([identity, fortune["date"], "experience"])
    rng = random.Random(hashlib.sha256(seed.encode("utf-8")).digest())
    value = rng.gauss(int(fortune["luck"]) / 100, 0.12)
    if value > 1:
        value = 2 - value
    elif value < 0:
        value = -value
    return round(max(0, min(1, value)) * (high - low) + low)


def level_info(total_exp: int) -> dict[str, Any]:
    levels = fortune_tables()["levels"]
    level = levels[0]
    next_exp = None
    for index, item in enumerate(levels):
        if total_exp < item["levelExp"]:
            break
        level = item
        next_exp = levels[index + 1]["levelExp"] if index + 1 < len(levels) else None
    progress = 100 if next_exp is None else (total_exp - level["levelExp"]) * 100 / (next_exp - level["levelExp"])
    return {
        "level": level["level"], "name": level["levelName"],
        "color": level["levelColor"], "next_exp": next_exp,
        "progress": max(0, min(100, progress)),
    }


@lru_cache(maxsize=7)
def _asset_uri(filename: str) -> str:
    mime = {".jpg": "image/jpeg", ".png": "image/png", ".woff2": "font/woff2", ".svg": "image/svg+xml"}[Path(filename).suffix]
    encoded = base64.b64encode((ASSETS / filename).read_bytes()).decode("ascii")
    return f"data:{mime};base64,{encoded}"


class _PublicImageOpener:
    def open(self, request, *, timeout):
        # Reuse the platform's public-address checks, including every redirect.
        return open_public_url(request.full_url, timeout=timeout)


def _avatar_data_uri(url: str) -> str:
    """Fetch a synced member avatar after commit; normalize pixels before embedding."""
    if not url:
        return _asset_uri("default_avatar.png")
    try:
        client = PluginHttpClient(opener_factory=lambda _: _PublicImageOpener())
        response = client.request(
            "GET", url, connect_timeout_seconds=2, total_timeout_seconds=2,
            max_response_bytes=MAX_AVATAR_BYTES, retry_count=0,
        )
        if not 200 <= response.status_code < 300:
            raise ValueError("Avatar download failed")
        with Image.open(io.BytesIO(response.body)) as picture:
            if picture.format not in {"JPEG", "PNG", "GIF", "WEBP"}:
                raise ValueError("Unsupported avatar format")
            if picture.width * picture.height > MAX_AVATAR_PIXELS:
                raise ValueError("Avatar exceeds pixel limit")
            picture.seek(0)
            picture.thumbnail((168, 168))
            normalized = picture.convert("RGBA")
            normalized.info.clear()
            output = io.BytesIO()
            normalized.save(output, format="PNG")
        encoded = base64.b64encode(output.getvalue()).decode("ascii")
        return f"data:image/png;base64,{encoded}"
    except Exception as exc:
        logger.warning("签到头像加载失败，已使用默认头像", extra={"error_type": type(exc).__name__})
        return _asset_uri("default_avatar.png")


def _background_data_uri(url: str) -> str:
    """Normalize a configured public image; failures never prevent a sign-in."""
    if not url:
        return _asset_uri("default_background.jpg")
    try:
        client = PluginHttpClient(opener_factory=lambda _: _PublicImageOpener())
        response = client.request(
            "GET", url, connect_timeout_seconds=3, total_timeout_seconds=3,
            max_response_bytes=MAX_BACKGROUND_BYTES, retry_count=0,
        )
        if not 200 <= response.status_code < 300:
            raise ValueError("Background download failed")
        with Image.open(io.BytesIO(response.body)) as picture:
            if picture.format not in {"JPEG", "PNG", "GIF", "WEBP"}:
                raise ValueError("Unsupported background format")
            if picture.width * picture.height > MAX_BACKGROUND_PIXELS:
                raise ValueError("Background exceeds pixel limit")
            picture.seek(0)
            picture.thumbnail((1200, 800))
            # Flatten transparent images onto the card's white background.
            rgba = picture.convert("RGBA")
            normalized = Image.new("RGB", rgba.size, "white")
            normalized.paste(rgba, mask=rgba.getchannel("A"))
            output = io.BytesIO()
            normalized.save(output, format="JPEG", quality=88)
        return "data:image/jpeg;base64," + base64.b64encode(output.getvalue()).decode("ascii")
    except Exception as exc:
        logger.warning("签到背景加载失败，已使用内置背景", extra={"error_type": type(exc).__name__})
        return _asset_uri("default_background.jpg")


def _get_greeting(hour: int) -> str:
    if 0 <= hour < 5:
        return "晚安"
    if 5 <= hour < 9:
        return "早上好"
    if 9 <= hour < 11:
        return "上午好"
    if 11 <= hour < 14:
        return "中午好"
    if 14 <= hour < 18:
        return "下午好"
    if 18 <= hour < 20:
        return "傍晚好"
    return "晚上好"


def _emoji_svg(codepoint: str) -> str:
    return f'<img class="icon" src="{_asset_uri(f"emoji_{codepoint}.svg")}" alt="">'


def build_card_html(view: Mapping[str, Any]) -> str:
    """Reuse upstream's original template; adapt only platform data and asset URIs."""
    escape = lambda value: html.escape(str(value), quote=True)
    fortune, level = view["fortune"], view["level"]
    current = now_shanghai()
    # Upstream's image omits its username field. Keep the synced group nickname
    # in the existing two-line quote area without introducing another panel.
    hitokoto = f'{escape(view["username"])}<br>{escape(fortune["hitokoto"])}'
    view = {
        "signed_today": view["provider_status"] != "signed",
        "exp_gain": int(view["exp_awarded"]),
        "coin_gain": int(view["points_awarded"]),
        "total_coin": int(view["points"]),
        "good_events": [{"name": item["name"], "good": item["text"]} for item in fortune["good_events"]],
        "bad_events": [{"name": item["name"], "bad": item["text"]} for item in fortune["bad_events"]],
        "hitokoto": hitokoto,
        "background": view.get("background_data_uri") or _asset_uri("default_background.jpg"),
        "avatar": view.get("avatar_data_uri") or _asset_uri("default_avatar.png"),
        "greeting": _get_greeting(current.hour),
        "date": datetime.fromisoformat(fortune["date"]).strftime("%m/%d"),
        "level": {"levelName": level["name"], "levelColor": level["color"]},
        "exp_text": f'{int(view["total_exp"])}/{level["next_exp"] if level["next_exp"] is not None else "???"}',
        "progress": f'{float(level["progress"]):.3f}',
        "luck": int(fortune["luck"]),
        "fortune_desc": fortune["description"],
    }
    status = "今天已经签到过了哦" if view["signed_today"] else "签到成功！"
    emoji_jelly = f'<img class="icon" src="{_asset_uri("emoji_1fae7.svg")}" alt="">'
    emoji_coin = f'<img class="icon" src="{_asset_uri("emoji_1fa99.svg")}" alt="">'
    reward = (
        f"{emoji_jelly}+{view['exp_gain']} {emoji_coin}+{view['coin_gain']}"
        if not view["signed_today"]
        else f"累计 {escape('积分')} {view['total_coin']}"
    )
    gooddo = "<br>".join(
        f"{escape(item['name'])}——{escape(item['good'])}"
        for item in view["good_events"]
    )
    baddo = "<br>".join(
        f"{escape(item['name'])}——{escape(item['bad'])}"
        for item in view["bad_events"]
    )
    # 昵称和一言已由上面的适配层转义，此处只保留受控的 <br>。
    hitokoto = view["hitokoto"]
    background = escape(view["background"])
    avatar = escape(view["avatar"])
    return f"""<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<meta http-equiv="Content-Security-Policy" content="default-src 'none'; img-src data:; style-src 'unsafe-inline'; font-src data:">
<title>运势签到</title>
<style>
@font-face {{ font-family: 'osans4'; src: url("{_asset_uri("osans4.subset.woff2")}") format("woff2"); }}
* {{ box-sizing: border-box; }}
html, body {{ margin: 0; padding: 0; background: transparent; }}
body {{ width: 600px; font-family: 'Noto Sans SC', 'Noto Sans CJK SC', osans4, 'WenQuanYi Zen Hei', 'Microsoft YaHei', Arial, sans-serif; }}
.container {{ width: 600px; background: #fff; overflow: hidden; }}
.hero {{ width: 600px; height: 389px; object-fit: cover; display: block; }}
.header {{ display: flex; flex-direction: row; justify-content: space-between; margin-left: 24px; margin-right: 40px; margin-top: -48px; position: relative; z-index: 2; }}
.avatar {{ width: 80px; height: 80px; object-fit: cover; border-radius: 10px; border: 3px solid #fff; }}
.dateInfo {{ height: 83px; width: 430px; border-radius: 10px; background: rgba(255,255,255,0.45); box-shadow: 0 0 5px #a7a7a7; backdrop-filter: blur(6px); -webkit-backdrop-filter: blur(6px); display: flex; flex-direction: row; justify-content: space-between; align-items: center; font-size: 60px; font-weight: 700; line-height: 1; }}
.dateInfo span {{ margin-left: 16px; margin-right: 16px; white-space: nowrap; }}
.dateInfo .date {{ color: #666; }}
.hitokoto {{ color: #838383; font-size: 16px; line-height: 1.45; text-align: center; padding-top: 18px; min-height: 72px; }}
.content {{ padding: 0 30px; }}
.signin {{ color: #838383; margin-top: 0; margin-bottom: 8px; font-size: 20px; font-weight: 600; }}
.icon {{ width: 1.2em; height: 1.2em; vertical-align: -0.2em; display: inline-block; }}
.levelInfo {{ display: flex; flex-direction: row; justify-content: space-between; font-size: 30px; font-weight: 800; line-height: 1.18; white-space: nowrap; align-items: baseline; color: #000; }}
.levelInfo .exp {{ color: #b4b1b1; }}
.level-bar {{ margin-top: 2px; display: flex; align-items: center; }}
.bar-container {{ width: 100%; background: #e0e0e0; border-radius: 5px; overflow: hidden; }}
.progress {{ width: {view["progress"]}%; background: #666; padding: 5px 0; border-radius: 5px 0 0 5px; height: 29px; }}
.fortune {{ display: flex; flex-direction: row; justify-content: space-between; align-items: center; margin-top: 10px; margin-right: 5px; }}
.fortune .luck {{ font-size: 36px; font-weight: 800; color: #000; }}
.fortune .desc {{ font-size: 28px; color: #838383; white-space: nowrap; font-weight: 600; }}
hr {{ border: 0; border-top: 1px solid #bcbcbc; margin: 10px 0 0; }}
.toDo {{ display: flex; flex-direction: row; margin-top: 18px; align-items: center; }}
.toDo p {{ margin: 0 0 0 20px; font-size: 20px; line-height: 1.28; font-weight: 600; }}
.goodText {{ color: #4b3732; text-shadow: 0 0 1px #ffbbbb; }}
.badText {{ color: #343a43; text-shadow: 0 0 1px #bcdbff; }}
.toDoBg {{ border-radius: 50%; width: 48px; height: 48px; min-width: 48px; display: flex; justify-content: center; align-items: center; }}
.toDoBg span {{ font-size: 32px; font-weight: 800; color: #fff; line-height: 1; }}
.credit {{ margin-top: 4px; text-align: center; color: #999; font-size: 12px; padding: 8px 10px 10px; }}
</style>
</head>
<body id="body">
  <div class="container">
<img class="hero" src="{background}" alt="background">
<div class="header">
  <img class="avatar" src="{avatar}" alt="avatar">
  <div class="dateInfo">
    <span>{escape(view["greeting"])}</span>
    <span class="date">{escape(view["date"])}</span>
  </div>
</div>
<div class="hitokoto">{hitokoto}</div>
<div class="content">
  <div class="signin">{status} {reward}</div>
  <div class="levelInfo">
    <span style="color:{view["level"]["levelColor"]}">{escape(view["level"]["levelName"])}</span>
    <span class="exp">{escape(view["exp_text"])}</span>
  </div>
  <div class="level-bar"><div class="bar-container"><div class="progress"></div></div></div>
  <div class="fortune">
    <span class="luck">{_emoji_svg("1f340")} {escape(view["luck"])}</span>
    <span class="desc">{_emoji_svg("1f320")} {escape(view["fortune_desc"])}</span>
  </div>
  <hr>
  <div class="toDo"><div class="toDoBg" style="background-color:#D94A3F;"><span>宜</span></div><p class="goodText">{gooddo}</p></div>
  <div class="toDo"><div class="toDoBg" style="background-color:#000;"><span>忌</span></div><p class="badText">{baddo}</p></div>
</div>
<div class="credit">随机生成 请勿迷信 | AstrBot</div>
  </div>
</body>
</html>"""


def _browser_path() -> str | None:
    if os.name == "nt":
        for root in (os.environ.get("PROGRAMFILES"), os.environ.get("PROGRAMFILES(X86)"), os.environ.get("LOCALAPPDATA")):
            if not root:
                continue
            for suffix in ("Google/Chrome/Application/chrome.exe", "Microsoft/Edge/Application/msedge.exe"):
                path = Path(root) / suffix
                if path.is_file():
                    return str(path)
    return shutil.which("chromium") or shutil.which("chromium-browser") or shutil.which("google-chrome")


async def _render_png(html_content: str) -> bytes:
    from playwright.async_api import async_playwright

    async with async_playwright() as driver:
        browser = await driver.chromium.launch(
            headless=True, executable_path=_browser_path(), timeout=4500,
            args=["--no-sandbox", "--disable-dev-shm-usage"],
        )
        try:
            page = await browser.new_page(viewport={"width": 600, "height": 1200}, device_scale_factor=1)
            await page.route("**/*", lambda route: route.abort())
            await page.set_content(html_content, wait_until="load", timeout=2500)
            await page.evaluate("async () => { await document.fonts.ready; await Promise.all(Array.from(document.images, i => i.decode())); }")
            return await page.locator(".container").screenshot(type="png", animations="disabled", timeout=2000)
        finally:
            await browser.close()


# 同时最多启动 2 个 Chromium:群里有人刷签到时,没有上限会把 CPU 和内存拉满。
# 等不到名额(2 秒)就抛错,调用方会自动改回复文字结果,签到奖励照常入账。
_RENDER_SLOTS = threading.BoundedSemaphore(2)
_RENDER_WAIT_SECONDS = 2


def render_card(view: Mapping[str, Any]) -> str:
    if not _RENDER_SLOTS.acquire(timeout=_RENDER_WAIT_SECONDS):
        raise RuntimeError("fortune_render_busy")
    try:
        return _render_card(view)
    finally:
        _RENDER_SLOTS.release()


def _render_card(view: Mapping[str, Any]) -> str:
    async def render() -> bytes:
        # Fetch in parallel so the 3-second image budget plus 7-second browser
        # budget fits inside the plugin's 12-second execution timeout.
        avatar_uri, background_uri = await asyncio.gather(
            asyncio.to_thread(_avatar_data_uri, str(view.get("avatar_url") or "")),
            asyncio.to_thread(_background_data_uri, str(view.get("background_url") or "")),
        )
        prepared = {**view, "avatar_data_uri": avatar_uri, "background_data_uri": background_uri}
        return await asyncio.wait_for(_render_png(build_card_html(prepared)), timeout=7)

    return base64.b64encode(asyncio.run(render())).decode("ascii")
