from __future__ import annotations

import os
from pathlib import Path

from sqlalchemy import create_engine, select
from sqlalchemy.orm import sessionmaker


PROJECT_ROOT = Path(__file__).resolve().parents[1]
TEST_RUNTIME = PROJECT_ROOT / ".run" / "test-runtime" / "existing-stateful-plugins"

os.environ["APP_ENV"] = "test"
os.environ["DATA_DIR"] = str(TEST_RUNTIME / "data")
os.environ["DATABASE_URL"] = f"sqlite:///{TEST_RUNTIME / 'app.db'}"
os.environ["LOG_DIR"] = str(TEST_RUNTIME / "logs")
os.environ["AUTH_SEED_DEMO_USERS"] = "false"

from app.core.db import Base
from app.core.tenant_context import tenant_scope
from app.core.tenant_db import tenant_session
from app.models.entities import PointsAccount, PointsTransaction
from app.services.points import PointsService


def test_points_service_idempotency_prevents_duplicate_balance_changes(tmp_path: Path) -> None:
    """同一租户业务键重复提交时只产生一条积分流水。"""

    engine = create_engine(f"sqlite:///{tmp_path / 'points-idempotency.db'}")
    Base.metadata.create_all(bind=engine)
    factory = sessionmaker(bind=engine, autoflush=False, expire_on_commit=False)
    try:
        with tenant_scope(1), tenant_session(factory) as db:
            service = PointsService(db)
            first = service.add_points(
                "user-1",
                "group-1",
                10,
                source="game_task",
                idempotency_key="game-task:conversation-1:task-1:reward",
            )
            repeated = service.add_points(
                "user-1",
                "group-1",
                10,
                source="game_task",
                idempotency_key="game-task:conversation-1:task-1:reward",
            )
            account = db.scalar(select(PointsAccount).where(PointsAccount.user_id == "user-1"))
            transactions = db.scalars(select(PointsTransaction)).all()

        assert first.ok is True
        assert repeated.ok is True
        assert repeated.extra and repeated.extra["idempotent"] is True
        assert account is not None and account.total_points == 10
        assert len(transactions) == 1
        assert transactions[0].idempotency_key == "game-task:conversation-1:task-1:reward"
    finally:
        Base.metadata.drop_all(bind=engine)
        engine.dispose()
