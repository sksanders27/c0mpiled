"""Exercise the real dispatch backend through unchanged Vapi webhook fixtures."""

import concurrent.futures
import contextlib
import json
import time
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from starlette.websockets import WebSocketDisconnect

from backend.config import BackendSettings
from main import create_app
from voice.config import Settings
from voice.ports import MockDispatch


FIXTURES = Path(__file__).parent / "fixtures"
if not FIXTURES.exists():
    FIXTURES = Path.cwd() / "tests" / "fixtures"


class FakeVapi:
    def __init__(self, *, say_ok=True):
        self.say_ok = say_ok
        self.said = []
        self.notes = []

    async def say(self, url, text):
        self.said.append((url, text))
        return self.say_ok

    async def add_message(self, url, content, **kwargs):
        self.notes.append((url, content))
        return True

    async def get_call(self, call_id):
        return {"id": call_id, "monitor": {"controlUrl": f"https://ctl.invalid/{call_id}"}}

    async def end_call(self, url):
        return True


def fixture(name):
    return json.loads((FIXTURES / name).read_text())


def make_app(tmp_path, *, vapi=None, delay=0.01, api_token=None, webhook_secret=""):
    settings = Settings(
        log_dir=tmp_path,
        sim_units=True,
        sim_unit_delay_s=delay,
        max_outbound_calls=4,
        vapi_webhook_secret=webhook_secret,
        vapi_api_key="PRIVATE_VAPI_KEY_TEST_SENTINEL",
        unit_phones=["+15551234567", "+15557654321"],
    )
    return create_app(
        settings=settings,
        backend_settings=BackendSettings(aircraft_mode="mock", offline_voice=True, api_token=api_token),
        vapi_client=vapi or FakeVapi(),
    )


def wait_for(predicate, timeout=3):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        value = predicate()
        if value:
            return value
        time.sleep(0.01)
    pytest.fail("Integrated voice/dispatch condition did not complete")


def get_only_incident(client):
    response = client.get("/api/incidents")
    assert response.status_code == 200
    incidents = response.json()
    return incidents[0] if incidents else None


def test_vapi_fixture_runs_real_dispatch_decline_reassign_brief_confirm(tmp_path):
    vapi = FakeVapi()
    with TestClient(make_app(tmp_path, vapi=vapi)) as client:
        assert not isinstance(client.app.state.dispatch, MockDispatch)
        body = fixture("pilot_report_emergency.json")
        result = client.post("/vapi/webhook", json=body)
        assert result.status_code == 200
        assert result.json() == {
            "results": [{"toolCallId": "call_report_001", "result": "Copy, coordinating now."}]
        }

        def ready():
            incident = get_only_incident(client)
            return incident if incident and incident["status"] == "PILOT_BRIEFED" else None

        incident = wait_for(ready)
        attempts = incident["assignments"]
        assert any(item["status"] == "DECLINED" for item in attempts)
        assert {item["unit_type"] for item in attempts if item["status"] == "ACCEPTED"} == {"FIRE", "AMBULANCE"}
        assert any("wilco" in text.lower() for _, text in vapi.said)

        duplicate = client.post("/vapi/webhook", json=body)
        assert duplicate.json() == result.json()
        assert len(client.get("/api/incidents").json()) == 1
        confirm = client.post("/vapi/webhook", json=fixture("pilot_confirm_plan.json"))
        assert confirm.status_code == 200
        assert confirm.json()["results"][0]["toolCallId"] == "call_confirm_001"
        assert confirm.json()["results"][0]["result"].startswith("Plan confirmed")
        confirmed = wait_for(
            lambda: (item if (item := get_only_incident(client))["status"] == "CONFIRMED" else None)
        )
        assert confirmed["id"] == incident["id"]
        assert all(unit["status"] == "AVAILABLE" for unit in client.get("/api/units").json())


def test_premature_voice_confirmation_cannot_close_incident(tmp_path):
    with TestClient(make_app(tmp_path, delay=30)) as client:
        client.post("/vapi/webhook", json=fixture("pilot_report_emergency.json"))
        wait_for(lambda: get_only_incident(client))
        confirm = client.post("/vapi/webhook", json=fixture("pilot_confirm_plan.json"))
        assert confirm.status_code == 200
        assert "Plan confirmed" not in confirm.json()["results"][0]["result"]
        assert get_only_incident(client)["status"] != "CONFIRMED"


def test_failed_pilot_playback_cannot_be_confirmed(tmp_path):
    vapi = FakeVapi(say_ok=False)
    with TestClient(make_app(tmp_path, vapi=vapi)) as client:
        client.post("/vapi/webhook", json=fixture("pilot_report_emergency.json"))

        def attempted_briefing():
            incident = get_only_incident(client)
            return incident if incident and incident["status"] == "UNIT_ACCEPTED" and len(vapi.said) >= 2 else None

        incident = wait_for(attempted_briefing)
        confirm = client.post("/vapi/webhook", json=fixture("pilot_confirm_plan.json"))
        assert confirm.status_code == 200
        assert "Plan confirmed" not in confirm.json()["results"][0]["result"]
        assert client.get(f"/api/incidents/{incident['id']}").json()["status"] == "UNIT_ACCEPTED"
        assert client.app.state.voice.registry.pilot_calls["fake-pilot-call-1"].confirmed is False


def test_dashboard_snapshot_and_events_exclude_private_voice_configuration(tmp_path):
    body = fixture("pilot_report_emergency.json")
    body["message"]["call"]["monitor"]["controlUrl"] = "https://private.invalid/SECRET_CONTROL_TOKEN"
    with TestClient(make_app(tmp_path)) as client:
        client.post("/vapi/webhook", json=body)
        wait_for(lambda: get_only_incident(client))
        snapshot = client.get("/api/snapshot")
        assert snapshot.status_code == 200
        data = snapshot.json()
        assert {"incidents", "units", "events", "airports"} <= data.keys()
        serialized = json.dumps(data)
        for secret in ("SECRET_CONTROL_TOKEN", "controlUrl", "control_url", "+15551234567", "+15557654321", "PRIVATE_VAPI_KEY_TEST_SENTINEL"):
            assert secret not in serialized
        assert '"phone"' not in serialized

        events = client.get("/api/events").json()
        assert events
        sequences = [item["sequence"] for item in events]
        assert sequences == sorted(set(sequences))
        tail = client.get("/api/events", params={"after": sequences[-1]}).json()
        assert all(item["sequence"] > sequences[-1] for item in tail)


def test_dashboard_api_auth_and_websocket_first_message(tmp_path):
    with TestClient(make_app(tmp_path, api_token="dashboard-secret")) as client:
        assert client.get("/health").status_code == 200
        assert client.get("/api/snapshot").status_code == 401
        authorized = client.get("/api/snapshot", headers={"Authorization": "Bearer dashboard-secret"})
        assert authorized.status_code == 200
        # Starlette>=1.x TestClient cancels the server task on exit and can surface CancelledError
        # if the handler hasn't returned yet; the assertions below run before that point.
        exit_race = contextlib.suppress(concurrent.futures.CancelledError)
        with exit_race, client.websocket_connect("/ws/events") as websocket:
            websocket.send_json({"token": "dashboard-secret"})
            snapshot = websocket.receive_json()
            assert snapshot["type"] == "snapshot"
            assert snapshot["data"]["incidents"] == []
        denied = None
        with contextlib.suppress(concurrent.futures.CancelledError), client.websocket_connect("/ws/events") as websocket:
            websocket.send_json({"token": "incorrect"})
            try:
                websocket.receive_json()
            except WebSocketDisconnect as error:
                denied = error
        assert denied is not None and denied.code == 1008


def test_integrated_vapi_webhook_retains_separate_secret_auth(tmp_path):
    with TestClient(make_app(tmp_path, api_token="dashboard-secret", webhook_secret="vapi-secret")) as client:
        body = fixture("pilot_report_emergency.json")
        assert client.post("/vapi/webhook", json=body).status_code == 401
        accepted = client.post("/vapi/webhook", json=body, headers={"X-Vapi-Secret": "vapi-secret"})
        assert accepted.status_code == 200
        assert accepted.json()["results"][0]["result"] == "Copy, coordinating now."


def _report(call_id, tool_call_id):
    body = fixture("pilot_report_emergency.json")
    body["message"]["call"]["id"] = call_id
    body["message"]["call"]["monitor"]["controlUrl"] = f"https://ctl.invalid/{call_id}"
    body["message"]["toolCallList"][0]["id"] = tool_call_id
    return body


def test_duplicate_callsign_tells_pilot_and_dev_reset_clears_it(tmp_path):
    """Rehearsal re-run: same callsign on a new call is a 409; the pilot must hear it, and reset fixes it."""
    vapi = FakeVapi()
    with TestClient(make_app(tmp_path, vapi=vapi)) as client:
        client.post("/vapi/webhook", json=_report("call-a", "tc-a"))
        wait_for(lambda: get_only_incident(client))
        client.post("/vapi/webhook", json=_report("call-b", "tc-b"))
        wait_for(lambda: any("could not open this incident" in text for _, text in vapi.said))
        assert any(url.endswith("/call-b") for url, text in vapi.said if "could not open" in text)

        reset = client.post("/dev/reset")
        assert reset.status_code == 200
        snapshot = client.get("/api/snapshot").json()
        assert snapshot["incidents"] == []
        assert [e["kind"] for e in snapshot["events"]] == ["system.reset"]
        assert all(u["status"] == "AVAILABLE" for u in snapshot["units"])
        assert client.app.state.voice.registry.pilot_calls == {}

        client.post("/vapi/webhook", json=_report("call-c", "tc-c"))
        incident = wait_for(lambda: get_only_incident(client))
        assert incident["report"]["call_id"] == "call-c"


def test_dev_reset_requires_token_when_configured(tmp_path):
    with TestClient(make_app(tmp_path, api_token="dashboard-secret")) as client:
        assert client.post("/dev/reset").status_code == 401
        ok = client.post("/dev/reset", headers={"Authorization": "Bearer dashboard-secret"})
        assert ok.status_code == 200


def test_startup_banner_masks_phones_and_warns_on_offline_with_key(tmp_path, caplog):
    caplog.set_level("INFO", logger="skywatch")
    with TestClient(make_app(tmp_path)):
        pass
    text = caplog.text
    assert "OFFLINE_VOICE=1 SIM_UNITS=1 AIRCRAFT_MODE=mock" in text
    assert "ambulance-1=***4321" in text and "fire-1=***4567" in text
    assert "+15551234567" not in text
    assert "a REAL pilot call will hear NO dispatch updates" in text


def test_aircraft_requests_send_a_non_default_user_agent(tmp_path):
    """adsb.lol rejects httpx's default User-Agent with 403."""
    import httpx

    seen = []

    def handler(request):
        seen.append(request.headers.get("user-agent", ""))
        return httpx.Response(200, json={"ac": []})

    settings = Settings(log_dir=tmp_path, sim_units=True, unit_phones=["+15551234567"])
    app = create_app(settings=settings,
                     backend_settings=BackendSettings(aircraft_mode="live", offline_voice=True),
                     vapi_client=FakeVapi(), aircraft_transport=httpx.MockTransport(handler))
    with TestClient(app) as client:
        client.get("/api/aircraft/UAL1234")
    assert seen and all(ua.startswith("SkyWatch/") for ua in seen)


def test_pilot_page_and_config_expose_only_public_key(tmp_path, monkeypatch):
    import main as main_module

    monkeypatch.setattr(main_module, "load_vapi_ids", lambda: {"pilot_assistant_id": "pilot-asst-1"})
    app = make_app(tmp_path)
    with TestClient(app) as client:
        assert client.get("/pilot").status_code == 200
        assert "@vapi-ai/web@2.7.1" in client.get("/pilot").text
        assert client.get("/pilot/config").status_code == 503  # no public key configured
        client.app.state.voice.settings.vapi_public_key = "PUBLIC_KEY_TEST"
        cfg = client.get("/pilot/config").json()
        assert cfg == {"publicKey": "PUBLIC_KEY_TEST", "assistantId": "pilot-asst-1"}
        assert "PRIVATE_VAPI_KEY_TEST_SENTINEL" not in json.dumps(cfg)
