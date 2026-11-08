# Avenza — voice banking layer

Avenza is a working prototype of a voice-first transfer flow with one rule:

> **Voice only drafts a transfer. Only a customer fingerprint, verified by the
> backend, may move money.**

Avenza holds no funds, runs no KYC, and does not settle payments. It sits in
front of a bank adapter. The included bank is a local simulation that moves no
real money and is labelled as simulated everywhere it appears.

---

## What this build actually is

| Area | Status |
| --- | --- |
| Transfer state machine | **Working.** Every transition is checked in code. Voice alone can never reach `APPROVED`. |
| Amount / account / bank parser | **Working and deterministic.** Digits, spoken English, mixed-language number words, 15 vs 50, whole-kobo amounts. |
| WebAuthn fingerprint approval | **Working.** Real registration and assertion verification with the `webauthn` library; replay, expiry and field tampering are rejected. Proven by tests with a software authenticator. |
| Idempotency / exactly-once | **Working against the mock bank.** Unique idempotency key, status check before retry, duplicate replies settle once. |
| Hash-chained audit log | **Working.** Tamper, deletion and broken links are detected by `/audit/verify`. |
| WebSocket + PWA client | **Working.** Installable PWA, mic capture, VAD, read-back, fingerprint approval, typed-text fallback. |
| Bank | **Simulated** by default. A sandbox adapter exists but its endpoints are placeholders marked `CONFIRM`. No real money moves. |
| Speech-to-text | **Simulated** by default: the browser's speech recognition or typed text supplies the transcript. Deepgram/Yarn/Sahara adapters exist but are not tested against live accounts. |
| Text-to-speech | **Simulated** by default: the browser speaks the prompts. Optional WAV prompt files can be generated. |
| Yorùbá / Hausa / Igbo / Pidgin | **Unverified translations.** Only English is claimed as checked. |
| KYC, holding funds, settlement, real bank | **Not implemented.** Out of scope for this design. |

The source specification is a design document. This repository implements the
parts that can be built and tested without a real bank, real provider
credentials, or a native-speaker language test. It does not pretend otherwise.

---

## Quick start

Requires Python 3.11+ (developed on 3.14).

```powershell
python -m pip install -r requirements.txt
python run.py
```

Open <http://localhost:8000>. The default database is SQLite (`avenza.db`), the
bank is the local mock, and the PWA is served from the same origin.

For a real fingerprint approval:

1. Enter a display name.
2. Tick the consent box.
3. Click **Record consent and enrol fingerprint**.
4. Accept the browser's passkey prompt (Windows Hello, Touch ID, Android
   fingerprint, etc.).
5. Click **Start listening**, then say or type:
   `Send five thousand naira to GTBank account 0123456789`.
6. Listen to the read-back and say **yes**.
7. Approve the exact payment with your fingerprint.

`localhost` is a secure context, so this works without HTTPS. A phone on the LAN
needs HTTPS; see [Phone testing](#phone-testing).

### Demo without a fingerprint sensor

For a quick UI demo only:

```powershell
$env:ALLOW_DEV_APPROVAL = "true"
python run.py
```

The PWA then shows **Dev enrolment (no fingerprint)** and **Simulate
fingerprint (dev)**. These are gated: they are refused when `APP_ENV=production`
and are never enabled in the Docker compose file. See
[SECURITY.md](docs/SECURITY.md).

### Tests

```powershell
python -m pytest -q
```

The suite covers the parser, state machine, transfers (double tap, repeated
command, lost reply, timeout then retry, exactly one payment), WebAuthn approval
(no approval / expired / replayed / modified / wrong device), audit tampering,
the orchestrator, REST and WebSocket behaviour. It uses an in-memory SQLite
database and a software authenticator; no network or hardware is required.

---

## Architecture

```
┌──────────────────────────── PWA (web/) ────────────────────────────┐
│  mic → energy VAD → 16 kHz mono PCM ─┐                             │
│  browser SpeechRecognition ──────────┤                             │
│  typed text (demo fallback) ─────────┤                             │
│  WebAuthn create()/get() ────────────┼── fingerprint assertion     │
└──────────────────────────────────────┼─────────────────────────────┘
                                       │ WebSocket /ws
┌──────────────────────────────────────▼─────────────────────────────┐
│ FastAPI                                                            │
│  ws.py          session + utterance protocol                       │
│  orchestrator   turn handling, field collection, read-back         │
│  intent.py      deterministic keywords; optional LLM (intent only) │
│  parser.py      amount / account / bank / time (authoritative)     │
│  services/      transfers, devices, sessions                       │
│  security.py    WebAuthn registration + approval + unlock          │
│  bank/          BankAdapter → MockBank or SandboxBank              │
│  audit.py       SHA-256 hash chain                                 │
└──────────────────────────────────────┬─────────────────────────────┘
                                       │
                          SQLite / PostgreSQL (SQLAlchemy 2)
```

The LLM, when enabled, may only classify intent. It never produces an amount.
Amounts always come from the deterministic parser.

---

## The transfer state machine

```
DRAFTED → READBACK_PLAYED → CONFIRMED_BY_VOICE → AWAITING_BIOMETRIC
        → APPROVED → SUBMITTED → SETTLED
                                  ↘ FAILED
                                  ↘ PENDING_UNKNOWN → SETTLED | FAILED

CANCELLED and EXPIRED are reachable only before submission.
```

Rules enforced in `avenza/state_machine.py` and at the service boundary:

- Only code transitions states. No timer or silence advances one.
- `APPROVED` is reachable only from `AWAITING_BIOMETRIC`, and only after a
  verified WebAuthn assertion.
- `SUBMITTED` is reachable only from `APPROVED`.
- `SUBMITTED` cannot be cancelled.
- Drafts expire. A payment that has not been submitted expires without moving
  money.
- A timeout never triggers a blind retry: the service checks status first.

Money is stored as an integer number of kobo. No floats. No PINs, card numbers,
or funds are stored.

---

## Configuration

Copy `.env.example` to `.env` and edit. The important switches:

| Variable | Default | Meaning |
| --- | --- | --- |
| `DATABASE_URL` | `sqlite:///./avenza.db` | SQLite out of the box; `postgresql+psycopg://...` for Postgres. |
| `BANK_MODE` | `mock` | `mock` = local simulation, `sandbox` = organiser adapter with placeholder endpoints. |
| `STT_PROVIDER` | `mock` | `mock`, `deepgram`, `yarn`, `sahara`. |
| `TTS_PROVIDER` | `mock` | `mock` (browser speech) or `yarn`. |
| `ALLOW_TEXT_UTTERANCE` | `true` | Allows typed/browser-recognition text in the demo. |
| `WEBAUTHN_RP_ID` | `localhost` | Registrable domain; must match the browser URL. |
| `WEBAUTHN_ORIGIN` | `http://localhost:8000` | Exact scheme + host + port. |
| `MAX_AMOUNT_KOBO` | `100000000` | ₦1,000,000 default cap. |
| `NEW_RECIPIENT_HOLD_S` | `10` | Safety pause before fingerprint for a large new recipient. |
| `DRAFT_TTL_S` | `120` | Draft lifetime. |
| `APPROVAL_TTL_S` | `60` | WebAuthn challenge lifetime. |
| `DEV_TOOLS` | `true` | Dev endpoints and demo accounts. Refused in production. |
| `ALLOW_DEV_APPROVAL` | `false` | Test/demo bypass. **Never enable in production.** |
| `ADMIN_TOKEN` | empty | When set, `/audit/*` and `/dev/*` require `X-Admin-Token`. |

`APP_ENV=production` disables dev enrolment, the dev approval bypass, permissive
CORS, and unauthenticated admin access. It does not by itself make the bank real.

---

## API at a glance

| Method | Path | Purpose |
| --- | --- | --- |
| `GET` | `/health` | Liveness, bank/STT/TTS mode, limits, WebAuthn config. |
| `GET` | `/config` | Public demo config and mock accounts. |
| `POST` | `/devices/register/options` | Start WebAuthn enrolment. |
| `POST` | `/devices/register/verify` | Verify attestation, return device token once. |
| `POST` | `/consents` | Record consent (device token required). |
| `GET` | `/me` | Current customer and device. |
| `GET` | `/transfers` | History for a date range. |
| `GET` | `/transfers/{id}` | One transfer, masked account number. |
| `GET` | `/transfers/{id}/receipt` | Bank receipt (mock or sandbox). |
| `POST` | `/locks` | Emergency lock. |
| `POST` | `/locks/options` | WebAuthn unlock challenge. |
| `POST` | `/locks/unlock` | Verify fingerprint and unlock. |
| `GET` | `/audit/verify` | Verify the hash chain. |
| `GET` | `/audit/recent` | Recent audit entries. |
| `WS` | `/ws` | Voice session protocol. |

Full message shapes, event ordering and error codes are in
[PROTOCOL.md](docs/PROTOCOL.md).

---

## Security in one page

- A WebSocket device token identifies a session. It **cannot** approve a
  payment. Only a WebAuthn assertion can.
- The approval challenge is rebuilt on the server from the stored transfer
  (amount, bank, account, recipient, nonce). Changing any stored field after the
  challenge is issued makes verification fail.
- Challenges are single-use, expire, and are bound to the credential.
- Three failed fingerprint reads cancel the draft.
- The audit log is a SHA-256 chain. Editing, deleting or reordering rows is
  detected.
- The mock bank deduplicates on the idempotency key, and the service checks
  status before retrying a timeout.

The full threat model, deployment checklist and known gaps are in
[SECURITY.md](docs/SECURITY.md).

---

## Docker and PostgreSQL

```powershell
docker compose up --build
```

Open <http://localhost:8000>. The compose file uses PostgreSQL 16, the mock bank,
and `ALLOW_DEV_APPROVAL=false`. Set `ADMIN_TOKEN` in `docker-compose.yml` before
sharing a demo instance.

To use Postgres without Docker, set:

```
DATABASE_URL=postgresql+psycopg://avenza:avenza@localhost:5432/avenza
```

Tables are created on startup. There are no migrations in this prototype.

---

## Phone testing (HTTPS)

WebAuthn needs a secure context. `localhost` counts; a LAN IP does not.

```powershell
python scripts/make_cert.py
$env:WEBAUTHN_ORIGIN = "https://192.168.1.20:8000"   # your LAN IP
python run.py --host 0.0.0.0 --https
```

Open the `https://...` URL on the phone, accept the development certificate
warning, then reload. The origin in `.env` must exactly match the URL in the
address bar, including port. The generated certificate covers `localhost`,
loopback and the machine's current LAN addresses.

---

## Prompt audio

Prompts are text-first and the PWA speaks them with the browser voice. To serve
fixed WAV files instead:

```powershell
python scripts/make_prompts.py          # English only
python scripts/make_prompts.py --force
```

The generator uses System.Speech on Windows, `say` on macOS, or `espeak-ng` on
Linux. Files are written to `web/prompts/` and picked up automatically. Non-
English prompts require `--allow-unverified`.

---

## Demo data

```powershell
python scripts/seed.py
```

| Bank | Account | Name |
| --- | --- | --- |
| GTBank | `0123456789` | Aisha Bello |
| GTBank | `0987654321` | Aisha Bello-Okafor |
| Access Bank | `1111222333` | Chinedu Okafor |
| Zenith Bank | `5555666777` | Fatima Yusuf |
| Kuda | `9999000011` | Muiz Adeyemi |
| First Bank | `2222333344` | Ngozi Eze |
| OPay | `3333444455` | Tunde Bakare |
| Moniepoint | `4444555566` | Zainab Musa |

A step-by-step script, including failure-mode demos, is in
[DEMO.md](docs/DEMO.md).

---

## Project layout

```
avenza/
  main.py          FastAPI app, REST routes, static PWA
  ws.py            WebSocket protocol
  orchestrator.py  turn flow, field collection, read-back, approval
  parser.py        deterministic amount/account/bank/time parser
  intent.py        keyword intent + optional LLM classifier
  state_machine.py transfer states and allowed transitions
  security.py      WebAuthn registration/approval/unlock
  audit.py         hash-chained audit log
  models.py        SQLAlchemy models
  bank/            adapter, mock ledger, sandbox placeholder
  providers/       STT/TTS interfaces and adapters
  services/        transfers, devices, sessions
web/               installable PWA (HTML/CSS/JS, service worker, icons)
scripts/           cert, prompt audio, seed, icons
tests/             pytest suite + software WebAuthn authenticator
docs/              protocol, security, demo
```

---

## Limitations

- No real bank, no settlement, no funds, no KYC, no card or PIN handling.
- The sandbox adapter is a placeholder; endpoints must be confirmed with the
  provider before use.
- Live STT/TTS adapters are not verified against provider accounts.
- Non-English prompts are unverified translations and the UI says so.
- SQLite is the default. Postgres is supported and used by Docker, but there is
  no migration tooling.
- The audit chain is tamper-evident, not tamper-proof: someone with direct
  database access can rewrite the whole chain. Ship the head hash elsewhere for
  stronger guarantees.
- This is a prototype, not a licensed financial product.

## Licence

No licence is granted for production financial use. Use for evaluation,
research and demonstration.
