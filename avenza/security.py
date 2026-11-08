from __future__ import annotations

import base64
import hashlib
import json
import secrets
import uuid
from datetime import timedelta

from sqlalchemy import select
from sqlalchemy.orm import Session

from .config import Settings
from .models import Device, Transfer, User, WebAuthnChallenge
from .timeutil import iso, utcnow

from webauthn import (
    generate_authentication_options,
    generate_registration_options,
    verify_authentication_response,
    verify_registration_response,
)
from webauthn.helpers import options_to_json
from webauthn.helpers.structs import (
    AttestationConveyancePreference,
    AuthenticatorAttachment,
    AuthenticatorSelectionCriteria,
    PublicKeyCredentialDescriptor,
    ResidentKeyRequirement,
    UserVerificationRequirement,
)


class ApprovalError(RuntimeError):
    code = "approval_rejected"


def b64url_encode(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode("ascii")


def b64url_decode(value: str) -> bytes:
    return base64.urlsafe_b64decode(value + "=" * (-len(value) % 4))


def _canonical(value: dict) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


class WebAuthnService:
    def __init__(self, settings: Settings):
        self.settings = settings
        self.origins = [o.strip() for o in settings.webauthn_origin.split(",") if o.strip()]
        self.origin = self.origins[0] if len(self.origins) == 1 else self.origins

    # ------------------------------------------------------------------
    # Registration (passkey enrolment)
    # ------------------------------------------------------------------
    def registration_options(self, db: Session, user: User) -> dict:
        challenge = secrets.token_bytes(32)
        row = WebAuthnChallenge(
            id=f"wac_{uuid.uuid4().hex}",
            user_id=user.id,
            kind="registration",
            challenge=b64url_encode(challenge),
            expires_at=utcnow() + timedelta(minutes=5),
        )
        db.add(row)

        existing = db.scalars(select(Device).where(Device.user_id == user.id)).all()
        exclude = [PublicKeyCredentialDescriptor(id=b64url_decode(d.credential_id)) for d in existing]
        options = generate_registration_options(
            rp_id=self.settings.webauthn_rp_id,
            rp_name=self.settings.webauthn_rp_name,
            user_id=uuid.UUID(hex=user.id).bytes,
            user_name=user.id,
            user_display_name=user.display_name,
            challenge=challenge,
            timeout=120_000,
            attestation=AttestationConveyancePreference.NONE,
            authenticator_selection=AuthenticatorSelectionCriteria(
                authenticator_attachment=AuthenticatorAttachment.PLATFORM,
                resident_key=ResidentKeyRequirement.PREFERRED,
                user_verification=UserVerificationRequirement.REQUIRED,
            ),
            exclude_credentials=exclude or None,
        )
        return {"challenge_id": row.id, "publicKey": json.loads(options_to_json(options))}

    def verify_registration(self, db: Session, challenge_id: str, credential: dict) -> tuple[WebAuthnChallenge, object]:
        row = db.get(WebAuthnChallenge, challenge_id)
        if row is None or row.kind != "registration":
            raise ApprovalError("registration challenge not found")
        if row.used_at is not None:
            raise ApprovalError("registration challenge already used")
        if row.expires_at < utcnow():
            raise ApprovalError("registration challenge expired")
        try:
            verified = verify_registration_response(
                credential=credential,
                expected_challenge=b64url_decode(row.challenge),
                expected_rp_id=self.settings.webauthn_rp_id,
                expected_origin=self.origin,
                require_user_presence=True,
                require_user_verification=True,
            )
        except Exception as exc:
            raise ApprovalError(f"registration verification failed: {exc}") from exc
        row.used_at = utcnow()
        return row, verified

    # ------------------------------------------------------------------
    # Transfer approval
    # ------------------------------------------------------------------
    def _approval_payload(self, transfer: Transfer, expires_at) -> dict:
        return {
            "purpose": "avenza.transfer.approval.v1",
            "transfer_id": transfer.id,
            "user_id": transfer.user_id,
            "amount_kobo": transfer.amount_kobo,
            "bank_code": transfer.bank_code,
            "account_number": transfer.account_number,
            "verified_recipient_name": transfer.verified_recipient_name,
            "nonce": transfer.nonce,
            "idempotency_key": transfer.idempotency_key,
            "expires_at": iso(expires_at),
        }

    def approval_options(self, db: Session, transfer: Transfer, device: Device) -> dict:
        expires_at = utcnow() + timedelta(seconds=self.settings.approval_ttl_s)
        payload = self._approval_payload(transfer, expires_at)
        canonical = _canonical(payload)
        challenge = hashlib.sha256(canonical.encode("utf-8")).digest()

        transfer.challenge = b64url_encode(challenge)
        transfer.challenge_payload = payload
        transfer.challenge_hash = hashlib.sha256(canonical.encode("utf-8")).hexdigest()
        transfer.approval_expires_at = expires_at
        transfer.approval_used_at = None

        options = generate_authentication_options(
            rp_id=self.settings.webauthn_rp_id,
            challenge=challenge,
            timeout=self.settings.approval_ttl_s * 1000,
            allow_credentials=[PublicKeyCredentialDescriptor(id=b64url_decode(device.credential_id))],
            user_verification=UserVerificationRequirement.REQUIRED,
        )
        return json.loads(options_to_json(options))

    def verify_approval(
        self,
        db: Session,
        transfer: Transfer,
        device: Device,
        credential: dict,
    ) -> object:
        if transfer.approval_used_at is not None:
            raise ApprovalError("approval already used (replay rejected)")
        if not transfer.approval_expires_at or transfer.approval_expires_at < utcnow():
            raise ApprovalError("approval expired")
        if not transfer.challenge or not transfer.challenge_payload:
            raise ApprovalError("no approval challenge on this transfer")

        # Rebuild the challenge from the stored transfer. A client cannot choose
        # or change the amount, recipient, bank or idempotency key.
        expires_at = transfer.approval_expires_at
        rebuilt_payload = self._approval_payload(transfer, expires_at)
        if _canonical(rebuilt_payload) != _canonical(transfer.challenge_payload):
            raise ApprovalError("stored approval payload no longer matches the transfer")
        expected_challenge = hashlib.sha256(_canonical(rebuilt_payload).encode("utf-8")).digest()
        if b64url_encode(expected_challenge) != transfer.challenge:
            raise ApprovalError("approval challenge hash mismatch")

        try:
            verified = verify_authentication_response(
                credential=credential,
                expected_challenge=expected_challenge,
                expected_rp_id=self.settings.webauthn_rp_id,
                expected_origin=self.origin,
                credential_public_key=device.public_key,
                credential_current_sign_count=device.sign_count,
                require_user_verification=True,
            )
        except Exception as exc:
            raise ApprovalError(f"assertion verification failed: {exc}") from exc

        # Signature counter replay defence. Some platform authenticators always
        # report zero; a zero counter is accepted but documented as weaker.
        if device.sign_count > 0 and verified.new_sign_count > 0 and verified.new_sign_count <= device.sign_count:
            raise ApprovalError("signature counter did not increase (replay rejected)")

        transfer.approval_used_at = utcnow()
        transfer.signed_approval = json.dumps(credential, ensure_ascii=False)
        device.sign_count = max(device.sign_count, int(verified.new_sign_count))
        device.last_used_at = utcnow()
        return verified

    def dev_approval_allowed(self) -> bool:
        return bool(self.settings.allow_dev_approval and not self.settings.is_production)

    # ------------------------------------------------------------------
    # Emergency lock unlock
    # ------------------------------------------------------------------
    def unlock_options(self, db: Session, user: User, device: Device) -> dict:
        challenge = secrets.token_bytes(32)
        row = WebAuthnChallenge(
            id=f"wac_{uuid.uuid4().hex}",
            user_id=user.id,
            device_id=device.id,
            kind="unlock",
            challenge=b64url_encode(challenge),
            expires_at=utcnow() + timedelta(minutes=2),
        )
        db.add(row)
        options = generate_authentication_options(
            rp_id=self.settings.webauthn_rp_id,
            challenge=challenge,
            timeout=120_000,
            allow_credentials=[PublicKeyCredentialDescriptor(id=b64url_decode(device.credential_id))],
            user_verification=UserVerificationRequirement.REQUIRED,
        )
        return {"challenge_id": row.id, "publicKey": json.loads(options_to_json(options))}

    def verify_unlock(self, db: Session, challenge_id: str, device: Device, credential: dict) -> object:
        row = db.get(WebAuthnChallenge, challenge_id)
        if row is None or row.kind != "unlock":
            raise ApprovalError("unlock challenge not found")
        if row.used_at is not None:
            raise ApprovalError("unlock challenge already used")
        if row.expires_at < utcnow():
            raise ApprovalError("unlock challenge expired")
        try:
            verified = verify_authentication_response(
                credential=credential,
                expected_challenge=b64url_decode(row.challenge),
                expected_rp_id=self.settings.webauthn_rp_id,
                expected_origin=self.origin,
                credential_public_key=device.public_key,
                credential_current_sign_count=device.sign_count,
                require_user_verification=True,
            )
        except Exception as exc:
            raise ApprovalError(f"unlock verification failed: {exc}") from exc
        if device.sign_count > 0 and verified.new_sign_count > 0 and verified.new_sign_count <= device.sign_count:
            raise ApprovalError("signature counter did not increase (replay rejected)")
        row.used_at = utcnow()
        device.sign_count = max(device.sign_count, int(verified.new_sign_count))
        device.last_used_at = utcnow()
        return verified
