from __future__ import annotations

import uuid
from datetime import timedelta

from sqlalchemy import select

from ..audit import AuditService
from ..config import Settings
from ..models import Consent, Session as SessionModel
from ..timeutil import utcnow


class SessionError(RuntimeError):
    code = "session_error"


class SessionService:
    def __init__(self, session_factory, audit: AuditService, settings: Settings):
        self.session_factory = session_factory
        self.audit = audit
        self.settings = settings

    def start(self, *, user_id: str, device_id: str, consent_id: str, language: str = "en") -> SessionModel:
        with self.audit.unit_of_work() as db:
            consent = db.get(Consent, consent_id)
            if consent is None or consent.user_id != user_id:
                raise SessionError("a recorded consent for this customer is required")
            session = SessionModel(
                id=f"ses_{uuid.uuid4().hex}",
                user_id=user_id,
                device_id=device_id,
                consent_id=consent_id,
                language=language,
                created_at=utcnow(),
                expires_at=utcnow() + timedelta(seconds=self.settings.session_ttl_s),
                last_seen_at=utcnow(),
            )
            db.add(session)
            self.audit.record(
                db,
                "session.started",
                session_id=session.id,
                details={"user_id": user_id, "device_id": device_id, "language": language},
            )
            return session

    def touch(self, session_id: str) -> None:
        with self.session_factory() as db:
            session = db.get(SessionModel, session_id)
            if session is not None:
                session.last_seen_at = utcnow()
                db.commit()

    def close(self, session_id: str) -> None:
        with self.audit.unit_of_work() as db:
            session = db.get(SessionModel, session_id)
            if session is not None and session.closed_at is None:
                session.closed_at = utcnow()
                self.audit.record(db, "session.ended", session_id=session.id, details={})

    def get(self, session_id: str) -> SessionModel | None:
        with self.session_factory() as db:
            return db.get(SessionModel, session_id)

    def is_active(self, session_id: str) -> bool:
        with self.session_factory() as db:
            session = db.get(SessionModel, session_id)
            return bool(
                session
                and session.closed_at is None
                and session.expires_at > utcnow()
            )
