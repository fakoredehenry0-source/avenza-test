from __future__ import annotations

import hashlib
import json
import threading
import uuid
from contextlib import contextmanager
from datetime import datetime
from typing import Any, Iterator

from sqlalchemy import select, text
from sqlalchemy.orm import Session, sessionmaker

from .models import AuditLog
from .timeutil import iso, utcnow

GENESIS_HASH = "0" * 64


def canonical_json(value: Any) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), default=str, ensure_ascii=False)


def compute_row_hash(
    *,
    entry_id: str,
    event: str,
    transfer_id: str | None,
    session_id: str | None,
    details: dict,
    previous_hash: str,
    created_at: datetime,
) -> str:
    payload = {
        "id": entry_id,
        "event": event,
        "transfer_id": transfer_id,
        "session_id": session_id,
        "details": details,
        "previous_hash": previous_hash,
        "created_at": iso(created_at),
    }
    return hashlib.sha256(canonical_json(payload).encode("utf-8")).hexdigest()


class AuditService:
    """Hash-chained audit log.

    In a single process the RLock serialises writers. On Postgres a
    transaction-scoped advisory lock also protects the chain across workers.
    A production deployment with multiple writers must use that advisory lock
    (or a single audit writer); this is documented in docs/SECURITY.md.
    """

    def __init__(self, session_factory: sessionmaker, dialect: str = "sqlite"):
        self.session_factory = session_factory
        self.dialect = dialect
        self._lock = threading.RLock()

    @contextmanager
    def unit_of_work(self) -> Iterator[Session]:
        with self._lock:
            db = self.session_factory()
            try:
                if self.dialect == "postgresql":
                    db.execute(text("SELECT pg_advisory_xact_lock(928374651)"))
                yield db
                db.commit()
            except Exception:
                db.rollback()
                raise
            finally:
                db.close()

    def record(
        self,
        db: Session,
        event: str,
        *,
        transfer_id: str | None = None,
        session_id: str | None = None,
        details: dict | None = None,
    ) -> AuditLog:
        with self._lock:
            last = db.scalar(select(AuditLog).order_by(AuditLog.seq.desc()).limit(1))
            previous_hash = last.row_hash if last else GENESIS_HASH
            entry_id = uuid.uuid4().hex
            created_at = utcnow()
            details = details or {}
            row_hash = compute_row_hash(
                entry_id=entry_id,
                event=event,
                transfer_id=transfer_id,
                session_id=session_id,
                details=details,
                previous_hash=previous_hash,
                created_at=created_at,
            )
            row = AuditLog(
                id=entry_id,
                transfer_id=transfer_id,
                session_id=session_id,
                event=event,
                details=details,
                previous_hash=previous_hash,
                row_hash=row_hash,
                created_at=created_at,
            )
            db.add(row)
            db.flush()
            return row

    def record_standalone(
        self,
        event: str,
        *,
        transfer_id: str | None = None,
        session_id: str | None = None,
        details: dict | None = None,
    ) -> None:
        with self.unit_of_work() as db:
            self.record(db, event, transfer_id=transfer_id, session_id=session_id, details=details)

    def verify(self, db: Session) -> dict:
        rows = db.scalars(select(AuditLog).order_by(AuditLog.seq.asc())).all()
        previous = GENESIS_HASH
        for row in rows:
            expected = compute_row_hash(
                entry_id=row.id,
                event=row.event,
                transfer_id=row.transfer_id,
                session_id=row.session_id,
                details=row.details or {},
                previous_hash=previous,
                created_at=row.created_at,
            )
            if row.previous_hash != previous:
                return {
                    "ok": False,
                    "entries": len(rows),
                    "first_bad_seq": row.seq,
                    "reason": "previous_hash_mismatch",
                }
            if row.row_hash != expected:
                return {
                    "ok": False,
                    "entries": len(rows),
                    "first_bad_seq": row.seq,
                    "reason": "row_hash_mismatch",
                }
            previous = row.row_hash
        return {
            "ok": True,
            "entries": len(rows),
            "head_hash": previous,
            "first_bad_seq": None,
            "reason": None,
        }
