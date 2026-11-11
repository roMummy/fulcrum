from __future__ import annotations

import asyncio
import os
import time
from pathlib import Path

import markdown
import psutil
from playwright.async_api import async_playwright


WINDOWS_CHROME_PATHS = (
    r"C:\Users\Liu_WIN10\AppData\Local\Google\Chrome\Application\chrome.exe",
    r"C:\Users\Liu-OPEN\AppData\Local\Google\Chrome\Application\chrome.exe",
    r"C:\Program Files\Google\Chrome\Application\chrome.exe",
    r"C:\Program Files (x86)\Google\Chrome\Application\chrome.exe",
)


async def safe_close_browser(browser, timeout: float = 4.0) -> None:
    if not browser:
        return

    for context in browser.contexts[:]:
        for page in context.pages[:]:
            try:
                await asyncio.wait_for(page.close(), timeout=1.5)
            except Exception:
                pass
        try:
            await asyncio.wait_for(context.close(), timeout=timeout)
        except Exception:
            pass

    try:
        await asyncio.wait_for(browser.close(), timeout=timeout)
        return
    except Exception:
        pass

    if getattr(browser, "process", None) and getattr(browser.process, "pid", None):
        try:
            parent = psutil.Process(browser.process.pid)
            children = parent.children(recursive=True)
            for proc in children:
                try:
                    proc.terminate()
                except (psutil.NoSuchProcess, psutil.AccessDenied, psutil.ZombieProcess):
                    pass
            try:
                parent.terminate()
            except (psutil.NoSuchProcess, psutil.AccessDenied, psutil.ZombieProcess):
                pass
            try:
                _, alive = psutil.wait_procs([parent, *children], timeout=2)
            except Exception:
                alive = [parent, *children]
            for proc in alive:
                try:
                    proc.kill()
                except (psutil.NoSuchProcess, psutil.AccessDenied, psutil.ZombieProcess):
                    pass
        except (psutil.NoSuchProcess, Exception):
            pass


async def md_str_to_html_content(md_content: str) -> str:
    html_body = markdown.markdown(md_content, extensions=["extra", "codehilite"])
    css = """
    <style>
        body {
            font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, 'Noto Sans CJK SC', 'Microsoft YaHei', sans-serif;
            padding: 24px 28px;
            line-height: 1.7;
            max-width: 780px;
            margin: 0 auto;
            background: linear-gradient(180deg, #f7f3eb 0%, #fbfaf7 100%);
            color: #2f241a;
            font-size: 16px;
        }
        .sheet {
            background: #fffdf8;
            border: 1px solid #eadfcd;
            border-radius: 22px;
            box-shadow: 0 18px 60px rgba(83, 63, 34, 0.12);
            padding: 28px 30px 24px;
        }
        h1, h2, h3, h4, h5, h6 {
            color: #20170f;
            margin-top: 22px;
            margin-bottom: 14px;
            font-weight: 700;
            line-height: 1.35;
        }
        h1 {
            font-size: 30px;
            text-align: center;
            margin-top: 0;
            padding-bottom: 14px;
            border-bottom: 1px solid #efe3d2;
        }
        h2 {
            font-size: 23px;
            padding-left: 12px;
            border-left: 5px solid #c58d47;
        }
        h3 {
            font-size: 19px;
            color: #6f4518;
        }
        p {
            margin: 14px 0;
            white-space: pre-wrap;
        }
        ul, ol {
            margin: 14px 0;
            padding-left: 24px;
        }
        li {
            margin: 8px 0;
        }
        strong {
            color: #5b3712;
        }
        blockquote {
            margin: 16px 0;
            padding: 12px 16px;
            background: #fbf4ea;
            border-left: 4px solid #d29d59;
            border-radius: 10px;
            color: #5b4a36;
        }
        code {
            background: #f6eee2;
            border-radius: 6px;
            padding: 2px 6px;
        }
        pre {
            background: #f6eee2;
            border-radius: 12px;
            padding: 14px;
            overflow: hidden;
        }
        table {
            width: 100%;
            border-collapse: collapse;
            margin: 16px 0;
            background: white;
        }
        th, td {
            border: 1px solid #efdfc9;
            padding: 10px 12px;
            text-align: left;
        }
        th {
            background: #fbf2e5;
        }
        hr {
            border: none;
            border-top: 1px solid #efe3d2;
            margin: 20px 0;
        }
        .footer {
            margin-top: 20px;
            text-align: right;
            color: #8c7355;
            font-size: 13px;
        }
    </style>
    """
    return f"""<html>
    <head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    {css}
    </head>
    <body>
      <div class="sheet">
        {html_body}
        <div class="footer">Generated by fulcrum</div>
      </div>
    </body>
    </html>"""


def _resolve_browser_path() -> str | None:
    if os.name == "nt":
        for path in WINDOWS_CHROME_PATHS:
            if os.path.isfile(path) and os.access(path, os.X_OK):
                return path
        return None
    return None


async def html_to_image(html_content: str, output_image: str) -> None:
    async with async_playwright() as p:
        launch_args = [
            "--no-sandbox",
            "--disable-setuid-sandbox",
            "--disable-dev-shm-usage",
        ]
        browser_path = _resolve_browser_path()
        if browser_path:
            browser = await p.chromium.launch(executable_path=browser_path, args=launch_args)
        else:
            browser = await p.chromium.launch(args=launch_args)

        try:
            context = await browser.new_context(
                viewport={"width": 820, "height": 600},
                device_scale_factor=1.5,
            )
            page = await context.new_page()
            await page.set_content(html_content, wait_until="load")
            await page.evaluate("document.fonts.ready")
            await asyncio.sleep(0.4)
            body_height = await page.evaluate(
                "Math.max(document.body.scrollHeight, document.documentElement.scrollHeight)"
            )
            await page.set_viewport_size({"width": 820, "height": max(600, int(body_height) + 24)})
            await page.screenshot(
                path=output_image,
                full_page=True,
                timeout=30000,
                animations="disabled",
            )
            if not os.path.exists(output_image):
                raise RuntimeError(f"image render failed: {output_image}")
        finally:
            await safe_close_browser(browser)


async def convert_md_str_to_image(md_content: str, output_image: str, *, base_dir: str | Path | None = None) -> str:
    if not str(md_content or "").strip():
        raise ValueError("Markdown content cannot be empty")
    root_dir = Path(base_dir) if base_dir else Path.cwd()
    output_dir = root_dir / "data" / "temp" / "md2image"
    output_dir.mkdir(parents=True, exist_ok=True)
    output_path = output_dir / output_image
    html_content = await md_str_to_html_content(md_content)
    await html_to_image(html_content, str(output_path))
    return str(output_path.resolve())


def convert_md_str_to_image_sync(md_content: str, output_image: str | None = None, *, base_dir: str | Path | None = None) -> str:
    file_name = output_image or f"summary_{int(time.time() * 1000)}.png"
    return asyncio.run(convert_md_str_to_image(md_content, file_name, base_dir=base_dir))
