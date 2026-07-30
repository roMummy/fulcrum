from __future__ import annotations

import secrets
from pathlib import Path
from urllib.parse import quote_plus

from sqlalchemy import create_engine, select, text
from sqlalchemy.orm import Session, sessionmaker

from app.core.db import Base
from app.core.settings import settings
from app.models.entities import AdminUser
from app.services.auth import hash_password_with_secret


APP_ROOT = Path(__file__).resolve().parents[2]
DEFAULT_SQLITE_PATH = (APP_ROOT / "data" / "app.db").resolve()


def is_setup_required(db: Session) -> bool:
    admin_count = db.scalar(select(AdminUser).where(AdminUser.role == "admin", AdminUser.is_enabled.is_(True)).limit(1))
    return admin_count is None


def build_default_sqlite_url() -> str:
    return f"sqlite:///{DEFAULT_SQLITE_PATH.as_posix()}"


def generate_secret_key() -> str:
    return secrets.token_urlsafe(32)


def build_mariadb_url(host: str, port: int, database: str, username: str, password: str) -> str:
    return (
        f"mysql+pymysql://{quote_plus(username)}:{quote_plus(password)}@{host}:{port}/{database}?charset=utf8mb4"
    )


def validate_database_connection(database_url: str) -> str | None:
    engine = None
    try:
        engine = create_engine(database_url, pool_pre_ping=True)
        with engine.connect() as connection:
            connection.execute(text("SELECT 1"))
        return None
    except Exception as exc:
        return str(exc)
    finally:
        if engine is not None:
            engine.dispose()


def provision_database(database_url: str, *, admin_username: str, admin_password: str, admin_display_name: str, secret_key: str) -> str | None:
    engine = None
    session_factory = None
    db = None
    try:
        engine = create_engine(database_url, pool_pre_ping=not database_url.startswith("sqlite"))
        Base.metadata.create_all(bind=engine)
        session_factory = sessionmaker(autocommit=False, autoflush=False, bind=engine)
        db = session_factory()
        existing_admin = db.scalar(select(AdminUser).where(AdminUser.role == "admin", AdminUser.is_enabled.is_(True)).limit(1))
        if existing_admin is None:
            db.add(
                AdminUser(
                    username=admin_username,
                    display_name=admin_display_name,
                    password_hash=hash_password_with_secret(secret_key, admin_password),
                    role="admin",
                    is_enabled=True,
                    auth_version=1,
                )
            )
            db.commit()
        return None
    except Exception as exc:
        if db is not None:
            db.rollback()
        return str(exc)
    finally:
        if db is not None:
            db.close()
        if engine is not None:
            engine.dispose()


def write_env_file(values: dict[str, str], *, env_path: Path | None = None) -> None:
    target_path = env_path or (APP_ROOT / ".env")
    existing: dict[str, str] = {}
    if target_path.exists():
        for line in target_path.read_text(encoding="utf-8").splitlines():
            normalized = line.strip()
            if not normalized or normalized.startswith("#") or "=" not in normalized:
                continue
            key, value = normalized.split("=", 1)
            existing[key.strip()] = value.strip()
    existing.update(values)
    lines = [f"{key}={_format_env_value(value)}" for key, value in sorted(existing.items())]
    target_path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def _format_env_value(value: str) -> str:
    text = str(value or "")
    escaped = text.replace("\\", "\\\\").replace('"', '\\"')
    return f'"{escaped}"'
