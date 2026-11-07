from __future__ import annotations

import json
import sys
from pathlib import Path

from sqlalchemy import select

# 支持从项目根目录直接执行脚本，不要求运维人员额外设置 PYTHONPATH。
PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from app.core.db import SessionLocal, TenantSessionLocal
from app.core.tenant_context import tenant_scope
from app.core.tenant_db import platform_session, tenant_session
from app.models.saas import Tenant
from app.services.plugin_secret_cleanup import scrub_tenant_plugin_plaintext_secrets


def main() -> int:
    """逐租户清理插件普通配置明文，只输出数量统计而不输出字段值。"""

    with platform_session(SessionLocal) as db:
        tenant_ids = [int(item) for item in db.scalars(select(Tenant.id).order_by(Tenant.id.asc())).all()]

    summary = {
        "tenant_count": len(tenant_ids),
        "scanned_bindings": 0,
        "changed_bindings": 0,
        "removed_fields": 0,
    }
    for tenant_id in tenant_ids:
        with tenant_scope(tenant_id), tenant_session(TenantSessionLocal) as db:
            tenant_result = scrub_tenant_plugin_plaintext_secrets(db)
            db.commit()
        for key in ("scanned_bindings", "changed_bindings", "removed_fields"):
            summary[key] += int(tenant_result[key])

    print(json.dumps(summary, ensure_ascii=False, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
