from __future__ import annotations

import asyncio
import json
import logging

from fastapi import WebSocket, WebSocketDisconnect

from .orchestrator import SessionContext
from .prompts import PROMPTS

logger = logging.getLogger("avenza.ws")


async def websocket_endpoint(websocket: WebSocket) -> None:
    app = websocket.app
    settings = app.state.settings
    devices = app.state.devices
    sessions = app.state.sessions
    orchestrator = app.state.orchestrator

    origin = websocket.headers.get("origin")
    if origin and settings.is_production:
        allowed = [o.strip() for o in settings.webauthn_origin.split(",") if o.strip()]
        if origin not in allowed:
            await websocket.close(code=1008, reason="origin not allowed")
            return

    await websocket.accept()
    send_lock = asyncio.Lock()
    ctx: SessionContext | None = None
    tick_task: asyncio.Task | None = None
    utterance: dict | None = None

    async def send_events(events: list[dict]) -> None:
        async with send_lock:
            for event in events:
                await websocket.send_json(event)

    async def send_error(code: str, prompt_id: str, message: str) -> None:
        await send_events(
            [{"type": "error", "code": code, "prompt_id": prompt_id, "message": message}]
        )

    async def tick_loop(session_ctx: SessionContext) -> None:
        try:
            while not session_ctx.closed:
                await asyncio.sleep(2)
                if session_ctx.closed:
                    return
                async with session_ctx.lock:
                    events = await orchestrator.tick(session_ctx)
                if events:
                    await send_events(events)
        except asyncio.CancelledError:
            return
        except Exception:
            logger.exception("tick loop failed")
            return

    try:
        while True:
            message = await websocket.receive()
            if message["type"] == "websocket.disconnect":
                break
            if message.get("bytes") is not None:
                if ctx is None:
                    await send_error("session_required", "session_expired", "start a session first")
                    continue
                if utterance is None:
                    await send_error("no_utterance", "unclear", "utterance.start was not sent")
                    continue
                utterance["buffer"].extend(message["bytes"])
                max_bytes = int(utterance["rate"]) * 2 * 15
                if len(utterance["buffer"]) > max_bytes:
                    utterance = None
                    await send_error("utterance_too_long", "error_generic", "utterance exceeded 15 seconds")
                continue

            raw = message.get("text")
            if raw is None:
                continue
            try:
                data = json.loads(raw)
            except json.JSONDecodeError:
                await send_error("bad_json", "error_generic", "message was not valid JSON")
                continue

            mtype = data.get("type")
            if mtype == "session.start":
                if ctx is not None:
                    await send_error("session_exists", "error_generic", "session already started")
                    continue
                token = data.get("device_token")
                consent_id = data.get("consent_id")
                language = data.get("language") or "en"
                auth = devices.authenticate(token)
                if auth is None:
                    await send_error("device_not_enrolled", "device_required", "device token is not valid")
                    continue
                device, user = auth
                consent = devices.get_consent(consent_id or "")
                if consent is None or consent.user_id != user.id:
                    await send_error("consent_required", "consent_required", "record consent first")
                    continue
                language_verified = language in PROMPTS
                if not language_verified:
                    language = "en"
                try:
                    session = sessions.start(
                        user_id=user.id,
                        device_id=device.id,
                        consent_id=consent.id,
                        language=language,
                    )
                except Exception as exc:
                    await send_error("session_error", "error_generic", str(exc))
                    continue

                ctx = SessionContext(
                    session_id=session.id,
                    user_id=user.id,
                    device_id=device.id,
                    language=language,
                    display_name=user.display_name,
                )
                ctx.send = send_events

                prompts = dict(PROMPTS["en"])
                prompts.update(PROMPTS.get(language, {}))
                await send_events(
                    [
                        {
                            "type": "session.ready",
                            "session_id": session.id,
                            "user": {"id": user.id, "display_name": user.display_name},
                            "language": language,
                            "language_verified": language_verified,
                            "bank": {
                                "mode": settings.bank_mode,
                                "simulated": settings.bank_is_simulated,
                            },
                            "stt": {
                                "provider": app.state.stt.name,
                                "simulated": app.state.stt.simulated,
                            },
                            "tts": {
                                "provider": app.state.tts.name,
                                "simulated": app.state.tts.simulated,
                            },
                            "webauthn": {
                                "rp_id": settings.webauthn_rp_id,
                                "origin": settings.webauthn_origin,
                            },
                            "dev_approval_allowed": app.state.security.dev_approval_allowed(),
                            "approval_ttl_s": settings.approval_ttl_s,
                            "prompts": prompts,
                        }
                    ]
                )
                if not language_verified:
                    await send_events([orchestrator._prompt(ctx, "unsupported_language")])
                await send_events(await orchestrator.start_events(ctx))
                tick_task = asyncio.create_task(tick_loop(ctx))
                continue

            if ctx is None:
                await send_error("session_required", "session_expired", "start a session first")
                continue

            if mtype == "utterance.start":
                try:
                    rate = int(data.get("sample_rate", 16000))
                except (TypeError, ValueError):
                    rate = 16000
                if rate < 8000 or rate > 48000:
                    await send_error("bad_sample_rate", "error_generic", "sample rate must be 8000-48000")
                    continue
                utterance = {"buffer": bytearray(), "rate": rate, "text": None}
                continue

            if mtype == "utterance.text":
                text = str(data.get("text", "")).strip()
                final = bool(data.get("final", True))
                if not final:
                    await send_events([{"type": "transcript.partial", "text": text}])
                    continue
                rate = utterance["rate"] if utterance else 16000
                utterance = None
                events = await orchestrator.process_audio(ctx, b"", rate, provided_text=text)
                await send_events(events)
                continue

            if mtype == "utterance.end":
                if utterance is None:
                    continue
                pcm = bytes(utterance["buffer"])
                rate = utterance["rate"]
                text = utterance["text"]
                utterance = None
                events = await orchestrator.process_audio(ctx, pcm, rate, provided_text=text)
                await send_events(events)
                continue

            if mtype == "approval.response":
                credential = data.get("credential")
                dev = bool(data.get("dev", False))
                events = await orchestrator.handle_approval(ctx, credential, dev=dev)
                await send_events(events)
                continue

            if mtype == "ping":
                await send_events([{"type": "pong"}])
                continue

            if mtype == "session.end":
                break

            # Unknown message types are ignored by contract.
            logger.debug("ignored websocket message type %s", mtype)

    except WebSocketDisconnect:
        pass
    except Exception:
        logger.exception("websocket failed")
        try:
            await send_error("internal_error", "error_generic", "internal error")
        except Exception:
            pass
    finally:
        if ctx is not None:
            ctx.closed = True
            if ctx.hold_task and not ctx.hold_task.done():
                ctx.hold_task.cancel()
            if tick_task and not tick_task.done():
                tick_task.cancel()
            try:
                sessions.close(ctx.session_id)
            except Exception:
                logger.exception("failed to close session")
