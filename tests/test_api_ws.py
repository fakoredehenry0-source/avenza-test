from __future__ import annotations

from fastapi.testclient import TestClient
from sqlalchemy import select

from avenza.main import create_app
from avenza.models import BankTransfer

REQUEST = "Send five thousand naira to GTBank account 0123456789"


def enrol(client, *, consent: bool = True):
    response = client.post(
        "/dev/devices/register",
        json={"display_name": "WS Test", "language": "en"},
    )
    assert response.status_code == 200, response.text
    data = response.json()
    token = data["device_token"]
    consent_id = None
    if consent:
        response = client.post(
            "/consents",
            json={"wording_version": "2026-11-08-v1", "language": "en"},
            headers={"Authorization": f"Bearer {token}"},
        )
        assert response.status_code == 200, response.text
        consent_id = response.json()["consent_id"]
    return token, consent_id


def take(ws, count: int) -> list[dict]:
    return [ws.receive_json() for _ in range(count)]


def start_session(ws, token, consent_id, language: str = "en") -> list[dict]:
    ws.send_json(
        {
            "type": "session.start",
            "device_token": token,
            "consent_id": consent_id,
            "language": language,
        }
    )
    return take(ws, 3)


def bank_rows(client) -> list[BankTransfer]:
    with client.app.state.session_factory() as db:
        return list(db.scalars(select(BankTransfer)).all())


# ---------------------------------------------------------------------------
# REST
# ---------------------------------------------------------------------------


def test_health_reports_simulated_bank(client):
    data = client.get("/health").json()
    assert data["status"] == "ok"
    assert data["bank"]["simulated"] is True
    assert data["llm"]["amount_authority"] == "deterministic_parser_only"
    assert data["dev_approval_allowed"] is True


def test_config_exposes_demo_accounts(client):
    data = client.get("/config").json()
    assert data["bank"]["simulated"] is True
    assert len(data["demo_accounts"]) >= 8
    assert data["languages"]["verified"] == ["en"]


def test_protected_routes_require_a_device_token(client):
    assert client.get("/me").status_code == 401
    assert client.get("/transfers").status_code == 401
    assert client.post("/locks").status_code == 401


def test_dev_enrolment_returns_a_token(client):
    token, consent_id = enrol(client)
    assert token and consent_id
    me = client.get("/me", headers={"Authorization": f"Bearer {token}"}).json()
    assert me["user"]["display_name"] == "WS Test"


def test_dev_enrolment_can_be_disabled(settings):
    app = create_app(settings.for_test(dev_tools=False))
    with TestClient(app) as client:
        response = client.post("/dev/devices/register", json={"display_name": "No"})
        assert response.status_code == 403


def test_production_never_allows_dev_approval(settings):
    app = create_app(settings.for_test(env="production", allow_dev_approval=True))
    with TestClient(app) as client:
        data = client.get("/health").json()
        assert data["dev_approval_allowed"] is False


# ---------------------------------------------------------------------------
# WebSocket happy path
# ---------------------------------------------------------------------------


def test_websocket_full_flow_settles_once(client):
    token, consent_id = enrol(client)
    transfer_id = None
    with client.websocket_connect("/ws") as ws:
        events = start_session(ws, token, consent_id)
        assert [e["type"] for e in events] == ["session.ready", "prompt.play", "listening"]
        assert events[0]["bank"]["simulated"] is True

        ws.send_json({"type": "utterance.text", "text": REQUEST, "final": True})
        events = take(ws, 4)
        assert [e["type"] for e in events] == [
            "transcript.final",
            "state.changed",
            "prompt.play",
            "listening",
        ]
        assert events[1]["state"] == "READBACK_PLAYED"
        transfer_id = events[1]["transfer_id"]

        ws.send_json({"type": "utterance.text", "text": "yes", "final": True})
        events = take(ws, 4)
        assert [e["type"] for e in events] == [
            "transcript.final",
            "state.changed",
            "prompt.play",
            "approval.request",
        ]
        assert events[1]["state"] == "AWAITING_BIOMETRIC"
        assert events[3]["options"]["challenge"]
        assert events[3]["transfer"]["amount_display"] == "₦5,000.00"

        ws.send_json({"type": "approval.response", "dev": True})
        events = take(ws, 3)
        assert [e["type"] for e in events] == ["state.changed", "prompt.play", "listening"]
        assert events[0]["state"] == "SETTLED"

    assert len(bank_rows(client)) == 1

    headers = {"Authorization": f"Bearer {token}"}
    detail = client.get(f"/transfers/{transfer_id}", headers=headers).json()
    assert detail["state"] == "SETTLED"
    assert detail["reference"]

    receipt = client.get(f"/transfers/{transfer_id}/receipt", headers=headers)
    assert receipt.status_code == 200
    assert receipt.json()["simulated"] is True

    audit = client.get("/audit/verify").json()
    assert audit["ok"] is True
    assert audit["entries"] > 0


def test_websocket_requires_a_valid_device_token(client):
    _, consent_id = enrol(client)
    with client.websocket_connect("/ws") as ws:
        ws.send_json(
            {
                "type": "session.start",
                "device_token": "not-a-token",
                "consent_id": consent_id,
                "language": "en",
            }
        )
        event = ws.receive_json()
        assert event["type"] == "error"
        assert event["code"] == "device_not_enrolled"


def test_websocket_requires_recorded_consent(client):
    token, _ = enrol(client, consent=False)
    with client.websocket_connect("/ws") as ws:
        ws.send_json(
            {
                "type": "session.start",
                "device_token": token,
                "consent_id": "missing",
                "language": "en",
            }
        )
        event = ws.receive_json()
        assert event["type"] == "error"
        assert event["code"] == "consent_required"


def test_voice_cannot_approve_over_the_socket(client):
    token, consent_id = enrol(client)
    with client.websocket_connect("/ws") as ws:
        start_session(ws, token, consent_id)
        ws.send_json({"type": "utterance.text", "text": REQUEST, "final": True})
        take(ws, 4)
        ws.send_json({"type": "utterance.text", "text": "yes", "final": True})
        take(ws, 4)

        ws.send_json({"type": "utterance.text", "text": "yes", "final": True})
        events = take(ws, 4)
        assert [e["type"] for e in events] == [
            "transcript.final",
            "error",
            "prompt.play",
            "listening",
        ]
        assert events[1]["code"] == "approval_required"
    assert bank_rows(client) == []


def test_approval_without_a_credential_is_rejected(client):
    token, consent_id = enrol(client)
    with client.websocket_connect("/ws") as ws:
        start_session(ws, token, consent_id)
        ws.send_json({"type": "utterance.text", "text": REQUEST, "final": True})
        take(ws, 4)
        ws.send_json({"type": "utterance.text", "text": "yes", "final": True})
        take(ws, 4)

        ws.send_json({"type": "approval.response"})
        events = take(ws, 3)
        assert [e["type"] for e in events] == [
            "state.changed",
            "prompt.play",
            "approval.request",
        ]
        assert events[0]["state"] == "AWAITING_BIOMETRIC"
    assert bank_rows(client) == []


def test_text_utterance_can_be_disabled(settings):
    app = create_app(settings.for_test(allow_text_utterance=False))
    with TestClient(app) as client:
        token, consent_id = enrol(client)
        with client.websocket_connect("/ws") as ws:
            start_session(ws, token, consent_id)
            ws.send_json({"type": "utterance.text", "text": REQUEST, "final": True})
            events = take(ws, 3)
            assert events[0]["type"] == "error"
            assert events[0]["code"] == "text_not_allowed"


def test_bad_sample_rate_is_rejected(client):
    token, consent_id = enrol(client)
    with client.websocket_connect("/ws") as ws:
        start_session(ws, token, consent_id)
        ws.send_json({"type": "utterance.start", "sample_rate": 1000})
        event = ws.receive_json()
        assert event["type"] == "error"
        assert event["code"] == "bad_sample_rate"


def test_emergency_lock_endpoint(client):
    token, _ = enrol(client)
    headers = {"Authorization": f"Bearer {token}"}
    assert client.get("/locks/status", headers=headers).json()["locked"] is False
    assert client.post("/locks", headers=headers).json()["locked"] is True
    assert client.get("/locks/status", headers=headers).json()["locked"] is True


def test_audit_endpoints_are_admin_gated_in_production(settings):
    app = create_app(settings.for_test(env="production", dev_tools=False))
    with TestClient(app) as client:
        assert client.get("/audit/verify").status_code == 403
