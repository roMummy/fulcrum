"""Tenant-owned question bank. Gameplay keeps no historical question statistics."""
from __future__ import annotations

from sqlalchemy import CheckConstraint, DateTime, Index, Integer, String, Text, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from app.core.db import Base
from app.models.mixins import TenantOwnedMixin, TimestampMixin


class GuessImageQuestion(Base, TenantOwnedMixin, TimestampMixin):
    __tablename__ = "guess_image_questions"
    __table_args__ = (
        UniqueConstraint("tenant_id", "question_id", name="uq_guess_image_question_tenant_uuid"),
        CheckConstraint("status IN ('draft', 'enabled', 'disabled')", name="ck_guess_image_question_status"),
        Index("ix_guess_image_bank_status", "tenant_id", "status", "category"),
    )
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    question_id: Mapped[str] = mapped_column(String(36), nullable=False)
    asset_id: Mapped[str] = mapped_column(String(64), nullable=False)
    answer: Mapped[str] = mapped_column(String(80), nullable=False)
    category: Mapped[str] = mapped_column(String(40), nullable=False, default="")
    tags_json: Mapped[str] = mapped_column(Text, nullable=False, default="[]")
    source: Mapped[str] = mapped_column(String(500), nullable=False, default="")
    license: Mapped[str] = mapped_column(String(500), nullable=False, default="")
    status: Mapped[str] = mapped_column(String(16), nullable=False, default="draft")


class GuessImageImport(Base, TenantOwnedMixin, TimestampMixin):
    __tablename__ = "guess_image_imports"
    __table_args__ = (UniqueConstraint("tenant_id", "token_hash", name="uq_guess_image_import_token"),)
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    token_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    user_id: Mapped[int] = mapped_column(Integer, nullable=False)
    session_id: Mapped[int] = mapped_column(Integer, nullable=False)
    expires_at: Mapped[DateTime] = mapped_column(DateTime(timezone=True), nullable=False)
    status: Mapped[str] = mapped_column(String(16), nullable=False, default="preview")
    report_json: Mapped[str] = mapped_column(Text, nullable=False, default="{}")
