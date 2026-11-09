# Avenza protocol

This document describes the WebSocket session protocol, the REST surface, the
transfer state machine, and the idempotency rules implemented in this build.
Everything here is enforced by `avenza/ws.py`, `avenza/orchestrator.py`,
`avenza/services/transfers.py`, `avenza/security.py` and
`avenza/state_machine.py`.

---

## 1. Transport

- WebSocket: `GET /ws` (same origin as the PWA).
- JSON control messages are text frames.
- Audio is **binary frames** of raw little-endian signed 16-bit mono PCM.
- The server accepts `8000`–`48000` Hz. The reference client downsamples to
  **16000 Hz** before sending.
- One utterance is capped at **15 seconds** (`sample_rate × 2 × 15` bytes). A
  longer utterance is discarded with `utterance_too_long`.
- The client is expected to do voice-activity detection and send 500 ms of
  pre-roll before speech onset. The server does not trim silence.

In production the server rejects a WebSocket whose `Origin` header is not in
`WEBAUTHN_ORIGIN` (comma-separated list allowed).

---

## 2. Session lifecycle

```
client                                  server
  │  session.start (device_token, consent_id, language)
  │ ───────────────────────────────────────────────────▶
  │  session.ready + prompt.play + listening
  │ ◀───────────────────────────────────────────────────
  │  utterance.start / binary frames / utterance.end
  │ ───────────────────────────────────────────────────▶
  │  transcript.final + state.changed + prompt.play + listening
  │ ◀───────────────────────────────────────────────────
  │  utterance.text "yes"
  │ ───────────────────────────────────────────────────▶
  │  transcript.final + state.changed + prompt.play + approval.request
  │ ◀───────────────────────────────────────────────────
  │  approval.response (WebAuthn credential)
  │ ───────────────────────────────────────────────────▶
  │  state.changed (APPROVED…) + state.changed (SETTLED) + prompt.play + listening
  │ ◀───────────────────────────────────────────────────
```

A session requires:

1. A valid device token, returned once by enrolment.
2. A consent record belonging to the same user.

There is exactly one session per WebSocket connection. The server closes the
session on disconnect, on `session.end`, or on error.

---

## 3. Client → server messages

### `session.start`

```json
{
  "type": "session.start",
  "device_token": "…",
  "consent_id": "cns_…",
  "language": "en"
}
```

- `device_token` is the bearer token issued by enrolment. It is not a payment
  credential.
- `consent_id` must exist and belong to the token's user.
- Unknown language codes fall back to `en` and the server sends
  `unsupported_language` before the normal start events.

### `utterance.start`

```json
{ "type": "utterance.start", "sample_rate": 16000 }
```

Starts a PCM buffer. `sample_rate` must be between 8000 and 48000.

### Binary audio frames

Send raw PCM after `utterance.start`. Each frame is appended to the buffer.
Frames sent without `utterance.start` produce `no_utterance`.

### `utterance.end`

Processes the buffered audio. With the default mock STT and no browser
transcript, the honest result is `no_speech`: the server never invents words.

```json
{ "type": "utterance.end" }
```

### `utterance.text`

Used by browser speech recognition or the typed demo fallback.

```json
{ "type": "utterance.text", "text": "send five thousand naira to GTBank account 0123456789", "final": true }
```

- `final: false` returns `transcript.partial` and does not process a turn.
- `final: true` runs a full turn.
- If `ALLOW_TEXT_UTTERANCE=false`, the server returns `text_not_allowed`.

### `approval.response`

```json
{
  "type": "approval.response",
  "credential": {
    "id": "…",
    "rawId": "…",
    "type": "public-key",
    "response": {
      "clientDataJSON": "…",
      "authenticatorData": "…",
      "signature": "…",
      "userHandle": null
    },
    "clientExtensionResults": {}
  }
}
```

- `credential` is a WebAuthn assertion produced by `navigator.credentials.get`.
- `dev: true` is only accepted when `ALLOW_DEV_APPROVAL=true` **and**
  `APP_ENV` is not production. It is for tests and the demo only.
- A missing credential is rejected and the client is asked to try again.

### `ping` / `session.end`

- `{"type":"ping"}` → `{"type":"pong"}`.
- `{"type":"session.end"}` closes the session cleanly.
- Unknown message types are ignored by contract.

---

## 4. Server → client events

| Event | When | Key fields |
| --- | --- | --- |
| `session.ready` | After a valid `session.start` | `session_id`, `user`, `language`, `language_verified`, `bank`, `stt`, `tts`, `webauthn`, `dev_approval_allowed`, `approval_ttl_s`, `prompts` |
| `prompt.play` | Any spoken prompt | `id`, `text`, `language`, `audio_url` |
| `listening` | The server is ready for the next utterance | `tone` |
| `transcript.final` | A final transcript exists | `text`, `provider`, `confidence`, `timings` |
| `transcript.partial` | Non-final text | `text` |
| `state.changed` | A transfer transitioned | `transfer_id`, `state`, `display`, `amount_display`, `recipient`, `reference` |
| `approval.request` | Fingerprint is required | `transfer_id`, `options`, `transfer`, `expires_in_s` |
| `history.data` | A history query ran | `period`, `transactions` |
| `lock.changed` | Emergency lock changed | `locked`, `reason` |
| `error` | A turn was refused | `code`, `prompt_id`, `message` |
| `pong` | Reply to `ping` | — |
| `audio.stream` | **Reserved.** Accepted by the client, not emitted by this build. | — |

`prompt.play.audio_url` is set when a matching WAV exists under `web/prompts/`.
Otherwise the client speaks `text` with the browser's speech synthesis.

### `approval.request` options

`options` is a standard WebAuthn `PublicKeyCredentialRequestOptions` JSON object:
`challenge`, `timeout`, `rpId`, `allowCredentials`, `userVerification`.

The client decodes the base64url fields, calls `navigator.credentials.get`,
serialises the assertion, and sends it back in `approval.response`.

The transfer summary inside `approval.request` is informational. The server
verifies the stored transfer, not the summary sent to the client.

---

## 5. Event ordering

A normal transfer request produces, in order:

```
transcript.final
state.changed            (READBACK_PLAYED)
prompt.play              (readback)
listening
```

Voice confirmation produces:

```
transcript.final
state.changed            (AWAITING_BIOMETRIC)
prompt.play              (confirm_voice)
approval.request
```

A successful approval produces:

```
state.changed            (APPROVED)
state.changed            (SUBMITTED)
state.changed            (SETTLED)
prompt.play              (settled)
listening
```

Intermediate states are emitted as they happen. The final `state.changed` is
authoritative. A `FAILED` or `PENDING_UNKNOWN` result also emits a matching
prompt and returns to listening. The periodic tick re-checks submitted or
pending transfers and emits a new `state.changed` only when the state changes.

Errors are emitted before the prompt and `listening`, for example:

```
transcript.final
error                    (approval_required)
prompt.play              (approval_required)
listening
```

---

## 6. Transfer states

| State | Meaning |
| --- | --- |
| `DRAFTED` | Fields parsed, draft created. |
| `READBACK_PLAYED` | The exact transfer was read back. |
| `CONFIRMED_BY_VOICE` | Customer said yes. Still no money movement. |
| `AWAITING_BIOMETRIC` | WebAuthn challenge issued. |
| `APPROVED` | Backend verified the fingerprint. |
| `SUBMITTED` | Sent to the bank adapter. |
| `SETTLED` | Bank confirmed settlement. |
| `FAILED` | Bank reported failure. No retry. |
| `PENDING_UNKNOWN` | Outcome unknown; status must be checked. |
| `CANCELLED` | Cancelled before submission. |
| `EXPIRED` | Draft or approval expired before submission. |

Allowed transitions:

```
DRAFTED             → READBACK_PLAYED | CANCELLED | EXPIRED
READBACK_PLAYED     → CONFIRMED_BY_VOICE | READBACK_PLAYED | CANCELLED | EXPIRED
CONFIRMED_BY_VOICE  → AWAITING_BIOMETRIC | CANCELLED | EXPIRED
AWAITING_BIOMETRIC  → APPROVED | CANCELLED | EXPIRED
APPROVED            → SUBMITTED
SUBMITTED           → SETTLED | FAILED | PENDING_UNKNOWN
PENDING_UNKNOWN     → SETTLED | FAILED | PENDING_UNKNOWN
SETTLED / FAILED / CANCELLED / EXPIRED → terminal
```

Silence never advances a state. Only code transitions states.

---

## 7. Error codes

| Code | Meaning |
| --- | --- |
| `session_required` | No session started. |
| `session_exists` | A session already started on this socket. |
| `device_not_enrolled` | Device token missing or invalid. |
| `consent_required` | No matching consent record. |
| `text_not_allowed` | Typed text disabled by config. |
| `bad_json` | Frame was not valid JSON. |
| `bad_sample_rate` | Sample rate outside 8000–48000. |
| `no_utterance` | Binary audio without `utterance.start`. |
| `utterance_too_long` | Audio exceeded 15 seconds. |
| `no_speech` | STT heard nothing. No transfer is created. |
| `low_confidence` | STT confidence too low. No transfer is created. |
| `unclear` | Intent not understood. |
| `ambiguous_amount` | Zero, multiple, or unsupported amounts. |
| `ambiguous_account` | Not exactly one 10-digit account. |
| `amount_too_large` | Above `MAX_AMOUNT_KOBO`. |
| `account_not_found` | Name enquiry failed. |
| `bank_unavailable` / `bank_error` | Name enquiry could not run. |
| `draft_active` | A draft already exists; it is not replaced. |
| `transfers_locked` | Emergency lock is on. |
| `approval_required` | Voice tried to approve at the biometric step. |
| `approval_error` | Challenge could not be issued. |
| `internal_error` | Unexpected server error. |

`error` events always include a `prompt_id` the client can speak.

---

## 8. REST endpoints

All protected endpoints accept either:

```
Authorization: Bearer <device_token>
X-Device-Token: <device_token>
```

| Method | Path | Auth | Notes |
| --- | --- | --- | --- |
| `GET` | `/health` | none | Status, modes, limits, WebAuthn config. |
| `GET` | `/config` | none | Public config, demo accounts in mock/dev mode. |
| `GET` | `/ai/status` | none | AI switches, languages, amount/approval authority. |
| `GET` | `/prompts` | none | Prompt text for every language. |
| `POST` | `/devices/register/options` | none | Body: `display_name`, `language`, optional `user_id`/`device_token`. |
| `POST` | `/devices/register/verify` | none | Body: `challenge_id`, `credential`. Returns device token **once**. |
| `POST` | `/dev/devices/register` | dev only | No WebAuthn key; cannot approve unless dev approval is on. |
| `POST` | `/consents` | device | Body: `wording_version`, `language`. |
| `GET` | `/me` | device | Current user and device. |
| `GET` | `/transfers?start=&end=&limit=` | device | History, ISO-8601 times. |
| `GET` | `/transfers/{id}` | device | Owner only; account masked. |
| `GET` | `/transfers/{id}/receipt` | device | 409 until submitted. |
| `POST` | `/locks` | device | Engage emergency lock. |
| `GET` | `/locks/status` | device | Lock state. |
| `POST` | `/locks/options` | device | WebAuthn unlock challenge. |
| `POST` | `/locks/unlock` | device | Body: `challenge_id`, `credential`. |
| `GET` | `/audit/verify` | admin/dev | Verifies the hash chain. |
| `GET` | `/audit/recent?limit=` | admin/dev | Recent audit rows. |
| `POST` | `/dev/bank/mode` | admin/dev | Mock failure mode: `mode`, `delay_ms`. |
| `GET` | `/dev/bank/accounts` | admin/dev | Demo accounts. |

Admin routes use `X-Admin-Token` when `ADMIN_TOKEN` is set. Otherwise they are
open only when `DEV_TOOLS=true` and `APP_ENV` is not production.

### Transfer summary shape

```json
{
  "id": "tr_…",
  "state": "SETTLED",
  "display": "Settled",
  "amount_kobo": 500000,
  "amount_display": "₦5,000.00",
  "bank_code": "058",
  "bank_name": "GTBank",
  "account_last4": "6789",
  "recipient": "Aisha Bello",
  "reference": "AVZ-…",
  "risk_flags": [],
  "created_at": "2026-11-08T16:00:00+00:00",
  "settled_at": "2026-11-08T16:00:03+00:00",
  "failure_reason": null
}
```

---

## 9. Idempotency and timeouts

Every draft gets a unique `idempotency_key`. The bank adapter is called with
that key.

- Repeating a create with the same key returns the first bank result. The mock
  bank enforces this with a database uniqueness constraint.
- A repeated client approval after settlement is rejected because the transfer
  is no longer in `AWAITING_BIOMETRIC`.
- A repeated `submit` on a settled transfer returns the settled transfer and
  does not call the bank again.
- On `BankTimeout`, the service calls `get_transfer_status` before any retry.
  If the bank already committed, the transfer is reconciled to `SETTLED`. If
  the bank has no record, the service may retry once with the same key.
- A bank `FAILED` result is terminal. It is not retried automatically.

The invariant is: **at most one bank transfer per idempotency key**, and a
retry must never create a second payment.

---

## 10. WebAuthn approval challenge

The approval challenge is not random per request. It is a SHA-256 hash of a
canonical JSON payload built from the **stored transfer**:

```json
{
  "purpose": "avenza.transfer.approval.v1",
  "transfer_id": "tr_…",
  "user_id": "…",
  "amount_kobo": 500000,
  "bank_code": "058",
  "account_number": "0123456789",
  "verified_recipient_name": "Aisha Bello",
  "nonce": "…",
  "idempotency_key": "…",
  "expires_at": "2026-11-08T16:01:00Z"
}
```

On verification the server rebuilds the payload from the current database row
and compares it with the payload stored when the challenge was issued. If any
field changed — amount, account, bank, recipient, nonce, key, expiry — the
comparison fails before the signature is checked.

Additional checks:

- Challenge must exist and not be used (`approval_used_at`).
- Challenge must not be expired.
- Assertion must verify against the registered credential public key.
- `user_verification` is required.
- Origin and RP ID must match configuration.
- Signature counter must increase when the authenticator uses one.

A successful approval is stored (`signed_approval`), marked used, and only then
does the transfer move to `APPROVED` and submission begin.

---

## 11. Audio format summary

| Property | Value |
| --- | --- |
| Encoding | signed 16-bit little-endian |
| Channels | 1 (mono) |
| Sample rate | 8000–48000 Hz; client uses 16000 |
| Max utterance | 15 seconds |
| Pre-roll | client-side, recommended 500 ms |
| Transport | WebSocket binary frames |
| Text fallback | `utterance.text` when enabled |
