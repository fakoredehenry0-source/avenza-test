"""A software WebAuthn authenticator used only by the test suite.

It produces real attestation and assertion objects so the backend's actual
`webauthn` verification runs. This is not shipped with the app.
"""

from __future__ import annotations

import base64
import hashlib
import json
import secrets

import cbor2
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.asymmetric import ec


def b64url(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode("ascii")


def unb64url(value: str) -> bytes:
    return base64.urlsafe_b64decode(value + "=" * (-len(value) % 4))


class SoftAuthenticator:
    def __init__(self, rp_id: str, origin: str):
        self.rp_id = rp_id
        self.origin = origin
        self.private_key = ec.generate_private_key(ec.SECP256R1())
        self.credential_id = secrets.token_bytes(32)
        self.sign_count = 0
        self.aaguid = b"\x00" * 16

    # ------------------------------------------------------------------
    # Encoding helpers
    # ------------------------------------------------------------------
    def _cose_public_key(self) -> bytes:
        numbers = self.private_key.public_key().public_numbers()
        return cbor2.dumps(
            {
                1: 2,  # kty: EC2
                3: -7,  # alg: ES256
                -1: 1,  # crv: P-256
                -2: numbers.x.to_bytes(32, "big"),
                -3: numbers.y.to_bytes(32, "big"),
            }
        )

    def _rp_id_hash(self) -> bytes:
        return hashlib.sha256(self.rp_id.encode("utf-8")).digest()

    def _client_data(self, kind: str, challenge: bytes) -> bytes:
        return json.dumps(
            {
                "type": kind,
                "challenge": b64url(challenge),
                "origin": self.origin,
                "crossOrigin": False,
            },
            separators=(",", ":"),
        ).encode("utf-8")

    # ------------------------------------------------------------------
    # Registration
    # ------------------------------------------------------------------
    def create(self, public_key: dict) -> dict:
        challenge = unb64url(public_key["challenge"])
        client_data = self._client_data("webauthn.create", challenge)

        flags = 0x01 | 0x04 | 0x40  # UP | UV | AT
        auth_data = (
            self._rp_id_hash()
            + bytes([flags])
            + self.sign_count.to_bytes(4, "big")
            + self.aaguid
            + len(self.credential_id).to_bytes(2, "big")
            + self.credential_id
            + self._cose_public_key()
        )
        attestation_object = cbor2.dumps({"fmt": "none", "attStmt": {}, "authData": auth_data})

        return {
            "id": b64url(self.credential_id),
            "rawId": b64url(self.credential_id),
            "type": "public-key",
            "response": {
                "clientDataJSON": b64url(client_data),
                "attestationObject": b64url(attestation_object),
                "transports": ["internal"],
            },
            "clientExtensionResults": {},
        }

    # ------------------------------------------------------------------
    # Authentication
    # ------------------------------------------------------------------
    def get(self, public_key: dict) -> dict:
        challenge = unb64url(public_key["challenge"])
        client_data = self._client_data("webauthn.get", challenge)

        self.sign_count += 1
        flags = 0x01 | 0x04  # UP | UV
        auth_data = (
            self._rp_id_hash()
            + bytes([flags])
            + self.sign_count.to_bytes(4, "big")
        )
        signed = auth_data + hashlib.sha256(client_data).digest()
        signature = self.private_key.sign(signed, ec.ECDSA(hashes.SHA256()))

        return {
            "id": b64url(self.credential_id),
            "rawId": b64url(self.credential_id),
            "type": "public-key",
            "response": {
                "clientDataJSON": b64url(client_data),
                "authenticatorData": b64url(auth_data),
                "signature": b64url(signature),
                "userHandle": None,
            },
            "clientExtensionResults": {},
        }
