# Backend integration guide

## Ownership and application wiring

`main:app` runs the real backend with the existing voice package. `dev_main:app` retains the standalone Voice + `MockDispatch` rehearsal.

```text
Vapi / synthetic pilot webhook
    -> voice.router and voice.tools
    -> DispatchEngine.create_incident(shared EmergencyReport)
    -> AircraftService: aircraft and nearby airports
    -> capability selection, reservation, reassignment
    -> VoiceService.call_unit(shared UnitBrief)
    -> DispatchEngine.on_unit_response(...)
    -> VoiceService.brief_pilot(...)
    -> delivered briefing event
    -> pilot confirm_plan
    -> CONFIRMED, incident closed, responders released
```

Voice owns the conversations, calls, unit simulation, primary unit-call timeouts, and outbound-call cap. Backend owns aircraft lookup, airport/dispatcher selection, incident state, capability matching, reservations, reassignment, and the observer event stream. Its delayed watchdog handles missing Voice callbacks. Network calls and speech run outside the state lock.

The shared models in `voice/models.py` remain unchanged:

```python
EmergencyReport(
    call_id, callsign_raw, callsign, emergency_type, severity,
    souls_on_board=None, fuel_minutes=None, intentions=None, summary=...
)
UnitBrief(
    incident_id, unit_id, unit_name, phone, callsign, emergency_type,
    severity, airport, eta_min, souls_on_board
)
```

`callsign` may be `None` when Voice cannot identify it confidently. Backend reuses `voice.callsign.normalize_callsign`; an unresolved identity enters `NEEDS_CLARIFICATION` without guessing an aircraft. Supported emergency types are `MEDICAL`, `FIRE_SMOKE`, `ENGINE`, `FUEL`, `DEPRESSURIZATION`, `SECURITY`, `GEAR_STRUCTURAL`, and `OTHER`. Severity is `MAYDAY` or `PAN`.

Backend implements these async `DispatchPort` methods:

```python
create_incident(report: EmergencyReport) -> str
on_unit_response(incident_id, unit_id, accepted: bool, reason: str | None) -> None
on_pilot_confirm(incident_id, readback: str) -> None
on_event(event: dict) -> None
```

Backend calls `VoiceService.call_unit(brief)`, `brief_pilot(incident_id, text)`, and `update_pilot(incident_id, text)`. A successful Python return from `brief_pilot` alone does not establish delivery: Backend waits for the matching `pilot_briefed` event with `delivered=true`.

## Configuration and safe rehearsal

From the project root, create a venv and install `requirements.txt` as shown in the README. For a complete offline run:

```powershell
$env:OFFLINE_VOICE = "1"
$env:SIM_UNITS = "1"
$env:MOCK_DISPATCH = "0"
$env:AIRCRAFT_MODE = "mock"
.\.venv\Scripts\python.exe -m uvicorn main:app --workers 1 --port 8000
```

| Setting | Default | Meaning |
|---|---|---|
| `OFFLINE_VOICE` | `1` | Use the local no-network Vapi substitute; requires `SIM_UNITS=1` |
| `SIM_UNITS` | `1` | Voice simulates the first decline and later acceptances |
| `MOCK_DISPATCH` | `0` | Must be false for `main:app`; true is for `dev_main:app` |
| `AIRCRAFT_MODE` | `mock` | `mock` uses sample positions; `live` activates ADSB/provider lookup |
| `ALLOW_AIRCRAFT_FALLBACK` | `1` | On unusable live data, return explicitly labeled sample data |
| `MAX_ATTEMPTS_PER_TYPE` | `3` | Backend attempt limit for each required capability |
| `MAX_TOTAL_ATTEMPTS` | `4` | Backend incident-wide attempt limit |
| `MAX_OUTBOUND_CALLS` | `4` | Voice incident-wide cap; effective total is the lower of this and Backend's limit |
| `UNIT_CALL_TIMEOUT_S` | `45` | Voice's unit-call decision deadline |
| `ASSIGNMENT_TIMEOUT_SECONDS` | unset | Optional Backend watchdog override; otherwise Voice timeout + 1 second |
| `SIM_UNIT_DELAY_S` | `4` | Delay before a simulated unit decides |
| `API_TOKEN` | unset | Optional REST/WebSocket observer token |
| `VAPI_WEBHOOK_SECRET` | unset | Independent webhook secret |
| `CORS_ORIGINS` | local dev origins | Comma-separated permitted browser origins; also checked on WebSockets |

For real pilot audio, set `OFFLINE_VOICE=0`, provide `VAPI_API_KEY`, and use the existing assistant IDs, public URL, and Vapi webhook configuration. Retain `SIM_UNITS=1` for the team's free-number setup. The project status document reports that number cannot place outbound calls. `CLAUDE.md` requires explicit approval before real outbound unit calls; this integration's demo script performs none.

State is in memory. Use one worker, avoid reloads during a demonstration, and expect a restart to clear incidents, reservations, and call mappings. Logs may contain raw Voice input; do not serve the logs directory or a raw registry as dashboard data.

## Incident lifecycle and resources

```text
REPORTED -> LOCATED -> DISPATCHER_ASSIGNED -> UNIT_PENDING
    -> decline/timeout -> another UNIT_PENDING
    -> all required capabilities accepted -> UNIT_ACCEPTED
    -> delivered final briefing -> PILOT_BRIEFED
    -> pilot readback -> CONFIRMED
```

Assignment records independently retain `PENDING`, `ACCEPTED`, `DECLINED`, `TIMED_OUT`, or `CANCELLED`.

| Emergency | Required simulated units |
|---|---|
| `MEDICAL`, `DEPRESSURIZATION` | Ambulance |
| `FUEL` | Fire |
| `SECURITY` | Police and ambulance |
| `FIRE_SMOKE`, `ENGINE`, `GEAR_STRUCTURAL`, `OTHER` | Fire and ambulance |

Backend reserves available units atomically and selects them by distance to the coordination airport. A tried unit is not reused within that incident. Late, duplicate, expired, or superseded responses do not change the current assignment. Backend stops when a capability/total attempt budget is exhausted or coverage/resources are insufficient; it records escalation rather than creating an unbounded mutual-aid loop.

The final response plan is issued only after every required capability accepts. Confirmation is blocked until the active pilot call has a delivered plan. On confirmation, the simulated incident closes and its responders become available again. Cancel and resolve also release responders. Escalation releases pending reservations while accepted resources remain held for explicit human resolution or cancellation.

`NEEDS_CLARIFICATION`, `ESCALATED`, and `FAILED` stop automatic processing. There is currently no edit/resume endpoint. To retry a clarification or escalation, cancel the old incident and create a corrected report with a **new `call_id`**. Reusing a call ID returns its original incident; changing the report does not edit it. Concurrent reports for the same active callsign on different calls are rejected.

The nearest airport is a demo coordination point, not an airport suitability assessment or ATC clearance. The shared voice report currently has no structured confirmed-destination field. Backend does not infer a clearance from the free-text `intentions` field. Aircraft farther than 300 km from the selected airport, or airports beyond the seeded responder coverage, pause for human coordination.

## Person 2: aircraft adapter

`AircraftService(client, mode="mock", allow_fallback=True, provider=None, airports_path=None)` exposes async `lookup(callsign)`. It uses the shared Voice callsign parser. `mode="mock"` always bypasses HTTP and the injected provider.

In live mode, the built-in adapter requests `https://api.adsb.lol/v2/callsign/{callsign}` with an absolute five-second deadline. It rejects absent/ambiguous aircraft, missing/invalid coordinates, stale positions over 120 seconds, invalid telemetry, and malformed responses. Valid zero coordinates are retained. Position age includes feed delay when an ADSB feed timestamp is present.

A previous probe from this environment received HTTP 403; live connectivity has not been reverified for this handoff. That observation does not establish a general ADSB.lol outage. Verify access on the demo machine or rehearse with mock mode.

The lookup result is:

```json
{
  "aircraft": {
    "callsign": "UAL1234",
    "icao_hex": "a1b2c3",
    "latitude": 32.8,
    "longitude": -117.2,
    "altitude_ft": 7200,
    "ground_speed_kt": 210,
    "heading_deg": 155,
    "squawk": "1200",
    "observed_at": "2026-10-24T22:00:00+00:00",
    "source": "mock",
    "is_mock": true,
    "position_age_seconds": 0
  },
  "nearby_airports": [
    {
      "icao": "KMYF",
      "name": "Montgomery-Gibbs Executive Airport",
      "latitude": 32.8157,
      "longitude": -117.1399,
      "distance_km": 5.88
    }
  ],
  "warnings": ["Simulated aircraft position; this is not a live aircraft location."]
}
```

The timestamp above is illustrative; providers must produce a current observation. `icao_hex`, altitude, speed, heading, and squawk may be null. Speed must be nonnegative, heading must be 0–360, and numeric booleans/nonfinite numbers are rejected. Source `mock` or `fallback` requires `is_mock=true`; `adsb.lol` requires `is_mock=false`. Custom provider labels are supported.

Person 2 can supply an async `provider(normalized_callsign) -> dict` to `main.create_app(aircraft_provider=provider)`, with `AIRCRAFT_MODE=live`. It should return the same shape. Provider timestamps must include a UTC offset, be nonfuture, and agree with the required `position_age_seconds` within five seconds. Provider execution has a five-second limit. Backend validates/recalculates airport distances, sorts them, and retains at most five. If the provider omits nearby airports, the local seed supplies them.

`data/airports.json` is an attributed public-domain OurAirports SoCal seed, loaded locally without startup downloads. Direct `AircraftService` construction can pass an OurAirports CSV path through `airports_path`; closed airports, heliports, seaplane bases, and balloonports are excluded. Broader airport data alone does not expand simulated responder coverage.

## HTTP contracts for Voice and the dashboard

Except `/health`, `/docs`, and `/vapi/webhook`, the API routes below use `Authorization: Bearer <API_TOKEN>` when configured. `/vapi/webhook` independently uses `X-Vapi-Secret: <VAPI_WEBHOOK_SECRET>`. Never use the Vapi private API key as the observer token.

| Method and path | Result |
|---|---|
| `GET /health` | Mode flags, authentication flag, and persistence mode |
| `POST /vapi/webhook` | Existing Vapi server-message/tool contract |
| `POST /api/incidents` or `/api/emergencies` | Shared `EmergencyReport`; HTTP 202 with initial incident |
| `GET /api/snapshot` | Incidents, units, airports, dispatchers, retained events, sequence, simulation flags |
| `GET /api/incidents` | Incident array |
| `GET /api/incidents/{id}` | One incident |
| `GET /api/incidents/{id}/voice-update` | Plan text, version, delivery/confirmation flags |
| `POST /api/incidents/{id}/confirm` | Body `{"readback":"wilco"}`; requires a delivered plan on an active pilot call |
| `POST /api/incidents/{id}/cancel` | Cancel and release resources |
| `POST /api/incidents/{id}/resolve` | Resolve and release resources |
| `GET /api/units` | Public simulated-unit records, without phone numbers |
| `GET /api/dispatchers` | Simulated dispatcher records |
| `GET /api/airports` | Configured local airport catalog |
| `GET /api/aircraft/{callsign}` | Aircraft context; HTTP 503 when unusable |
| `GET /api/events?after=0&incident_id=...` | Retained events after a global sequence; incident filter optional |

JSON intake example, using the shared Voice fields:

```json
{
  "call_id": "unique-pilot-call-001",
  "callsign_raw": "United twelve thirty-four",
  "callsign": "UAL1234",
  "emergency_type": "FIRE_SMOKE",
  "severity": "MAYDAY",
  "souls_on_board": 140,
  "fuel_minutes": 25,
  "intentions": "Exercise coordination only; continuing with existing ATC instructions.",
  "summary": "Simulated cabin-smoke report."
}
```

Creating an incident through REST binds it to the provided call ID but does not create a real pilot telephone call. A real pilot uses the existing Vapi assistant/webhook. JSON intake can be used for an offline rehearsal or an already-established call.

`Incident` nests the shared report under `report`; aircraft/airport/dispatcher data is under `aircraft`, `nearby_airports`, `selected_airport`, and `dispatcher`. Use `status`, `version`, `required_unit_types`, `assignments`, `warnings`, `plan_delivered`, and `pilot_confirmed` for display. The simulation flags and aircraft provenance should remain visible.

## WebSocket and event shape

Connect to `/ws/events`. A browser `Origin`, when present, must match `CORS_ORIGINS`. With `API_TOKEN` enabled, either send the bearer header from a client that supports it, or send this first frame within five seconds:

```json
{"token":"your-observer-token"}
```

The server then sends an atomic initial snapshot:

```json
{"type":"snapshot","data":{"incidents":[],"units":[],"airports":[],"dispatchers":[],"events":[],"sequence":0,"simulation":{"emergencies":true,"responders":true,"aircraft_mode":"mock","sim_units":true,"offline_voice":true}}}
```

Each subsequent event has an outer transport envelope and an inner event:

```json
{
  "type": "event",
  "data": {
    "event_id": "evt_example",
    "sequence": 12,
    "timestamp": "2026-10-24T22:00:00+00:00",
    "type": "assignment.pending",
    "kind": "assignment.pending",
    "incident_id": "inc_example",
    "incident_version": 6,
    "message": "Requesting Mesa Fire 2 through the voice service.",
    "data": {
      "assignment_id": "asg_example",
      "incident": {},
      "units": []
    }
  }
}
```

`data.incident` and `data.units` inside the inner event contain full updated records; the empty objects above shorten the example. Voice events retain their original `kind` and selected fields under the inner event's `data.voice_event`, for example a `transcript` event containing `role`, `text`, and `leg`. Unassociated call events may have a null `incident_id`.

Replace local state on `snapshot`. For events, deduplicate by global `sequence` and update the incident and units from the inner payload. The server replaces an overflowed subscriber queue with a fresh snapshot. It sends `{"type":"heartbeat","sequence":...}` when idle; sending text `ping` returns `{"type":"pong"}`. Reconnect to obtain a new snapshot. REST event history retains the latest 2,000 entries; use `/api/snapshot` to recover complete current state rather than assuming unlimited replay.

The public feed excludes raw registries, control URLs, configured unit phone numbers, credentials, and arbitrary Vapi error bodies. Keep dashboard code on these APIs rather than reading Voice internals or log files.

## Offline demo

`scripts/demo_backend.py --base-url http://127.0.0.1:8000 --timeout 40` uses HTTP only. It requires `/health` to report both `offline_voice=true` and `sim_units=true`, submits a synthetic `report_emergency` tool call with unique IDs, prints events, waits for `PILOT_BRIEFED`, and sends `confirm_plan`. It checks that the incident reaches `CONFIRMED` and releases its resources. It reads `API_TOKEN` and `VAPI_WEBHOOK_SECRET` separately from environment or `.env`.
