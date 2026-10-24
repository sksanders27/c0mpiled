"""Real dispatch lifecycle tests with the repository's simulated VoiceService."""

import asyncio
import time
from datetime import datetime, timezone

import pytest
from fastapi.testclient import TestClient

from backend.config import BackendSettings
from backend.dispatch import DispatchError
from main import create_app
from voice.config import Settings
from voice.models import EmergencyReport


REPORT = {
    "call_id": "rest-pilot-1",
    "callsign_raw": "United twelve thirty-four",
    "callsign": "UAL1234",
    "emergency_type": "FIRE_SMOKE",
    "severity": "MAYDAY",
    "souls_on_board": 140,
    "fuel_minutes": 30,
    "intentions": "Divert to San Diego",
    "summary": "Training scenario: cabin smoke.",
}


class RecordingVapi:
    def __init__(self):
        self.said = []
        self.notes = []
        self.say_ok = True

    async def say(self, url, text):
        self.said.append((url, text))
        return self.say_ok

    async def add_message(self, url, content, **kwargs):
        self.notes.append((url, content))
        return True

    async def get_call(self, call_id):
        return {"id": call_id, "monitor": {"controlUrl": f"https://control.invalid/{call_id}"}}

    async def end_call(self, url):
        return True


def make_app(tmp_path, *, delay=0.01, max_calls=4, **backend_overrides):
    settings = Settings(
        log_dir=tmp_path,
        sim_units=True,
        sim_unit_delay_s=delay,
        unit_call_timeout_s=45,
        max_outbound_calls=max_calls,
    )
    backend = BackendSettings(aircraft_mode="mock", offline_voice=True, **backend_overrides)
    return create_app(settings=settings, backend_settings=backend, vapi_client=RecordingVapi())


def report(client, **overrides):
    response = client.post("/api/emergencies", json={**REPORT, **overrides})
    assert response.status_code == 202, response.text
    return response.json()


def wait_incident(client, incident_id, predicate, timeout=3):
    deadline = time.monotonic() + timeout
    last = None
    while time.monotonic() < deadline:
        response = client.get(f"/api/incidents/{incident_id}")
        assert response.status_code == 200, response.text
        last = response.json()
        if predicate(last):
            return last
        time.sleep(0.01)
    pytest.fail(f"Incident did not reach expected state: {last}")


def pending(incident):
    return [assignment for assignment in incident["assignments"] if assignment["status"] == "PENDING"]


def test_rest_intake_dispatches_every_required_capability_and_confirm_releases(tmp_path):
    with TestClient(make_app(tmp_path)) as client:
        incident = report(client)
        briefed = wait_incident(client, incident["id"], lambda item: item["status"] == "PILOT_BRIEFED")
        attempts = briefed["assignments"]
        assert any(item["status"] == "DECLINED" for item in attempts)
        accepted = [item for item in attempts if item["status"] == "ACCEPTED"]
        assert {item["unit_type"] for item in accepted} == {"FIRE", "AMBULANCE"}
        assert len({item["unit_id"] for item in accepted}) == len(accepted)
        assert briefed["aircraft"] and briefed["nearby_airports"]
        assert client.app.state.voice.registry.pilot_for_incident(incident["id"]) is not None

        client.portal.call(client.app.state.dispatch.on_pilot_confirm, incident["id"], "Wilco")
        confirmed = client.get(f"/api/incidents/{incident['id']}").json()
        assert confirmed["status"] == "CONFIRMED"
        units = client.get("/api/units").json()
        assigned_ids = {item["unit_id"] for item in accepted}
        assert all(
            unit["status"] == "AVAILABLE" and unit["incident_id"] is None
            for unit in units if unit["id"] in assigned_ids
        )


def test_voice_call_budget_escalates_and_releases_pending_reservations(tmp_path):
    with TestClient(make_app(tmp_path, max_calls=1)) as client:
        incident = report(client)
        escalated = wait_incident(client, incident["id"], lambda item: item["status"] == "ESCALATED")
        assert client.app.state.voice.registry.outbound_count[incident["id"]] == 1
        assert not pending(escalated)
        assert all(
            unit["status"] == "AVAILABLE" and unit["incident_id"] is None
            for unit in client.get("/api/units").json()
        )
        time.sleep(0.05)
        assert client.get(f"/api/incidents/{incident['id']}").json()["status"] == "ESCALATED"


def test_premature_backend_confirmation_rejects_without_releasing_resources(tmp_path):
    with TestClient(make_app(tmp_path, delay=30)) as client:
        incident = report(client)
        wait_incident(client, incident["id"], lambda item: bool(pending(item)))
        with pytest.raises(DispatchError) as rejected:
            client.portal.call(client.app.state.dispatch.on_pilot_confirm, incident["id"], "Wilco")
        assert rejected.value.status_code == 409
        current = client.get(f"/api/incidents/{incident['id']}").json()
        assert current["status"] == "UNIT_PENDING"
        assert current["pilot_confirmed"] is False
        assert pending(current)


def test_same_pilot_call_is_idempotent_and_active_callsign_rejects_another_call(tmp_path):
    with TestClient(make_app(tmp_path, delay=30)) as client:
        incident = report(client)
        duplicate = report(client)
        assert duplicate["id"] == incident["id"]
        conflict = client.post("/api/emergencies", json={**REPORT, "call_id": "another-pilot"})
        assert conflict.status_code == 409
        assert len(client.get("/api/incidents").json()) == 1


def test_stale_unit_acceptance_cannot_replace_current_assignment(tmp_path):
    with TestClient(make_app(tmp_path, delay=30)) as client:
        incident = report(client, emergency_type="MEDICAL")
        first_state = wait_incident(client, incident["id"], lambda item: bool(pending(item)))
        first = pending(first_state)[0]
        dispatch = client.app.state.dispatch
        client.portal.call(dispatch.on_unit_response, incident["id"], first["unit_id"], False, "Busy")
        reassigned = wait_incident(
            client,
            incident["id"],
            lambda item: any(attempt["unit_id"] != first["unit_id"] for attempt in pending(item)),
        )
        second = next(item for item in pending(reassigned) if item["unit_id"] != first["unit_id"])
        client.portal.call(dispatch.on_unit_response, incident["id"], first["unit_id"], True, None)
        current = client.get(f"/api/incidents/{incident['id']}").json()
        assert next(item for item in current["assignments"] if item["id"] == first["id"])["status"] == "DECLINED"
        assert any(item["id"] == second["id"] for item in pending(current))
        assert not any(item["status"] == "ACCEPTED" for item in current["assignments"])


def test_simultaneous_incidents_reserve_distinct_units(tmp_path):
    with TestClient(make_app(tmp_path, delay=30)) as client:
        first = report(client, emergency_type="MEDICAL")
        second = report(
            client,
            call_id="rest-pilot-2",
            callsign_raw="Delta forty-two",
            callsign="DAL42",
            emergency_type="MEDICAL",
        )
        first_state = wait_incident(client, first["id"], lambda item: bool(pending(item)))
        second_state = wait_incident(client, second["id"], lambda item: bool(pending(item)))
        first_units = {item["unit_id"] for item in pending(first_state)}
        second_units = {item["unit_id"] for item in pending(second_state)}
        assert first_units and second_units
        assert first_units.isdisjoint(second_units)
        units = client.get("/api/units").json()
        assert all(unit["status"] == "RESERVED" for unit in units if unit["id"] in first_units | second_units)


def test_unknown_callsign_requests_clarification_without_fake_aircraft(tmp_path):
    with TestClient(make_app(tmp_path)) as client:
        incident = report(client, callsign=None, callsign_raw="unknown aircraft near the coast")
        clarified = wait_incident(
            client, incident["id"], lambda item: item["status"] == "NEEDS_CLARIFICATION"
        )
        assert clarified["aircraft"] is None
        assert clarified["assignments"] == []
        assert all(unit["status"] == "AVAILABLE" for unit in client.get("/api/units").json())


def test_out_of_area_provider_does_not_assign_local_responders(tmp_path):
    async def provider(callsign):
        return {
            "aircraft": {
                "callsign": callsign,
                "latitude": 40.65,
                "longitude": -73.80,
                "observed_at": datetime.now(timezone.utc).isoformat(),
                "position_age_seconds": 0,
                "is_mock": False,
                "source": "test-provider",
            },
            "nearby_airports": [{
                "icao": "KJFK",
                "name": "John F. Kennedy International Airport",
                "latitude": 40.6398,
                "longitude": -73.7789,
            }],
        }

    app = create_app(
        settings=Settings(log_dir=tmp_path, sim_units=True, sim_unit_delay_s=0.01),
        backend_settings=BackendSettings(aircraft_mode="live", allow_aircraft_fallback=False, offline_voice=True),
        aircraft_provider=provider,
        vapi_client=RecordingVapi(),
    )
    with TestClient(app) as client:
        incident = report(client)
        paused = wait_incident(client, incident["id"], lambda item: item["status"] == "NEEDS_CLARIFICATION")
        assert paused["aircraft"]["is_mock"] is False
        assert paused["selected_airport"]["icao"] == "KJFK"
        assert paused["assignments"] == []
        assert client.app.state.voice.registry.outbound_count.get(incident["id"], 0) == 0
        assert all(unit["status"] == "AVAILABLE" for unit in client.get("/api/units").json())


def test_cancellation_before_lookup_task_starts_cannot_resurrect_incident(tmp_path):
    with TestClient(make_app(tmp_path)) as client:
        dispatch = client.app.state.dispatch

        async def create_and_cancel_before_yielding():
            emergency = EmergencyReport.model_validate({
                **REPORT,
                "callsign": None,
                "callsign_raw": "unknown aircraft near the coast",
            })
            incident_id = await dispatch.create_incident(emergency)
            await dispatch.end(incident_id)
            await asyncio.sleep(0.02)
            return await dispatch.incident(incident_id)

        cancelled = client.portal.call(create_and_cancel_before_yielding)
        assert cancelled["status"] == "CANCELLED"
        assert cancelled["assignments"] == []
