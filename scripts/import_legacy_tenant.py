from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Sequence


# 允许从项目根目录直接执行 ``.venv/bin/python scripts/import_legacy_tenant.py``，
# 不依赖调用方预先设置 PYTHONPATH，也不会改变应用读取 .env 的工作目录语义。
PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from app.core.db import SessionLocal  # noqa: E402
from app.services.legacy_import import run_legacy_import  # noqa: E402


def build_parser() -> argparse.ArgumentParser:
    """构造显式参数解析器，目标租户和来源数据库都不允许自动猜测。"""

    parser = argparse.ArgumentParser(description="把旧 Multi Bot Admin 核心数据导入指定 SaaS 租户")
    parser.add_argument("--source-url", required=True, help="旧系统只读数据库 URL；建议使用专用只读账号")
    parser.add_argument("--tenant-code", required=True, help="目标 SaaS 租户编码")
    parser.add_argument(
        "--report-path",
        default=".run/legacy-import-report.json",
        help="机器可读 JSON 报告路径，默认写入 .run/legacy-import-report.json",
    )
    parser.add_argument("--dry-run", action="store_true", help="只执行校验和计数，不写目标数据库")
    return parser


def _write_report(report_path: str, report: dict[str, object]) -> Path:
    """先写临时文件再替换正式报告，避免中断时留下半截 JSON。"""

    resolved_path = Path(report_path).expanduser().resolve()
    resolved_path.parent.mkdir(parents=True, exist_ok=True)
    temporary_path = resolved_path.with_name(f".{resolved_path.name}.tmp")
    temporary_path.write_text(json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    temporary_path.replace(resolved_path)
    return resolved_path


def main(argv: Sequence[str] | None = None) -> int:
    """运行导入并返回稳定退出码：成功为 0，校验或导入失败为 2。"""

    arguments = build_parser().parse_args(argv)
    report = run_legacy_import(
        source_url=arguments.source_url,
        tenant_code=arguments.tenant_code,
        target_session_factory=SessionLocal,
        dry_run=bool(arguments.dry_run),
    )
    report_path = _write_report(arguments.report_path, report)

    print(f"mode={report['mode']}")
    print(f"success={str(bool(report['success'])).lower()}")
    print(f"source_fingerprint={report['source_fingerprint']}")
    for table_name, stats in report["tables"].items():
        print(
            f"{table_name}: read={stats['read']} written={stats['written']} "
            f"skipped={stats['skipped']} errors={stats['errors']}"
        )
    print(f"report={report_path}")
    if report["errors"]:
        print("errors=" + ",".join(report["errors"]))
    return 0 if report["success"] else 2


if __name__ == "__main__":
    raise SystemExit(main())
