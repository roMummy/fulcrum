from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models.entities import AdminUser
from app.core.settings import settings
from app.services.auth import hash_password


DEFAULT_DEMO_USERS = [
    {"username": "admin", "display_name": "系统管理员", "password": "admin123", "role": "admin"},
    {"username": "ops", "display_name": "运维账号", "password": "ops123", "role": "ops"},
    {"username": "viewer", "display_name": "只读账号", "password": "viewer123", "role": "readonly"},
]


def seed_auth_users(db: Session):
    existing_users = db.scalars(select(AdminUser)).all()
    has_any_user = bool(existing_users)
    users_to_create: list[AdminUser] = []
    # AUTH_BOOTSTRAP_ADMIN_* 现在只用于独立的 platform_users 平台身份；旧 AdminUser
    # 仅在显式演示模式下创建，避免生产环境出现一套无法用于 SaaS 平台登录的影子管理员。
    if not has_any_user and settings.should_seed_demo_users:
        users_to_create.extend(
            [
                AdminUser(
                    username=item["username"],
                    display_name=item["display_name"],
                    password_hash=hash_password(item["password"]),
                    role=item["role"],
                    is_enabled=True,
                )
                for item in DEFAULT_DEMO_USERS
                if item["username"] not in {user.username for user in users_to_create}
            ]
        )

    if users_to_create:
        db.add_all(users_to_create)
        db.commit()
