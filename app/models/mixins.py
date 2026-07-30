from sqlalchemy import DateTime
from sqlalchemy.orm import Mapped, mapped_column

from app.core.time import now_shanghai


class TimestampMixin:
    """为业务模型统一提供上海时区语义下的创建和更新时间。"""

    created_at: Mapped[DateTime] = mapped_column(DateTime(timezone=True), default=now_shanghai)
    updated_at: Mapped[DateTime] = mapped_column(DateTime(timezone=True), default=now_shanghai, onupdate=now_shanghai)
