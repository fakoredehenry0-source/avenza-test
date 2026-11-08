from __future__ import annotations

import hashlib
import json
import secrets
import uuid

from sqlalchemy import select

from ..audit import AuditService
from ..config import Settings
from ..models import Consent, Device, User
from ..security import ApprovalError, WebAuthnService, b64url_encode
from ..timeutil import utcnow


class DeviceService:
    def __init__(
        self,
        session_factory,
        audit: AuditService,
        security: WebAuthnService,
        settings: Settings,
    ):
        self.session_factory = session_factory
        self.audit = audit
        self.security = security
        self.settings = settings

    @staticmethod
    def _hash_token(token: str) -> str:
        return hashlib.sha256(token.encode("utf-8")).hexdigest()

    def begin_registration(
        self,
        *,
        display_name: str,
        language: str = "en",
        user_id: str | None = None,
        device_token: str | None = None,
    ) -> dict:
        with self.audit.unit_of_work() as db:
            if user_id:
                if not device_token:
                    raise ApprovalError("an existing customer needs a device token to add a passkey")
                device = db.scalar(
                    select(Device).where(Device.device_token_hash == self._hash_token(device_token))
                )
                if device is None or device.user_id != user_id:
                    raise ApprovalError("device token does not match the customer")
                user = db.get(User, user_id)
                if user is None:
                    raise ApprovalError("customer not found")
            else:
                user = User(
                    id=uuid.uuid4().hex,
                    display_name=display_name.strip()[:200] or "Avenza customer",
                    language=language,
                )
                db.add(user)
                db.flush()

            options = self.security.registration_options(db, user)
            self.audit.record(
                db,
                "device.registration_started",
                details={"user_id": user.id},
            )
            return {
                "user_id": user.id,
                "display_name": user.display_name,
                "language": user.language,
                **options,
            }

    def verify_registration(
        self,
        *,
        challenge_id: str,
        credential: dict,
        ip: str = "",
        user_agent: str = "",
    ) -> dict:
        with self.audit.unit_of_work() as db:
            challenge, verified = self.security.verify_registration(db, challenge_id, credential)
            credential_id = b64url_encode(verified.credential_id)
            existing = db.scalar(select(Device).where(Device.credential_id == credential_id))
            if existing is not None:
                raise ApprovalError("this passkey is already enrolled")

            token = secrets.token_urlsafe(32)
            transports = ""
            try:
                transports = json.dumps(credential.get("response", {}).get("transports", []))
            except Exception:
                transports = "[]"
            device = Device(
                id=f"dev_{uuid.uuid4().hex}",
                user_id=challenge.user_id,
                credential_id=credential_id,
                public_key=verified.credential_public_key,
                sign_count=int(verified.sign_count),
                transports=transports,
                aaguid=str(verified.aaguid),
                device_token_hash=self._hash_token(token),
                created_at=utcnow(),
            )
            db.add(device)
            db.flush()
            self.audit.record(
                db,
                "device.registered",
                details={
                    "user_id": challenge.user_id,
                    "device_id": device.id,
                    "aaguid": device.aaguid,
                    "ip": ip,
                },
            )
            user = db.get(User, challenge.user_id)
            return {
                "user_id": challenge.user_id,
                "device_id": device.id,
                "device_token": token,
                "credential_id": credential_id,
                "display_name": user.display_name if user else "",
                "language": user.language if user else "en",
            }

    # ------------------------------------------------------------------
    # Developer/demo enrolment
    # ------------------------------------------------------------------
    def dev_tools_allowed(self) -> bool:
        """Dev enrolment is only ever available outside production.

        It still creates a real user and a real device row, but the device has
        no WebAuthn public key, so it can never approve a payment except through
        the separately gated ALLOW_DEV_APPROVAL bypass.
        """
        return bool(self.settings.dev_tools and not self.settings.is_production)

    def dev_register(self, *, display_name: str, language: str = "en") -> dict:
        if not self.dev_tools_allowed():
            raise ApprovalError("developer enrolment is disabled")
        with self.audit.unit_of_work() as db:
            user = User(
                id=uuid.uuid4().hex,
                display_name=display_name.strip()[:200] or "Demo customer",
                language=language,
            )
            db.add(user)
            db.flush()

            token = secrets.token_urlsafe(32)
            credential_id = f"devcred_{uuid.uuid4().hex}"
            device = Device(
                id=f"dev_{uuid.uuid4().hex}",
                user_id=user.id,
                credential_id=credential_id,
                public_key=b"",
                sign_count=0,
                transports="[]",
                aaguid="dev",
                device_token_hash=self._hash_token(token),
                created_at=utcnow(),
            )
            db.add(device)
            db.flush()
            self.audit.record(
                db,
                "device.dev_registered",
                details={
                    "user_id": user.id,
                    "device_id": device.id,
                    "dev_tools": True,
                },
            )
            return {
                "user_id": user.id,
                "device_id": device.id,
                "device_token": token,
                "credential_id": credential_id,
                "display_name": user.display_name,
                "language": user.language,
                "dev": True,
            }

    def authenticate(self, token: str | None) -> tuple[Device, User] | None:
        if not token:
            return None
        with self.session_factory() as db:
            device = db.scalar(
                select(Device).where(Device.device_token_hash == self._hash_token(token))
            )
            if device is None:
                return None
            user = db.get(User, device.user_id)
            if user is None:
                return None
            return device, user

    def get_device(self, device_id: str) -> Device | None:
        with self.session_factory() as db:
            return db.get(Device, device_id)

    def device_for_credential(self, user_id: str, credential_id: str) -> Device | None:
        with self.session_factory() as db:
            return db.scalar(
                select(Device).where(
                    Device.user_id == user_id,
                    Device.credential_id == credential_id,
                )
            )

    def record_consent(
        self,
        *,
        user_id: str,
        wording_version: str,
        language: str,
        ip: str = "",
        user_agent: str = "",
    ) -> Consent:
        with self.audit.unit_of_work() as db:
            consent = Consent(
                id=f"con_{uuid.uuid4().hex}",
                user_id=user_id,
                wording_version=wording_version.strip()[:32],
                language=language,
                timestamp=utcnow(),
                ip=ip[:64],
                user_agent=user_agent[:500],
            )
            db.add(consent)
            self.audit.record(
                db,
                "consent.recorded",
                details={
                    "user_id": user_id,
                    "wording_version": consent.wording_version,
                    "language": language,
                },
            )
            return consent

    def get_consent(self, consent_id: str) -> Consent | None:
        with self.session_factory() as db:
            return db.get(Consent, consent_id)
