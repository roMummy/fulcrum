from __future__ import annotations

from scripts.saas_business_smoke import run_business_smoke


def test_two_tenant_provider_flow_isolated_across_restart() -> None:
    """双租户同名资源、节点任务、到期策略和重建恢复必须保持完整隔离。"""

    result = run_business_smoke()

    assert result == {
        "tenants": 2,
        "provider_nodes": 2,
        "completed_jobs": 3,
        "expired_pending_jobs": 1,
        "restart_verified": True,
    }
