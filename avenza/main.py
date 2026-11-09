from __future__ import annotations

import logging
from contextlib import asynccontextmanager
from datetime import timedelta
from pathlib import Path

from fastapi import Depends, FastAPI, Header, HTTPException, Query, Request, WebSocket
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field
from sqlalchemy import select, text

from . import __version__
from .ai import build_ai
from .audit import AuditService
from .bank import BANK_NAMES, DEMO_ACCOUNTS, MockBank, build_bank, seed_mock_bank
from .config import Settings, load_settings
from .db import init_db, make_engine, make_session_factory
from .intent import IntentClassifier
from .models import AuditLog, Device, Transfer, User
from .money import format_naira
from .orchestrator import TurnOrchestrator
from .prompts import PROMPTS, PromptLibrary
from .providers import build_stt, build_tts
from .security import ApprovalError, WebAuthnService
from .services import DeviceService, SessionError, SessionService, TransferLocked, TransferNotFound, TransferService
from .timeutil import iso, parse_iso, utcnow
from .ws import websocket_endpoint

logger = logging.getLogger("avenza.api")


class ConsentIn(BaseModel):
    wording_version: str = Field(min_length=1, max_length=32)
    language: str = Field(default="en", max_length=16)


class RegisterOptionsIn(BaseModel):
    display_name: str = Field(default="", max_length=200)
    language: str = Field(default="en", max_length=16)
    user_id: str | None = None
    device_token: str | None = None


class RegisterVerifyIn(BaseModel):
    challenge_id: str
    credential: dict


class LockUnlockIn(BaseModel):
    challenge_id: str
    credential: dict


class BankModeIn(BaseModel):
    mode: str
    delay_ms: int | None = Field(default=None, ge=0, le=60_000)


class Auth:
    def __init__(self, device: Device, user: User):
        self.device = device
        self.user = user


def _extract_token(authorization: str | None, x_device_token: str | None) -> str | None:
    if x_device_token:
        return x_device_token.strip()
    if authorization and authorization.lower().startswith("bearer "):
        return authorization[7:].strip()
    return None


def create_app(settings: Settings | None = None) -> FastAPI:
    settings = settings or load_settings()
    logging.basicConfig(
        level=getattr(logging, settings.log_level, logging.INFO),
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )

    engine = make_engine(settings.database_url)
    init_db(engine)
    session_factory = make_session_factory(engine)
    if settings.bank_mode == "mock":
        seed_mock_bank(session_factory)

    audit = AuditService(session_factory, engine.dialect.name)
    security = WebAuthnService(settings)
    prompts = PromptLibrary(settings.prompt_dir)
    stt = build_stt(settings)
    tts = build_tts(settings)
    ai = build_ai(settings)
    intent = ai if ai is not None else IntentClassifier(settings)
    bank = build_bank(settings, session_factory)
    devices = DeviceService(session_factory, audit, security, settings)
    sessions = SessionService(session_factory, audit, settings)
    transfers = TransferService(session_factory, audit, bank, security, prompts, settings)
    orchestrator = TurnOrchestrator(
        settings, prompts, stt, tts, intent, bank, transfers, sessions, ai=ai
    )

    settings.web_dir.mkdir(parents=True, exist_ok=True)
    settings.prompt_dir.mkdir(parents=True, exist_ok=True)

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        logger.info(
            "Avenza %s starting env=%s bank=%s stt=%s tts=%s db=%s",
            __version__,
            settings.env,
            settings.bank_mode,
            stt.name,
            tts.name,
            engine.dialect.name,
        )
        if settings.allow_dev_approval and not settings.is_production:
            logger.warning(
                "ALLOW_DEV_APPROVAL is enabled: fingerprint approval can be bypassed in tests/demo only"
            )
        yield
        engine.dispose()

    app = FastAPI(
        title="Avenza API",
        version=__version__,
        description=(
            "Voice banking layer. Voice only drafts a request; a WebAuthn fingerprint "
            "approval verified by the backend is required before submission. "
            "Avenza holds no funds."
        ),
        lifespan=lifespan,
    )
    app.state.settings = settings
    app.state.engine = engine
    app.state.session_factory = session_factory
    app.state.audit = audit
    app.state.security = security
    app.state.prompts = prompts
    app.state.stt = stt
    app.state.tts = tts
    app.state.intent = intent
    app.state.ai = ai
    app.state.bank = bank
    app.state.devices = devices
    app.state.sessions = sessions
    app.state.transfers = transfers
    app.state.orchestrator = orchestrator

    origins = ["*"] if not settings.is_production else [
        o.strip() for o in settings.webauthn_origin.split(",") if o.strip()
    ]
    app.add_middleware(
        CORSMiddleware,
        allow_origins=origins,
        allow_credentials=False,
        allow_methods=["*"],
        allow_headers=["*"],
    )

    def get_auth(
        authorization: str | None = Header(default=None),
        x_device_token: str | None = Header(default=None, alias="X-Device-Token"),
    ) -> Auth:
        token = _extract_token(authorization, x_device_token)
        auth = devices.authenticate(token)
        if auth is None:
            raise HTTPException(status_code=401, detail="device token required or invalid")
        return Auth(*auth)

    def require_admin(x_admin_token: str | None = Header(default=None, alias="X-Admin-Token")) -> None:
        if settings.admin_token:
            if x_admin_token != settings.admin_token:
                raise HTTPException(status_code=403, detail="admin token required")
            return
        if settings.dev_tools and not settings.is_production:
            return
        raise HTTPException(status_code=403, detail="admin access disabled")

    # ------------------------------------------------------------------
    # Health / config
    # ------------------------------------------------------------------
    @app.get("/health")
    def health():
        db_ok = True
        try:
            with session_factory() as db:
                db.execute(text("SELECT 1"))
        except Exception:
            db_ok = False
        bank_ready = bool(getattr(bank, "ready", True))
        return {
            "status": "ok" if db_ok and bank_ready else "degraded",
            "app": settings.app_name,
            "version": __version__,
            "time": iso(utcnow()),
            "env": settings.env,
            "database": {"dialect": engine.dialect.name, "ok": db_ok},
            "bank": {
                "mode": settings.bank_mode,
                "simulated": settings.bank_is_simulated,
                "ready": bank_ready,
            },
            "stt": {"provider": stt.name, "simulated": stt.simulated},
            "tts": {"provider": tts.name, "simulated": tts.simulated},
            "llm": {"enabled": settings.llm_enabled, "amount_authority": "deterministic_parser_only"},
            "ai": ai.status() if ai is not None else {"enabled": False},
            "webauthn": {
                "rp_id": settings.webauthn_rp_id,
                "origin": settings.webauthn_origin,
                "secure_context_required": True,
            },
            "dev_tools": settings.dev_tools,
            "dev_approval_allowed": security.dev_approval_allowed(),
            "limits": {
                "max_amount_kobo": settings.max_amount_kobo,
                "max_amount_display": format_naira(settings.max_amount_kobo),
                "draft_ttl_s": settings.draft_ttl_s,
                "approval_ttl_s": settings.approval_ttl_s,
            },
            "languages": {
                "verified": prompts.verified_languages(),
                "available_unverified": [k for k in PROMPTS.keys() if k not in prompts.verified_languages()],
            },
        }

    @app.get("/config")
    def public_config():
        demo_accounts = []
        if settings.bank_mode == "mock" and settings.dev_tools:
            demo_accounts = [
                {
                    "bank_code": code,
                    "bank_name": BANK_NAMES.get(code, code),
                    "account_number": number,
                    "account_name": name,
                }
                for code, number, name in DEMO_ACCOUNTS
            ]
        return {
            "app": settings.app_name,
            "version": __version__,
            "bank": {"mode": settings.bank_mode, "simulated": settings.bank_is_simulated},
            "webauthn": {"rp_id": settings.webauthn_rp_id, "origin": settings.webauthn_origin},
            "dev_approval_allowed": security.dev_approval_allowed(),
            "allow_text_utterance": settings.allow_text_utterance,
            "limits": {
                "max_amount_display": format_naira(settings.max_amount_kobo),
                "large_amount_display": format_naira(settings.large_amount_threshold_kobo),
                "new_recipient_hold_s": settings.new_recipient_hold_s,
            },
            "languages": {
                "verified": prompts.verified_languages(),
                "available": list(PROMPTS.keys()),
            },
            "demo_accounts": demo_accounts,
        }

    @app.get("/ai/status")
    def ai_status():
        return {
            "ai": ai.status() if ai is not None else {"enabled": False},
            "amount_authority": "backend_deterministic_parser",
            "approval": "webauthn_backend_verified_only",
        }

    @app.get("/prompts")
    def all_prompts():
        return {
            "verified_languages": prompts.verified_languages(),
            "prompts": PROMPTS,
        }

    # ------------------------------------------------------------------
    # Enrolment and consent
    # ------------------------------------------------------------------
    @app.post("/devices/register/options")
    def register_options(body: RegisterOptionsIn):
        if not body.user_id and not body.display_name.strip():
            raise HTTPException(status_code=400, detail="display_name is required for a new customer")
        language = body.language if body.language in PROMPTS else "en"
        try:
            return devices.begin_registration(
                display_name=body.display_name,
                language=language,
                user_id=body.user_id,
                device_token=body.device_token,
            )
        except ApprovalError as exc:
            raise HTTPException(status_code=400, detail=str(exc))

    @app.post("/devices/register/verify")
    def register_verify(body: RegisterVerifyIn, request: Request):
        try:
            result = devices.verify_registration(
                challenge_id=body.challenge_id,
                credential=body.credential,
                ip=request.client.host if request.client else "",
                user_agent=request.headers.get("user-agent", ""),
            )
            return result
        except ApprovalError as exc:
            raise HTTPException(status_code=400, detail=str(exc))

    @app.post("/dev/devices/register")
    def dev_register(body: RegisterOptionsIn):
        """Developer-only enrolment used by the PWA demo and automated tests.

        It creates a device with no WebAuthn public key, so it cannot approve a
        payment unless the separately gated ALLOW_DEV_APPROVAL bypass is on.
        Disabled in production.
        """
        if not devices.dev_tools_allowed():
            raise HTTPException(status_code=403, detail="developer enrolment is disabled")
        language = body.language if body.language in PROMPTS else "en"
        try:
            return devices.dev_register(
                display_name=body.display_name,
                language=language,
            )
        except ApprovalError as exc:
            raise HTTPException(status_code=403, detail=str(exc))

    @app.post("/consents")
    def record_consent(body: ConsentIn, auth: Auth = Depends(get_auth)):
        consent = devices.record_consent(
            user_id=auth.user.id,
            wording_version=body.wording_version,
            language=body.language if body.language in PROMPTS else "en",
        )
        return {
            "consent_id": consent.id,
            "user_id": consent.user_id,
            "wording_version": consent.wording_version,
            "language": consent.language,
            "timestamp": iso(consent.timestamp),
        }

    @app.get("/me")
    def me(auth: Auth = Depends(get_auth)):
        return {
            "user": {"id": auth.user.id, "display_name": auth.user.display_name, "language": auth.user.language},
            "device": {"id": auth.device.id, "credential_id": auth.device.credential_id},
        }

    # ------------------------------------------------------------------
    # Transfers / history
    # ------------------------------------------------------------------
    @app.get("/transfers")
    def list_transfers(
        start: str | None = Query(default=None),
        end: str | None = Query(default=None),
        limit: int = Query(default=50, ge=1, le=100),
        auth: Auth = Depends(get_auth),
    ):
        end_dt = parse_iso(end) if end else utcnow()
        start_dt = parse_iso(start) if start else end_dt - timedelta(days=30)
        rows = transfers.history(auth.user.id, start_dt, end_dt, limit)
        return {
            "transactions": [TransferService.summary(r) for r in rows],
            "bank_simulated": settings.bank_is_simulated,
        }

    @app.get("/transfers/{transfer_id}")
    def get_transfer(transfer_id: str, auth: Auth = Depends(get_auth)):
        transfer = transfers.get(transfer_id)
        if transfer.user_id != auth.user.id:
            raise HTTPException(status_code=404, detail="transfer not found")
        data = TransferService.summary(transfer)
        data.update(
            {
                "account_number_masked": "*" * 6 + transfer.account_number[-4:],
                "expires_at": iso(transfer.expires_at),
                "approval_expires_at": iso(transfer.approval_expires_at),
                "confirmations_required": transfer.confirmations_required,
                "confirmations_done": transfer.confirmations_done,
            }
        )
        return data

    @app.get("/transfers/{transfer_id}/receipt")
    async def get_receipt(transfer_id: str, auth: Auth = Depends(get_auth)):
        transfer = transfers.get(transfer_id)
        if transfer.user_id != auth.user.id:
            raise HTTPException(status_code=404, detail="transfer not found")
        if not transfer.bank_reference:
            raise HTTPException(status_code=409, detail="transfer has not been submitted")
        receipt = await bank.get_receipt(transfer.bank_reference)
        if receipt is None:
            raise HTTPException(status_code=404, detail="receipt not found")
        receipt["simulated"] = settings.bank_is_simulated
        return receipt

    # ------------------------------------------------------------------
    # Emergency lock
    # ------------------------------------------------------------------
    @app.post("/locks")
    def engage_lock(auth: Auth = Depends(get_auth)):
        lock = transfers.engage_lock(auth.user.id, reason="customer_request")
        return {"locked": True, "lock_id": lock.id, "locked_at": iso(lock.locked_at)}

    @app.get("/locks/status")
    def lock_status(auth: Auth = Depends(get_auth)):
        with session_factory() as db:
            locked = transfers.is_locked(db, auth.user.id)
        return {"locked": locked}

    @app.post("/locks/options")
    def lock_options(auth: Auth = Depends(get_auth)):
        with audit.unit_of_work() as db:
            device = db.get(Device, auth.device.id)
            user = db.get(User, auth.user.id)
            options = security.unlock_options(db, user, device)
            return options

    @app.post("/locks/unlock")
    def unlock(body: LockUnlockIn, auth: Auth = Depends(get_auth)):
        try:
            with audit.unit_of_work() as db:
                device = db.get(Device, auth.device.id)
                security.verify_unlock(db, body.challenge_id, device, body.credential)
                audit.record(
                    db,
                    "lock.unlock_verified",
                    details={"user_id": auth.user.id, "device_id": device.id},
                )
            transfers.mark_unlocked(auth.user.id)
            return {"locked": False}
        except ApprovalError as exc:
            raise HTTPException(status_code=400, detail=str(exc))

    # ------------------------------------------------------------------
    # Audit
    # ------------------------------------------------------------------
    @app.get("/audit/verify")
    def verify_audit(_: None = Depends(require_admin)):
        with session_factory() as db:
            result = audit.verify(db)
        return result

    @app.get("/audit/recent")
    def recent_audit(limit: int = Query(default=50, ge=1, le=500), _: None = Depends(require_admin)):
        with session_factory() as db:
            rows = db.scalars(
                select(AuditLog)
                .order_by(AuditLog.seq.desc())
                .limit(limit)
            ).all()
            return {
                "entries": [
                    {
                        "seq": row.seq,
                        "event": row.event,
                        "transfer_id": row.transfer_id,
                        "session_id": row.session_id,
                        "details": row.details,
                        "previous_hash": row.previous_hash,
                        "row_hash": row.row_hash,
                        "created_at": iso(row.created_at),
                    }
                    for row in rows
                ]
            }

    # ------------------------------------------------------------------
    # Dev/demo bank controls
    # ------------------------------------------------------------------
    @app.post("/dev/bank/mode")
    def set_bank_mode(body: BankModeIn, _: None = Depends(require_admin)):
        if not isinstance(bank, MockBank):
            raise HTTPException(status_code=400, detail="bank mode controls are only available in mock mode")
        try:
            bank.set_mode(body.mode, body.delay_ms)
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc))
        audit.record_standalone("dev.bank_mode", details={"mode": body.mode, "delay_ms": body.delay_ms})
        return {"bank": {"mode": body.mode, "delay_ms": body.delay_ms, "simulated": True}}

    @app.get("/dev/bank/accounts")
    def demo_accounts(_: None = Depends(require_admin)):
        if not settings.dev_tools:
            raise HTTPException(status_code=403, detail="dev tools disabled")
        return {
            "accounts": [
                {
                    "bank_code": code,
                    "bank_name": BANK_NAMES.get(code, code),
                    "account_number": number,
                    "account_name": name,
                }
                for code, number, name in DEMO_ACCOUNTS
            ],
            "bank": {"mode": settings.bank_mode, "simulated": settings.bank_is_simulated},
        }

    # ------------------------------------------------------------------
    # WebSocket
    # ------------------------------------------------------------------
    @app.websocket("/ws")
    async def ws(websocket: WebSocket):
        await websocket_endpoint(websocket)

    # ------------------------------------------------------------------
    # Static PWA
    # ------------------------------------------------------------------
    app.mount("/static", StaticFiles(directory=str(settings.web_dir)), name="static")
    app.mount("/prompts", StaticFiles(directory=str(settings.prompt_dir)), name="prompts")

    @app.get("/")
    def index():
        path = settings.web_dir / "index.html"
        if not path.exists():
            return JSONResponse({"detail": "PWA not built"}, status_code=404)
        return FileResponse(path)

    @app.get("/manifest.webmanifest")
    def manifest():
        return FileResponse(settings.web_dir / "manifest.webmanifest", media_type="application/manifest+json")

    @app.get("/sw.js")
    def service_worker():
        return FileResponse(settings.web_dir / "sw.js", media_type="application/javascript")

    @app.get("/favicon.ico")
    def favicon():
        path = settings.web_dir / "icons" / "icon-192.png"
        if path.exists():
            return FileResponse(path)
        return JSONResponse(status_code=204, content=None)

    @app.exception_handler(ApprovalError)
    async def approval_error_handler(request: Request, exc: ApprovalError):
        return JSONResponse(status_code=400, content={"detail": str(exc)})

    @app.exception_handler(TransferNotFound)
    async def transfer_not_found_handler(request: Request, exc: TransferNotFound):
        return JSONResponse(status_code=404, content={"detail": "transfer not found"})

    @app.exception_handler(TransferLocked)
    async def transfer_locked_handler(request: Request, exc: TransferLocked):
        return JSONResponse(status_code=423, content={"detail": "transfers are locked"})

    @app.exception_handler(SessionError)
    async def session_error_handler(request: Request, exc: SessionError):
        return JSONResponse(status_code=400, content={"detail": str(exc)})

    return app
