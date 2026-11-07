from __future__ import annotations

import html
from pathlib import Path
from typing import Any, Mapping, Sequence


class PdfDependencyError(RuntimeError):
    """当前运行镜像没有可用的浏览器 PDF 渲染依赖。"""


def build_pdf_html(title: str, movies: Sequence[Mapping[str, Any]]) -> str:
    """生成只包含结构化影片字段的 HTML，不把上游原始页面直接嵌入 PDF。"""

    cards: list[str] = []
    for movie in movies:
        rows = []
        for label, key in (("番号", "code"), ("标题", "title"), ("发行日期", "release_date"), ("详情", "detail_url")):
            value = html.escape(str(movie.get(key) or "").strip())
            if value:
                rows.append(f"<p><strong>{label}：</strong>{value}</p>")
        cards.append(f"<section class='movie'>{''.join(rows)}</section>")
    body = "".join(cards) or "<p>本次没有可用影片。</p>"
    return f"""<!doctype html><html><head><meta charset='utf-8'><style>
body{{font-family:Arial,'Noto Sans CJK SC',sans-serif;color:#241b14;padding:24px;}}
h1{{font-size:24px;border-bottom:1px solid #d7c4ae;padding-bottom:12px;}}
.movie{{break-inside:avoid;border:1px solid #e4d8cb;border-radius:8px;padding:12px;margin:0 0 12px;}}
.movie p{{margin:4px 0;font-size:12px;line-height:1.5;word-break:break-all;}}
strong{{color:#6e431e;}}
</style></head><body><h1>{html.escape(title)}</h1>{body}</body></html>"""


def render_html_to_pdf(html_content: str, output_path: str | Path, *, launcher: Any | None = None) -> Path:
    """使用已安装的 Playwright Chromium 输出 PDF；缺依赖时明确失败。"""

    target = Path(output_path).expanduser()
    target.parent.mkdir(parents=True, exist_ok=True)
    try:
        from playwright.sync_api import sync_playwright
    except ImportError as exc:  # pragma: no cover - 运行镜像依赖缺失时走稳定错误。
        raise PdfDependencyError("PDF 渲染依赖不可用") from exc

    try:
        with sync_playwright() as playwright:
            browser = launcher(playwright) if launcher else playwright.chromium.launch(args=["--no-sandbox", "--disable-dev-shm-usage"])
            try:
                page = browser.new_page()
                page.set_content(html_content, wait_until="load")
                page.pdf(path=str(target), format="A4", print_background=True)
            finally:
                browser.close()
    except Exception as exc:
        raise PdfDependencyError("PDF 渲染依赖不可用或浏览器启动失败") from exc
    if not target.is_file() or target.stat().st_size <= 0:
        raise PdfDependencyError("PDF 渲染未生成有效文件")
    return target.resolve(strict=True)

