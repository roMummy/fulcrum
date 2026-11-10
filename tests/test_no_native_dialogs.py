"""项目统一使用 dialog.js 的自绘弹窗,不允许再出现浏览器默认的 confirm / alert / prompt。"""

from __future__ import annotations

import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
NATIVE = re.compile(r"(?<![\w.])(?:window\.)?(?:confirm|alert|prompt)\(")


def _sources():
    for base in ("app/templates", "app/static/js"):
        for path in (ROOT / base).rglob("*"):
            if path.suffix in {".html", ".js"} and path.name != "dialog.js":
                yield path


def test_no_native_browser_dialogs_in_templates_or_scripts() -> None:
    offenders = []
    for path in _sources():
        for number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), start=1):
            if NATIVE.search(line) and "appDialog" not in line:
                offenders.append(f"{path.relative_to(ROOT)}:{number}: {line.strip()[:80]}")
    assert not offenders, "请改用 appDialog.confirm/alert 或 data-confirm-*:\n" + "\n".join(offenders)


def test_base_template_loads_the_shared_dialog_script() -> None:
    base = (ROOT / "app/templates/base.html").read_text(encoding="utf-8")
    assert "/static/js/dialog.js" in base
    # 旧的确认框标记和内联脚本已删除,只保留统一实现。
    assert "confirm-action-modal" not in base
