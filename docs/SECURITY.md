# Avenza security notes

This document states what the prototype guarantees, where those guarantees are
enforced, and what it explicitly does **not** protect against. Read it before
using Avenza for anything beyond a demo.

Avenza is not a bank, does not hold funds, does not run KYC, and does not
settle payments. It is a voice front end plus a strict approval gate in front
of a bank adapter.

---

## 1. Non-negotiable invariants

These are the rules that must never be weakened. Each has a test.

1. **Voice alone cannot move money.**
   `AWAITING_BIOMETRIC → APPROVED` requires a verified WebAuthn assertion.
   Enforced in `services/transfers.py` and `security.py`.
2. **A device token is not a payment credential.**
   The token opens a WebSocket session and calls non-money endpoints. It cannot
   approve a transfer.
3. **The approval is bound to the exact stored transfer.**
   Amount, bank, account number, verified recipient name, nonce, idempotency
   key and expiry are hashed into the challenge. Changing any stored field
   after the challenge is issued invalidates it.
4. **An approval is single-use.**
   `approval_used_at` is set on success and replayed assertions are rejected.
5. **An expired approval cannot be used.**
   `APPROVAL_TTL_S` is checked before signature verification.
6. **At most one payment per idempotency key.**
   The bank ledger has a uniqueness constraint, and the service checks status
   before retrying a timeout.
7. **Failure never retries silently.**
   `FAILED` is terminal. `PENDING_UNKNOWN` requires an explicit status check.
8. **No silence-driven transitions.**
   Timers only expire drafts and approvals. They never approve or submit.
9. **The dev bypass cannot run in production.**
   `ALLOW_DEV_APPROVAL` is ignored when `APP_ENV=production`; dev enrolment is
   refused in production too.
10. **No PINs, card numbers, or funds are stored.**
    Only the minimum transfer metadata, a credential public key, and audit
    rows. Money is an integer number of kobo.

Never merge a change that lets a payment complete without a verified approval.

---

## 2. Threat model

### In scope

| Threat | Defence |
| --- | --- |
| A spoken "yes" is treated as payment approval | Voice only reaches `CONFIRMED_BY_VOICE`; `APPROVED` requires WebAuthn. |
| A malicious client sends `approval.response` without a fingerprint | The server verifies a signature against the stored credential public key. |
| A client changes the amount or recipient after read-back | Challenge is rebuilt from the stored row; mismatch fails before signature verification. |
| Replay of a captured assertion | Single-use challenge, expiry, and signature counter check. |
| Replay of a create-transfer call | Idempotency key uniqueness at the bank; service returns the first result. |
| Lost bank reply causes a double payment | Status check before retry; one key per transfer. |
| Someone edits or deletes audit rows | SHA-256 chain verification reports the first bad sequence. |
| A stolen device token is used to approve | The token cannot approve; a WebAuthn assertion is still required. |
| Brute-force fingerprint retries | Three failed reads cancel the draft. |
| Cross-origin WebSocket abuse in production | Origin allow-list from `WEBAUTHN_ORIGIN`. |
| Cross-user transfer access | `/transfers/{id}` checks ownership; mismatches return 404. |

### Out of scope

- A compromised server or database administrator. The server sees plaintext
  transfer data and can rewrite the database, including the audit chain.
- A compromised browser or operating system. WebAuthn protects the private key,
  but malware on the device can act after approval.
- Phishing that tricks the customer into approving a different payment on a
  look-alike site. RP ID and origin binding reduce this, but user education is
  still required.
- Real bank fraud, settlement disputes, chargebacks, or regulatory compliance.
- SIM swap, account takeover at the bank, or KYC impersonation.
- Denial of service, traffic analysis, or side channels.

---

## 3. WebAuthn approval

### Registration

1. `POST /devices/register/options` creates a random 32-byte challenge valid for
   five minutes, with `authenticatorAttachment=platform`,
   `residentKey=preferred`, and `userVerification=required`.
2. The browser calls `navigator.credentials.create`.
3. `POST /devices/register/verify` verifies the attestation with the `webauthn`
   library against the configured RP ID and origin.
4. The device stores the credential public key and returns a device token
   **once**. The token is stored hashed (SHA-256) in the database.

The registration challenge is single-use. Reusing it fails.

### Approval

1. After voice confirmation, the service moves the transfer to
   `AWAITING_BIOMETRIC` and issues a challenge.
2. The challenge is:

   ```
   SHA-256(canonical_json({
     purpose, transfer_id, user_id, amount_kobo, bank_code,
     account_number, verified_recipient_name, nonce,
     idempotency_key, expires_at
   }))
   ```

   It is stored alongside the payload and a payload hash.
3. The browser calls `navigator.credentials.get` with
   `allowCredentials=[the enrolled credential]` and `userVerification=required`.
4. The server rebuilds the payload from the current row and compares it to the
   stored payload **before** checking the signature.
5. The server verifies the assertion with the stored public key, expected
   challenge, RP ID, origin, and `require_user_verification=True`.
6. If the authenticator uses a signature counter, it must increase. Platform
   authenticators that always report zero are accepted; this is a documented
   weaker case.
7. On success, `approval_used_at` is set, the assertion is stored, and only
   then does the transfer move to `APPROVED` and submission begin.

### Why the challenge is not random per approval

A random challenge would prove "the user touched the sensor", not "the user
approved this exact transfer". Deriving it from the stored transfer binds the
signature to the amount, recipient and account. The nonce and expiry prevent a
precomputed challenge from being reused later.

### RP ID and origin

`WEBAUTHN_RP_ID` must be the registrable domain (`localhost` for local dev).
`WEBAUTHN_ORIGIN` must exactly match the browser address bar, including scheme
and port. A mismatch makes registration or approval fail. This is intentional.

---

## 4. Device token and session security

- Tokens are 32 random bytes, URL-safe, returned once at enrolment.
- Only a SHA-256 hash is stored.
- Tokens are sent as `Authorization: Bearer` or `X-Device-Token`.
- A token identifies a device and user for non-money operations.
- A WebSocket session additionally requires a consent record owned by the same
  user.
- The session is closed on disconnect, `session.end`, or error. The session
  row is closed server-side.

There is no token refresh, revocation endpoint, or multi-device management in
this prototype. Deleting the device row is the revocation mechanism.

---

## 5. The developer bypass

`ALLOW_DEV_APPROVAL=true` lets `approval.response` include `dev: true` and skip
WebAuthn. It exists only for automated tests and a sensor-less demo.

Gates:

- `APP_ENV` must not be `production` or `prod`.
- The PWA shows the dev controls only when `/config` reports
  `dev_approval_allowed`.
- Dev enrolment creates a device with an empty public key and `aaguid="dev"`;
  it can never pass real WebAuthn verification.
- Docker compose sets `ALLOW_DEV_APPROVAL=false`.
- `tests/test_security.py` asserts the bypass is off in production.

Treat turning this on as equivalent to disabling the central safety property.
Never enable it on a shared or internet-facing deployment.

---

## 6. Audit log

The audit log is a SHA-256 hash chain:

```
row_hash = SHA-256(canonical_json({
  id, event, transfer_id, session_id, details, previous_hash, created_at
}))
```

- The first row links to `"0" * 64`.
- Each later row links to the previous `row_hash`.
- `GET /audit/verify` recomputes every row and reports the first bad sequence
  and whether the failure was a row hash or a previous-hash link.

Writers are serialised with an in-process `threading.RLock`. On PostgreSQL a
transaction-scoped advisory lock (`pg_advisory_xact_lock(928374651)`) also
serialises writers across workers.

**Limits.** The chain is tamper-evident, not tamper-proof. Someone with write
access to the database can rewrite every row and recompute the chain. To make
tampering harder, periodically export the head hash to an append-only store or
a third party. Deleting only the tail of the chain cannot be detected by
`/audit/verify` alone; compare the current head with the exported head.

Audit rows contain transfer ids, session ids and event details. They do not
contain full account numbers in the details added by the service, but treat the
database as sensitive.

---

## 7. Money and data handling

- Amounts are integer kobo everywhere (`amount_kobo`). No floats in storage.
- `MAX_AMOUNT_KOBO` defaults to ₦1,000,000.
- The parser rejects zero, multiple amounts, unsupported currencies, and bare
  number words without a currency or scale.
- The LLM, if enabled, only classifies intent. It never supplies an amount.
- `/transfers/{id}` returns a masked account (`******6789`); the full number is
  used only for the bank call.
- The signed WebAuthn assertion is stored on the transfer for audit. It contains
  no biometric data, only a signature.
- No PINs, card numbers, CVVs, passwords, or funds are stored.

---

## 8. Risk controls

| Control | Default |
| --- | --- |
| Max amount | ₦1,000,000 |
| Large amount threshold | ₦50,000 |
| New-recipient safety hold | 10 s before fingerprint |
| Duplicate detection window | 24 h, same account and amount |
| Duplicate confirmation | Two voice confirmations |
| Draft TTL | 120 s |
| Read-back TTL | 90 s |
| Approval TTL | 60 s |
| Failed fingerprint reads before cancel | 3 |
| Session TTL | 1800 s |

The safety hold is a pause, not an approval. It cannot move money. Duplicate
detection is a heuristic and can produce false positives; the extra voice
confirmation is deliberate.

---

## 9. Deployment checklist

Before exposing an instance beyond your machine:

- [ ] `APP_ENV=production`.
- [ ] `ALLOW_DEV_APPROVAL=false` (and verify `/health` reports `false`).
- [ ] `DEV_TOOLS=false` unless you intentionally want dev endpoints.
- [ ] `ADMIN_TOKEN` set to a strong random value; `/audit/*` and `/dev/*`
      protected.
- [ ] `WEBAUTHN_RP_ID` and `WEBAUTHN_ORIGIN` match the real HTTPS URL exactly.
- [ ] TLS terminated by a trusted certificate, not the development cert.
- [ ] `DATABASE_URL` points at PostgreSQL with backups and restricted access.
- [ ] A real bank adapter with signed contracts, not `BANK_MODE=mock` or the
      placeholder sandbox.
- [ ] A real STT/TTS provider, with consent wording that names it.
- [ ] Audit head hash exported to an append-only location.
- [ ] Rate limiting and WAF in front of `/ws` and enrolment.
- [ ] A privacy notice and lawful basis for voice processing.
- [ ] A native-speaker language test before claiming any non-English language.

---

## 10. Known gaps

- No KYC, AML, sanctions screening, or transaction monitoring.
- No real settlement, reconciliation, or dispute handling.
- No PIN, password, or knowledge-factor fallback.
- No device revocation UI, account recovery, or multi-device management.
- No hardware attestation policy; any platform authenticator is accepted.
- Signature counters are optional for platform authenticators.
- No rate limiting in the app itself.
- No key rotation or encryption at rest beyond the database's own controls.
- The sandbox bank adapter's endpoints are placeholders marked `CONFIRM`.
- Live STT/TTS adapters are not verified against provider accounts.
- Non-English prompt translations are unverified.
- SQLite is not suitable for multi-writer production use.

If any of these gaps matter for your use case, the prototype is not ready for
that use case.
