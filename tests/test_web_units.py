"""UNIT_TRANSPORT=web: unit calls are queued for /unit?u=N browsers instead of dialed."""
import asyncio
import json

import pytest
from fastapi.testclient import TestClient

from backend.config import BackendSettings
from main import create_app
from tests.test_router import fixture, make, wait_for
from voice.config import Settings
from voice.models import EmergencyType, Severity, UnitBrief

PHONE_1, PHONE_2 = "+15550000001", "+15550000002"


@pytest.fixture
def anyio_backend():
    return "asyncio"


def brief(phone=PHONE_2, unit_id="ambulance-1", name="Harbor Ambulance 1"):
    return UnitBrief(incident_id="INC-9", unit_id=unit_id, unit_name=name, phone=phone, callsign="UAL1234",
                     emergency_type=EmergencyType.MEDICAL, severity=Severity.MAYDAY, airport="KSAN",
                     eta_min=None, souls_on_board=140)


def web(tmp_path, **overrides):
    return make(tmp_path, dispatch="rec", sim_units=False, unit_transport="web", **overrides)


def status(call_id, state, **call_extra):
    return {"message": {"type": "status-update", "status": state,
                        "call": {"id": call_id, "type": "webCall", "assistantId": "unit-assistant-fake", **call_extra}}}


@pytest.mark.anyio
async def test_call_unit_queues_for_the_phone_slot_instead_of_dialing(tmp_path):
    svc, vapi, d, c = web(tmp_path)
    await svc.call_unit(brief(phone=PHONE_2))
    assert vapi.calls == []  # nothing dialed
    assert svc.web_pending(1) is None
    p = svc.web_pending(2)
    assert p["assistantId"] == "unit-assistant-fake" and p["unit_name"] == "Harbor Ambulance 1"
    v = p["variableValues"]
    assert v["callsign"] == "United 12 34" and v["airport"] == "San Diego" and v["souls"] == "140"
    assert v["dispatch_id"] == p["dispatch_id"]
    assert PHONE_2 not in json.dumps(p)
    kinds = [json.loads(l)["kind"] for l in (tmp_path / "events.jsonl").read_text().splitlines()]
    assert "unit_call_started" in kinds


@pytest.mark.anyio
async def test_bind_then_accept_tool_reports_to_dispatch(tmp_path):
    svc, vapi, d, c = web(tmp_path)
    await svc.call_unit(brief())
    did = svc.web_pending(2)["dispatch_id"]
    assert svc.web_bind(did, "web-call-1") == "ok"
    assert svc.web_pending(2) is None  # claimed
    r = await c.post("/vapi/webhook", json=fixture("unit_accept.json", UNIT_CALL_ID="web-call-1"))
    assert "Assignment confirmed" in r.json()["results"][0]["result"]
    assert await wait_for(lambda: d.unit_responses)
    assert d.unit_responses == [("INC-9", "ambulance-1", True, None)]


@pytest.mark.anyio
async def test_decline_tool_and_scripted_decline_first(tmp_path):
    svc, vapi, d, c = web(tmp_path, scripted_decline_first=True)
    await svc.call_unit(brief())
    p = svc.web_pending(2)
    assert "DEMO INSTRUCTION" in p["variableValues"]["demo_directive"]
    svc.web_bind(p["dispatch_id"], "web-call-1")
    r = await c.post("/vapi/webhook", json=fixture("unit_decline.json", UNIT_CALL_ID="web-call-1"))
    assert "unable" in r.json()["results"][0]["result"]
    assert await wait_for(lambda: d.unit_responses)
    assert d.unit_responses == [("INC-9", "ambulance-1", False, "committed to another call")]
    await svc.call_unit(brief(phone=PHONE_1, unit_id="ambulance-2"))
    assert svc.web_pending(1)["variableValues"]["demo_directive"] == ""  # only attempt 1 is scripted


@pytest.mark.anyio
async def test_timeout_before_anyone_picks_up_is_no_answer(tmp_path):
    svc, vapi, d, c = web(tmp_path, unit_call_timeout_s=0.1)
    await svc.call_unit(brief())
    did = svc.web_pending(2)["dispatch_id"]
    assert await wait_for(lambda: d.unit_responses)
    assert d.unit_responses == [("INC-9", "ambulance-1", False, "no_answer")]
    assert svc.web_pending(2) is None
    assert svc.web_bind(did, "late-call") == "expired"  # page will hang up


@pytest.mark.anyio
async def test_timeout_after_bind_ends_the_web_call(tmp_path):
    svc, vapi, d, c = web(tmp_path, unit_call_timeout_s=0.2)
    await svc.call_unit(brief())
    did = svc.web_pending(2)["dispatch_id"]
    await c.post("/vapi/webhook", json=status("web-call-1", "in-progress",
                                              monitor={"controlUrl": "https://ctl.invalid/web-call-1"}))
    svc.web_bind(did, "web-call-1")
    assert svc.registry.unit_calls["web-call-1"].control_url == "https://ctl.invalid/web-call-1"
    assert svc.registry.pilot_calls == {}  # early unit webhook must not create a pilot entry
    assert await wait_for(lambda: d.unit_responses)
    assert d.unit_responses[0][2:] == (False, "no_answer")
    assert await wait_for(lambda: vapi.ended == ["https://ctl.invalid/web-call-1"])


@pytest.mark.anyio
async def test_call_ended_without_decision_is_no_answer(tmp_path):
    svc, vapi, d, c = web(tmp_path)
    await svc.call_unit(brief())
    svc.web_bind(svc.web_pending(2)["dispatch_id"], "web-call-1")
    await c.post("/vapi/webhook", json=status("web-call-1", "ended", endedReason="customer-ended-call"))
    assert await wait_for(lambda: d.unit_responses)
    assert d.unit_responses == [("INC-9", "ambulance-1", False, "no_answer")]


@pytest.mark.anyio
async def test_webhook_binds_from_echoed_dispatch_id_if_bind_is_late(tmp_path):
    svc, vapi, d, c = web(tmp_path)
    await svc.call_unit(brief())
    did = svc.web_pending(2)["dispatch_id"]
    await c.post("/vapi/webhook", json=status(
        "web-call-1", "in-progress", assistantOverrides={"variableValues": {"dispatch_id": did}},
        monitor={"controlUrl": "https://ctl.invalid/web-call-1"}))
    uc = svc.registry.unit_calls["web-call-1"]
    assert uc.control_url == "https://ctl.invalid/web-call-1" and svc.registry.pilot_calls == {}
    assert svc.web_bind(did, "web-call-1") == "ok"  # the page's own bind is then a no-op
    assert svc.web_bind(did, "other-call") == "conflict"
    r = await c.post("/vapi/webhook", json=fixture("unit_accept.json", UNIT_CALL_ID="web-call-1"))
    assert "Assignment confirmed" in r.json()["results"][0]["result"]


@pytest.mark.anyio
async def test_unlisted_phone_is_still_blocked_in_web_mode(tmp_path):
    svc, vapi, d, c = web(tmp_path)
    await svc.call_unit(brief(phone="+19998887777"))
    assert svc.web_pending(1) is None and svc.web_pending(2) is None
    assert await wait_for(lambda: d.unit_responses)
    assert d.unit_responses[0][3] == "phone_not_allowlisted"


def test_unit_endpoints_on_integrated_app(tmp_path):
    from tests.test_integrated_app import FakeVapi

    settings = Settings(log_dir=tmp_path, sim_units=False, unit_transport="web", pilot_say_gap_s=0,
                        vapi_api_key="PRIVATE_VAPI_KEY_TEST_SENTINEL", vapi_public_key="PUBLIC_KEY_TEST",
                        unit_phones=[PHONE_1, PHONE_2])
    app = create_app(settings=settings, backend_settings=BackendSettings(aircraft_mode="mock", offline_voice=False),
                     vapi_client=FakeVapi())
    with TestClient(app) as client:
        voice = client.app.state.voice
        voice.unit_assistant_id = "unit-asst"
        assert client.get("/unit").status_code == 200
        assert "@vapi-ai/web@2.7.1" in client.get("/unit").text
        assert client.get("/unit/pending", params={"u": 2}).json()["dispatch"] is None
        client.portal.call(voice.call_unit, brief())
        body = client.get("/unit/pending", params={"u": 2}).json()
        raw = json.dumps(body)
        assert body["dispatch"]["assistantId"] == "unit-asst"
        assert "PRIVATE_VAPI_KEY_TEST_SENTINEL" not in raw and PHONE_2 not in raw
        did = body["dispatch"]["dispatch_id"]
        assert client.post("/unit/bind", json={"dispatch_id": did, "call_id": "web-1"}).status_code == 200
        assert client.post("/unit/bind", json={"dispatch_id": did, "call_id": "web-2"}).status_code == 409
        assert client.post("/unit/bind", json={"dispatch_id": "nope", "call_id": "web-3"}).status_code == 404
        assert client.get("/unit/pending", params={"u": 2}).json()["dispatch"] is None
        client.post("/dev/reset")
        assert voice.registry.web_dispatches == {}
