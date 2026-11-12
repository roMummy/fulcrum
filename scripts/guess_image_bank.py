"""Explicit-tenant offline administration of an online-obtained ZIP; never auto-select a tenant."""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.core.tenant_context import tenant_scope
from app.core.tenant_db import tenant_session
from app.models.saas import Tenant
from app.services import guess_image_bank as bank


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("archive", type=Path)
    parser.add_argument("--tenant-id", required=True, type=int)
    parser.add_argument("--commit", action="store_true", help="Default validates only; explicitly commit to the selected tenant")
    args = parser.parse_args()
    if args.archive.stat().st_size > bank.MAX_ZIP:
        parser.error("ZIP exceeds 20 MiB")
    data = args.archive.read_bytes()
    rows, _ = bank.read_archive(data)
    with tenant_scope(args.tenant_id), tenant_session() as db:
        tenant = db.get(Tenant, args.tenant_id)
        if tenant is None or tenant.status != "active":
            parser.error("Selected tenant does not exist or is inactive")
        if not args.commit:
            print(json.dumps({"tenant_id": args.tenant_id, "valid_questions": len(rows), "committed": False}))
            return
        token, _ = bank.preview_import(db, data, user_id=0, session_id=0)
        report = bank.commit_import(db, token, user_id=0, session_id=0)
        from app.services.ops import record_audit
        record_audit(db, "guess-image.cli-import", "guess_image_question", detail=json.dumps(report), actor="cli")
        print(json.dumps({"tenant_id": args.tenant_id, **report, "committed": True}))


if __name__ == "__main__":
    main()
