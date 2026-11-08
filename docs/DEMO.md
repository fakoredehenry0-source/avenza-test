# Avenza demo guide

This walks through the prototype end to end. The default bank is a **local
simulation**; no real money moves. The UI and `/health` both say so.

---

## 1. Start

```powershell
python -m pip install -r requirements.txt
python run.py
```

Open <http://localhost:8000>.

`localhost` is a secure context, so WebAuthn works without HTTPS. If you use a
different host or port, set `WEBAUTHN_ORIGIN` to the exact URL.

Optional: seed demo data first.

```powershell
python scripts/seed.py
```

---

## 2. Enrol a real fingerprint

1. Enter a display name.
2. Choose **English (verified)**.
3. Tick the consent box.
4. Click **Record consent and enrol fingerprint**.
5. Accept the platform passkey prompt:
   - Windows: Windows Hello (PIN, face, or fingerprint)
   - macOS: Touch ID
   - Android: screen lock or fingerprint
6. The session panel appears. Click **Start listening**.

If the browser has no passkey support and `ALLOW_DEV_APPROVAL=true`, the client
offers dev enrolment instead. That is a demo fallback, not a fingerprint.

---

## 3. Send a payment

Say or type:

> Send five thousand naira to GTBank account 0123456789

The server:

1. Parses the amount deterministically (₦5,000.00 = 500,000 kobo).
2. Looks up the account name at the mock bank (Aisha Bello).
3. Reads the exact transfer back.
4. Waits for a voice confirmation.

Say **yes** (or type it). The client now shows **Fingerprint required**.

Click **Approve with fingerprint** and complete the platform prompt. The
backend verifies the assertion, then submits to the mock bank. The status card
ends at **Settled** with a reference.

Try these variants:

| Say | Expected |
| --- | --- |
| `Send fifty thousand naira to Access Bank account 1111222333` | ₦50,000.00 to Chinedu Okafor |
| `Send fifteen thousand naira to Zenith Bank account 5555666777` | ₦15,000.00, not ₦50,000 |
| `Send 1,250 naira and 50 kobo to Kuda account 9999000011` | ₦1,250.50 |
| `Send five thousand dollars…` | Refused: unsupported currency |
| `Send two million naira…` | Refused: above the limit |
| `Show my last three transactions` | History summary |
| `Emergency lock` | Transfers locked |

Voice confirmation words are deterministic. The LLM, if enabled, never decides
yes/no, repeat, cancel, or an amount.

---

## 4. Demo without a sensor

For a quick UI walkthrough:

```powershell
$env:ALLOW_DEV_APPROVAL = "true"
python run.py
```

Then:

1. Enter a display name, then click **Dev enrolment (no fingerprint)**.
2. Send a request, say **yes**, then click **Simulate fingerprint (dev)**.

The bypass is refused when `APP_ENV=production`. Never enable it on a shared
instance. The normal **Approve with fingerprint** button still works when a
real passkey is enrolled.

---

## 5. Failure-mode demos

Open **Show developer tools** at the bottom of the session panel. The mock bank
has these modes:

| Mode | What happens |
| --- | --- |
| `normal` | Settles immediately. |
| `fail` | Bank declines. Transfer ends `FAILED`; no retry. |
| `pending` | Returns pending. The tick loop checks status and settles later. |
| `timeout_before_commit` | First call times out without committing; service checks status, then retries once with the same key. |
| `timeout_after_commit` | First call commits but the reply is lost; status check reconciles to `SETTLED`; no second payment. |
| `slow` | Delays the bank call. |
| `unavailable` | Bank unreachable. No money moves. |
| `name_unavailable` | Name enquiry fails before a draft is created. |

Each mode is applied with **Apply bank mode** and is recorded in the audit log.

After a `timeout_after_commit` run, check the history: there is exactly one
transaction, not two.

---

## 6. Emergency lock

- Say **emergency lock**, or click **Emergency lock**.
- New drafts are refused with `423`.
- Existing drafts cannot be approved.
- Click **Unlock with fingerprint** and complete the platform prompt.

The unlock challenge is single-use and expires after two minutes.

---

## 7. History and receipt

Say **show my last three transactions**. The server returns a `history.data`
event and speaks a summary.

For a settled transfer, the REST receipt is available:

```powershell
$token = "<device token>"
$id = "<transfer id>"
curl -H "Authorization: Bearer $token" http://localhost:8000/transfers/$id
curl -H "Authorization: Bearer $token" http://localhost:8000/transfers/$id/receipt
```

The receipt is mock data and includes `"simulated": true`.

---

## 8. Audit log

```powershell
curl http://localhost:8000/audit/verify
```

Expected:

```json
{ "ok": true, "entries": 12, "head_hash": "…", "first_bad_seq": null, "reason": null }
```

If `ADMIN_TOKEN` is set, add `-H "X-Admin-Token: …"`.

To see tamper detection without editing the database, run the audit tests:

```powershell
python -m pytest tests/test_audit.py -q
```

---

## 9. Phone testing over HTTPS

1. Find the LAN IP (the cert script prints it):

   ```powershell
   python scripts/make_cert.py
   ```

2. Set the exact origin:

   ```powershell
   $env:WEBAUTHN_ORIGIN = "https://192.168.1.20:8000"
   ```

3. Start with HTTPS on all interfaces:

   ```powershell
   python run.py --host 0.0.0.0 --https
   ```

4. On the phone, open `https://192.168.1.20:8000`, accept the development
   certificate warning, then reload.

The origin in the address bar must match `WEBAUTHN_ORIGIN` exactly, including
the port. A mismatch makes passkey enrolment fail with an origin error.

---

## 10. Reset

Stop the server, then:

```powershell
python scripts/seed.py --reset
```

This deletes the SQLite file and recreates the schema and demo accounts. It
refuses to run against PostgreSQL. For Postgres, drop and recreate the database
yourself.

Browser state (device token, consent) is in `localStorage`. To re-enrol, clear
site data for the origin.

---

## 11. Troubleshooting

| Symptom | Likely cause | Fix |
| --- | --- | --- |
| No passkey prompt | Not a secure context, or no platform authenticator | Use `localhost` or HTTPS; check Windows Hello / Touch ID is set up. |
| `NotAllowedError` | Prompt cancelled or timed out | Try again; approve within the timeout. |
| Origin/RP error | `WEBAUTHN_ORIGIN` does not match the URL | Set it exactly, including scheme and port. |
| `device token required or invalid` | Cleared storage or changed database | Re-enrol the device. |
| `no_speech` when speaking | Mock STT cannot transcribe raw audio | Enable browser speech recognition, or type the request. |
| Browser speech recognition unavailable | Browser does not support it | Type the request in the text box. |
| `amount_too_large` | Above `MAX_AMOUNT_KOBO` | Use a smaller amount or raise the limit. |
| `ambiguous_amount` | Two amounts, zero, or an unsupported currency | Say one naira amount. |
| `account_not_found` | Account not in the mock ledger | Use a demo account. |
| Port already in use | Another server is running | `python run.py --port 8080`, and update `WEBAUTHN_ORIGIN`. |
| Database is locked | Another process holds SQLite | Close other servers/tests and retry. |

---

## 12. What to point out in the demo

- The read-back is generated from stored fields, not from the transcript.
- Saying **yes** only reaches `AWAITING_BIOMETRIC`; the UI says fingerprint
  required.
- Saying **yes** again at that step is refused (`approval_required`).
- The approval challenge is bound to the amount and recipient. Changing either
  after read-back invalidates it.
- A repeated approval or submit does not create a second payment.
- The audit log is a hash chain, and tampering is detectable.
- The bank badge says **simulated** because no real money moves.
