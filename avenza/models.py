from __future__ import annotations

from datetime import datetime

from sqlalchemy import (
    JSON,
    BigInteger,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    LargeBinary,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship

from .timeutil import utcnow


class Base(DeclarativeBase):
    pass


class User(Base):
    __tablename__ = "users"

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    display_name: Mapped[str] = mapped_column(String(200))
    language: Mapped[str] = mapped_column(String(16), default="en")
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)

    devices: Mapped[list["Device"]] = relationship(back_populates="user")


class Device(Base):
    __tablename__ = "devices"

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    user_id: Mapped[str] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), index=True)
    credential_id: Mapped[str] = mapped_column(String(1024), unique=True, index=True)
    public_key: Mapped[bytes] = mapped_column(LargeBinary)
    sign_count: Mapped[int] = mapped_column(Integer, default=0)
    transports: Mapped[str] = mapped_column(String(200), default="")
    aaguid: Mapped[str] = mapped_column(String(64), default="")
    device_token_hash: Mapped[str] = mapped_column(String(128), unique=True, index=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    last_used_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)

    user: Mapped[User] = relationship(back_populates="devices")


class Consent(Base):
    __tablename__ = "consents"

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    user_id: Mapped[str] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), index=True)
    wording_version: Mapped[str] = mapped_column(String(32))
    language: Mapped[str] = mapped_column(String(16), default="en")
    timestamp: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    ip: Mapped[str] = mapped_column(String(64), default="")
    user_agent: Mapped[str] = mapped_column(String(500), default="")


class Session(Base):
    __tablename__ = "sessions"

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    user_id: Mapped[str] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), index=True)
    device_id: Mapped[str] = mapped_column(ForeignKey("devices.id", ondelete="CASCADE"), index=True)
    consent_id: Mapped[str] = mapped_column(ForeignKey("consents.id"))
    language: Mapped[str] = mapped_column(String(16), default="en")
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    expires_at: Mapped[datetime] = mapped_column(DateTime)
    last_seen_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    closed_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)


class Transfer(Base):
    __tablename__ = "transfers"

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    user_id: Mapped[str] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), index=True)
    session_id: Mapped[str] = mapped_column(ForeignKey("sessions.id"), index=True)

    idempotency_key: Mapped[str] = mapped_column(String(80), unique=True, index=True)
    state: Mapped[str] = mapped_column(String(32), index=True)

    amount_kobo: Mapped[int] = mapped_column(BigInteger)
    bank_code: Mapped[str] = mapped_column(String(16))
    account_number: Mapped[str] = mapped_column(String(32))
    verified_recipient_name: Mapped[str] = mapped_column(String(200), default="")
    recipient_hint: Mapped[str] = mapped_column(String(200), default="")
    readback_text: Mapped[str] = mapped_column(Text, default="")

    challenge: Mapped[str] = mapped_column(String(128), default="")
    challenge_payload: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    challenge_hash: Mapped[str] = mapped_column(String(128), default="")
    signed_approval: Mapped[str | None] = mapped_column(Text, nullable=True)
    approval_used_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)

    bank_reference: Mapped[str | None] = mapped_column(String(80), nullable=True)
    nonce: Mapped[str] = mapped_column(String(64), unique=True)
    confirmations_required: Mapped[int] = mapped_column(Integer, default=1)
    confirmations_done: Mapped[int] = mapped_column(Integer, default=0)
    risk_flags: Mapped[list] = mapped_column(JSON, default=list)
    duplicate_of: Mapped[str | None] = mapped_column(String(64), nullable=True)
    risk_hold_until: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)

    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, onupdate=utcnow)
    expires_at: Mapped[datetime] = mapped_column(DateTime)
    approval_expires_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    submitted_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    settled_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    failure_reason: Mapped[str | None] = mapped_column(String(300), nullable=True)
    bank_status: Mapped[dict | None] = mapped_column(JSON, nullable=True)

    __table_args__ = (
        Index("ix_transfers_user_created", "user_id", "created_at"),
        Index("ix_transfers_recipient", "account_number", "bank_code"),
    )


class AuditLog(Base):
    __tablename__ = "audit_log"

    seq: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    id: Mapped[str] = mapped_column(String(64), unique=True, index=True)
    transfer_id: Mapped[str | None] = mapped_column(String(64), index=True, nullable=True)
    session_id: Mapped[str | None] = mapped_column(String(64), index=True, nullable=True)
    event: Mapped[str] = mapped_column(String(80), index=True)
    details: Mapped[dict] = mapped_column(JSON, default=dict)
    previous_hash: Mapped[str] = mapped_column(String(64))
    row_hash: Mapped[str] = mapped_column(String(64))
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)


class Lock(Base):
    __tablename__ = "locks"

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    user_id: Mapped[str] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), index=True)
    locked_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    unlocked_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    reason: Mapped[str] = mapped_column(String(200), default="customer_request")


class WebAuthnChallenge(Base):
    __tablename__ = "webauthn_challenges"

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    user_id: Mapped[str] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), index=True)
    device_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    kind: Mapped[str] = mapped_column(String(32), index=True)
    challenge: Mapped[str] = mapped_column(String(256))
    expires_at: Mapped[datetime] = mapped_column(DateTime)
    used_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)


class BankAccount(Base):
    __tablename__ = "bank_accounts"

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    bank_code: Mapped[str] = mapped_column(String(16), index=True)
    bank_name: Mapped[str] = mapped_column(String(100))
    account_number: Mapped[str] = mapped_column(String(32), index=True)
    account_name: Mapped[str] = mapped_column(String(200))

    __table_args__ = (UniqueConstraint("bank_code", "account_number", name="uq_bank_account"),)


class BankTransfer(Base):
    """The simulated bank's own ledger. Avenza never stores funds."""

    __tablename__ = "bank_transfers"

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    idempotency_key: Mapped[str] = mapped_column(String(80), unique=True, index=True)
    customer_ref: Mapped[str] = mapped_column(String(64), index=True)
    bank_code: Mapped[str] = mapped_column(String(16))
    account_number: Mapped[str] = mapped_column(String(32))
    recipient_name: Mapped[str] = mapped_column(String(200))
    amount_kobo: Mapped[int] = mapped_column(BigInteger)
    status: Mapped[str] = mapped_column(String(24), index=True)
    reference: Mapped[str] = mapped_column(String(80), unique=True, index=True)
    failure_reason: Mapped[str | None] = mapped_column(String(200), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    settled_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)


class BankControl(Base):
    __tablename__ = "bank_control"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    mode: Mapped[str] = mapped_column(String(40), default="normal")
    delay_ms: Mapped[int] = mapped_column(Integer, default=0)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, onupdate=utcnow)
