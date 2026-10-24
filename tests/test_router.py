import asyncio
import json
from pathlib import Path

import httpx
import pytest
from fastapi import FastAPI

from voice.config import Settings
from voice.models import EmergencyType, Severity, UnitBrief
from voice.ports import MockDispatch, VoiceService
from voice.router import build_router

FIXTURES = Path(__file__).parent / "fixtures"
PILOT = "fake-pilot-call-1"


@pytest.fixture
def anyio_backend():
    return "asyncio"


class FakeVapi:
    def __init__(self):
        self.said: list[tuple[str, str]] = []
        self.notes: list[tuple[str, str]] = []
        self.ended: list[str] = []
        self.calls: list[dict] = []
        self.say_ok = True

    async def say(self, url, text):
        self.said.append((url, text))
        return self.say_ok

    async def add_message(self, url, content, role="system", trigger_response=False):
        self.notes.append((url, content))
        return True

    async def end_call(self, url):
        self.ended.append(url)
        return True

    async def get_call(self, call_id):
        return {"id": call_id, "monitor": {}}

    async def create_call(self, assistant_id, phone_number_id, number, variables=None):
        cid = f"real-unit-{len(self.calls) + 1}"
        self.calls.append({"id": cid, "number": number, "variables": variables})
        return {"id": cid, "monitor": {"controlUrl": f"https://ctl.invalid/{cid}"}}


class RecordingDispatch:
    def __init__(self):
        self.reports, self.unit_responses, self.confirms, self.events = [], [], [], []

    async def create_incident(self, report):
        self.reports.append(report)
        return f"INC-{len(self.reports)}"

    async def on_unit_response(self, incident_id, unit_id, accepted, reason):
        self.unit_responses.append((incident_id, unit_id, accepted, reason))

    async def on_pilot_confirm(self, incident_id, readback):
        self.confirms.append((incident_id, readback))

    async def on_event(self, event):
        self.events.append(event)


def fixture(name, **replace):
    text = (FIXTURES / name).read_text()
    for k, v in replace.items():
        text = text.replace(k, v)
    return json.loads(text)


def make(tmp_path, dispatch="mock", **overrides):
    s = Settings(log_dir=tmp_path, sim_unit_delay_s=0.05, mock_dispatch_delay_s=0.05,
                 unit_phones=["+15550000001", "+15550000002"], vapi_phone_number_id="pn-1",
                 **{"pilot_say_gap_s": 0, **overrides})
    vapi = FakeVapi()
    svc = VoiceService(s, vapi, unit_assistant_id="unit-assistant-fake")
    d = MockDispatch(svc, s) if dispatch == "mock" else RecordingDispatch()
    svc.bind_dispatch(d)
    app = FastAPI()
    app.include_router(build_router(svc))
    client = httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test")
    return svc, vapi, d, client


async def wait_for(pred, timeout=3.0):
    for _ in range(int(timeout / 0.02)):
        if pred():
            return True
        await asyncio.sleep(0.02)
    return False


@pytest.mark.anyio
async def test_report_emergency_returns_fast_and_normalizes(tmp_path):
    svc, vapi, d, c = make(tmp_path, dispatch="rec")
    r = await c.post("/vapi/webhook", json=fixture("pilot_report_emergency.json"))
    assert r.status_code == 200
    assert r.json() == {"results": [{"toolCallId": "call_report_001", "result": "Copy, coordinating now."}]}
    assert await wait_for(lambda: d.reports)
    rep = d.reports[0]
    assert rep.callsign == "UAL1234" and rep.emergency_type == EmergencyType.FIRE_SMOKE
    assert rep.severity == Severity.MAYDAY and rep.souls_on_board == 140 and rep.call_id == PILOT
    assert await wait_for(lambda: svc.registry.pilot_calls[PILOT].incident_id == "INC-1")
    assert svc.registry.pilot_calls[PILOT].control_url.endswith("/dev/control/fake-pilot-call-1")


@pytest.mark.anyio
async def test_duplicate_tool_call_and_second_report_ignored(tmp_path):
    svc, vapi, d, c = make(tmp_path, dispatch="rec")
    body = fixture("pilot_report_emergency.json")
    await c.post("/vapi/webhook", json=body)
    r2 = await c.post("/vapi/webhook", json=body)  # same toolCallId
    assert r2.json()["results"][0]["result"] == "Copy, coordinating now."
    body2 = fixture("pilot_report_emergency.json", call_report_001="call_report_002")
    r3 = await c.post("/vapi/webhook", json=body2)  # new toolCallId, same call
    assert "Already reported" in r3.json()["results"][0]["result"]
    await asyncio.sleep(0.1)
    assert len(d.reports) == 1


@pytest.mark.anyio
async def test_string_arguments_and_tool_with_tool_call_list(tmp_path):
    svc, vapi, d, c = make(tmp_path, dispatch="rec")
    body = fixture("pilot_confirm_plan.json")
    del body["message"]["toolCallList"]  # only toolWithToolCallList, arguments as JSON string
    r = await c.post("/vapi/webhook", json=body)
    assert r.json()["results"][0]["toolCallId"] == "call_confirm_001"
    assert "No emergency on file" in r.json()["results"][0]["result"]


@pytest.mark.anyio
async def test_confirm_before_plan_is_rejected(tmp_path):
    svc, vapi, d, c = make(tmp_path, dispatch="rec")
    await c.post("/vapi/webhook", json=fixture("pilot_report_emergency.json"))
    assert await wait_for(lambda: svc.registry.pilot_calls[PILOT].incident_id)
    r = await c.post("/vapi/webhook", json=fixture("pilot_confirm_plan.json"))
    assert "No plan has been issued" in r.json()["results"][0]["result"]
    assert d.confirms == []


@pytest.mark.anyio
async def test_full_loop_sim_units_mock_dispatch(tmp_path):
    """Milestone 3: decline -> reassign -> accept -> plan spoken via say -> wilco -> confirm_plan."""
    svc, vapi, d, c = make(tmp_path, dispatch="mock", sim_units=True)
    await c.post("/vapi/webhook", json=fixture("pilot_status_in_progress.json"))
    await c.post("/vapi/webhook", json=fixture("pilot_report_emergency.json"))
    assert await wait_for(lambda: svc.registry.pilot_calls[PILOT].plan is not None)

    said = [t for _, t in vapi.said]
    assert said[0] == "Stand by, assigning another unit."
    assert "ARFF Rescue Twelve is responding" in said[1] and said[1].endswith("say wilco.")
    assert "United 12 34" in said[1]
    assert all(note.startswith("Dispatch update") for _, note in vapi.notes)
    units = sorted(svc.registry.unit_calls.values(), key=lambda u: u.attempt)
    assert [(u.unit_id, u.accepted, u.reason) for u in units] == [
        ("unit-1", False, "committed to another call"), ("unit-2", True, None)]

    r = await c.post("/vapi/webhook", json=fixture("pilot_confirm_plan.json"))
    assert r.json()["results"][0]["result"].startswith("Plan confirmed")
    iid = svc.registry.pilot_calls[PILOT].incident_id
    assert await wait_for(lambda: iid in d.confirmed)
    assert "Wilco" in d.confirmed[iid]
    kinds = [json.loads(l)["kind"] for l in (tmp_path / "events.jsonl").read_text().splitlines()]
    for k in ("report_received", "incident_created", "unit_declined", "unit_accepted", "pilot_briefed", "pilot_confirmed"):
        assert k in kinds


async def _real_unit_call(svc):
    await svc.call_unit(UnitBrief(
        incident_id="INC-9", unit_id="unit-1", unit_name="Engine 7", phone="+15550000001", callsign="UAL1234",
        emergency_type=EmergencyType.ENGINE, severity=Severity.MAYDAY, airport="SAN", eta_min=10, souls_on_board=None))


@pytest.mark.anyio
async def test_real_unit_accept_and_decline_tools(tmp_path):
    svc, vapi, d, c = make(tmp_path, dispatch="rec", sim_units=False)
    await _real_unit_call(svc)
    assert vapi.calls[0]["variables"]["callsign"] == "United 12 34"
    assert vapi.calls[0]["variables"]["souls"] == "an unknown number of"
    r = await c.post("/vapi/webhook", json=fixture("unit_accept.json", UNIT_CALL_ID="real-unit-1"))
    assert "Assignment confirmed" in r.json()["results"][0]["result"]
    await _real_unit_call(svc)
    r = await c.post("/vapi/webhook", json=fixture("unit_decline.json", UNIT_CALL_ID="real-unit-2"))
    assert "unable" in r.json()["results"][0]["result"]
    assert await wait_for(lambda: len(d.unit_responses) == 2)
    assert d.unit_responses == [("INC-9", "unit-1", True, None), ("INC-9", "unit-1", False, "committed to another call")]


@pytest.mark.anyio
async def test_unit_hangup_without_decision_is_no_answer(tmp_path):
    svc, vapi, d, c = make(tmp_path, dispatch="rec", sim_units=False)
    await _real_unit_call(svc)
    await c.post("/vapi/webhook", json=fixture("unit_status_ended.json", UNIT_CALL_ID="real-unit-1"))
    assert await wait_for(lambda: d.unit_responses)
    assert d.unit_responses == [("INC-9", "unit-1", False, "no_answer")]
    # a late accept after the call ended doesn't flip the decision
    await c.post("/vapi/webhook", json=fixture("unit_accept.json", UNIT_CALL_ID="real-unit-1"))
    await asyncio.sleep(0.05)
    assert len(d.unit_responses) == 1


@pytest.mark.anyio
async def test_unit_timeout(tmp_path):
    svc, vapi, d, c = make(tmp_path, dispatch="rec", sim_units=False, unit_call_timeout_s=0.1)
    await _real_unit_call(svc)
    assert await wait_for(lambda: d.unit_responses)
    assert d.unit_responses == [("INC-9", "unit-1", False, "no_answer")]
    assert vapi.ended == ["https://ctl.invalid/real-unit-1"]


@pytest.mark.anyio
async def test_scripted_decline_first(tmp_path):
    svc, vapi, d, c = make(tmp_path, dispatch="rec", sim_units=False, scripted_decline_first=True)
    await _real_unit_call(svc)
    await _real_unit_call(svc)
    assert "DEMO INSTRUCTION" in vapi.calls[0]["variables"]["demo_directive"]
    assert vapi.calls[1]["variables"]["demo_directive"] == ""


@pytest.mark.anyio
async def test_never_dials_unlisted_number(tmp_path):
    svc, vapi, d, c = make(tmp_path, dispatch="rec", sim_units=False)
    await svc.call_unit(UnitBrief(
        incident_id="INC-9", unit_id="u", unit_name="X", phone="+19998887777", callsign="UAL1",
        emergency_type=EmergencyType.OTHER, severity=Severity.PAN, airport="SAN", eta_min=None, souls_on_board=None))
    assert vapi.calls == []
    assert await wait_for(lambda: d.unit_responses)
    assert d.unit_responses[0][3] == "phone_not_allowlisted"


@pytest.mark.anyio
async def test_max_outbound_calls_escalates(tmp_path):
    svc, vapi, d, c = make(tmp_path, dispatch="rec", sim_units=False, max_outbound_calls=2)
    for _ in range(3):
        await _real_unit_call(svc)
    assert len(vapi.calls) == 2
    await asyncio.sleep(0.05)
    assert any(e["kind"] == "escalate_human" for e in d.events)


@pytest.mark.anyio
async def test_say_failure_emits_event(tmp_path):
    svc, vapi, d, c = make(tmp_path, dispatch="rec")
    vapi.say_ok = False
    await c.post("/vapi/webhook", json=fixture("pilot_report_emergency.json"))
    assert await wait_for(lambda: svc.registry.pilot_calls[PILOT].incident_id)
    await svc.brief_pilot("INC-1", "Plan.")
    await asyncio.sleep(0.05)
    assert any(e["kind"] == "pilot_control_failed" and e["reason"] == "say_failed" for e in d.events)


@pytest.mark.anyio
async def test_webhook_secret(tmp_path):
    svc, vapi, d, c = make(tmp_path, dispatch="rec", vapi_webhook_secret="s3cret")
    body = fixture("pilot_status_in_progress.json")
    assert (await c.post("/vapi/webhook", json=body)).status_code == 401
    assert (await c.post("/vapi/webhook", json=body, headers={"X-Vapi-Secret": "s3cret"})).status_code == 200


@pytest.mark.anyio
async def test_final_transcripts_forwarded(tmp_path):
    svc, vapi, d, c = make(tmp_path, dispatch="rec")
    await c.post("/vapi/webhook", json=fixture("pilot_transcript_final.json"))
    partial = fixture("pilot_transcript_final.json")
    partial["message"].update(type="transcript", transcriptType="partial")
    await c.post("/vapi/webhook", json=partial)
    await asyncio.sleep(0.05)
    tx = [e for e in d.events if e["kind"] == "transcript"]
    assert len(tx) == 1 and tx[0]["leg"] == "pilot" and "United twelve" in tx[0]["text"]


class FailingDispatch(RecordingDispatch):
    async def create_incident(self, report):
        from backend.dispatch import DispatchError
        raise DispatchError(409, "This callsign already has an active incident on another pilot call")


@pytest.mark.anyio
async def test_create_incident_failure_is_spoken_to_pilot(tmp_path):
    svc, vapi, d, c = make(tmp_path, dispatch="rec")
    svc.bind_dispatch(FailingDispatch())
    r = await c.post("/vapi/webhook", json=fixture("pilot_report_emergency.json"))
    assert r.json()["results"][0]["result"] == "Copy, coordinating now."
    assert await wait_for(lambda: vapi.said)
    url, text = vapi.said[0]
    assert url.endswith("/dev/control/fake-pilot-call-1")
    assert text.startswith("Dispatch could not open this incident. A human coordinator will take over.")
    assert "Do not call report_emergency again" in vapi.notes[0][1]
    events = [json.loads(l) for l in (tmp_path / "events.jsonl").read_text().splitlines()]
    err = next(e for e in events if e["kind"] == "dispatch_error")
    assert "already has an active incident" in err["error"]
    assert any(e["kind"] == "escalate_human" and e["pilot_notified"] for e in events)
    assert svc.registry.pilot_calls["fake-pilot-call-1"].reporting is False


@pytest.mark.anyio
async def test_pilot_says_are_serialized_with_a_gap(tmp_path):
    svc, vapi, d, c = make(tmp_path, dispatch="rec", pilot_say_gap_s=0.2)
    svc.SECONDS_PER_WORD = 0.05
    stamps = []
    orig = vapi.say

    async def timed_say(url, text):
        stamps.append((asyncio.get_running_loop().time(), text))
        return await orig(url, text)

    vapi.say = timed_say
    await c.post("/vapi/webhook", json=fixture("pilot_report_emergency.json"))
    assert await wait_for(lambda: svc.registry.pilot_calls[PILOT].incident_id)
    await asyncio.gather(svc.update_pilot("INC-1", "one two three four"), svc.brief_pilot("INC-1", "plan"))
    assert [t for _, t in stamps] == ["one two three four", "plan Read back the plan, or say wilco."]
    # second message waits for ~4 words * 0.05s + 0.2s gap after the first
    assert stamps[1][0] - stamps[0][0] >= 0.39
