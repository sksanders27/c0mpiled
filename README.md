# SkyWatch emergency dispatch

The integrated FastAPI app connects the team's existing Vapi voice service to aircraft lookup, nearby airports, simulated responder selection, automatic reassignment, and observer APIs.

**Every emergency and responder is simulated.** An airport selected by distance is a demonstration coordination point. It is not a landing recommendation or ATC clearance. Pilots continue following their existing ATC instructions.

## Run an offline demonstration on Windows

Run these PowerShell commands from the project folder. Python 3.11 or newer is required.

```powershell
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements.txt
if (-not (Test-Path .env)) { Copy-Item .env.example .env }

$env:OFFLINE_VOICE = "1"
$env:SIM_UNITS = "1"
$env:MOCK_DISPATCH = "0"
$env:AIRCRAFT_MODE = "mock"
.\.venv\Scripts\python.exe -m uvicorn main:app --workers 1 --port 8000
```

Use one worker: incidents, reservations, call correlation, and event history are stored in memory and reset when the process restarts. The integrated defaults also use offline voice, simulated units, and mock aircraft data; the explicit settings above make the rehearsal configuration clear.

In a second PowerShell terminal, run:

```powershell
.\.venv\Scripts\python.exe scripts/demo_backend.py
```

The script verifies that both offline voice and simulated units are enabled before sending anything. It submits a synthetic pilot tool call, prints the dispatch timeline, waits for the first unit to decline and replacement units to accept, then submits the pilot's confirmation. Confirmation closes the incident and releases its simulated responders. It creates no real phone calls.

Open [API documentation](http://127.0.0.1:8000/docs) or [health](http://127.0.0.1:8000/health). There is no dashboard bundled here; Person 4 can build against `/api/snapshot`, `/api/events`, and `/ws/events`. Detailed contracts are in [the integration guide](docs/BACKEND_INTEGRATION.md).

## Connect the real pilot voice agent

The existing `voice/` package and shared `voice.models` contracts remain the integration boundary. `main.py` creates `VoiceService`, binds the real `DispatchEngine` through `DispatchPort`, and exposes the existing `/vapi/webhook` in the same process. No HTTP adapter between Voice and Backend is required.

For a real pilot call, configure the existing Vapi credentials and public webhook URL in `.env`, set `OFFLINE_VOICE=0`, and restart `main:app`. Keep `MOCK_DISPATCH=0` and `SIM_UNITS=1` for simulated responders. `OFFLINE_VOICE=0` requires `VAPI_API_KEY`; offline mode records speech operations locally and does not speak into a real pilot call.

The team's status handoff reports that its free Vapi number cannot make outbound unit calls. Keep responder simulation enabled for this setup. Real outbound calls need a supported imported number, allowlisted teammate numbers, and the explicit approval required by `CLAUDE.md`.

`dev_main:app` remains the original voice-only application. It requires `MOCK_DISPATCH=1` and uses `MockDispatch`; it does not run the real aircraft/dispatch backend.

## Aircraft data and resource behavior

`AIRCRAFT_MODE=mock` uses a clearly labeled San Diego sample without network requests. `AIRCRAFT_MODE=live` queries ADSB.lol by normalized callsign. Local OurAirports coordinates provide up to five nearby airports. Live failures can return explicitly labeled sample data when `ALLOW_AIRCRAFT_FALLBACK=1`; set it to `0` to require usable live data. Verify live access from the demo machine before presenting.

Emergency types determine the capabilities required. Fire/smoke requires both a fire unit and an ambulance, and the final plan waits for every required capability to accept. Defaults allow three attempts per capability and four overall; the overall limit is also bounded by Voice's outbound-call cap. Failed, declined, expired, and superseded responses cannot overwrite a newer assignment.

`API_TOKEN` protects observer and JSON APIs with `Authorization: Bearer ...`. `VAPI_WEBHOOK_SECRET` separately protects `/vapi/webhook` with `X-Vapi-Secret`. Set both before exposing the service publicly. The rehearsal script reads these values from environment variables or `.env` without printing them.

## Tests

```powershell
.\.venv\Scripts\python.exe -m pytest -q
```

No real-call script needs to run to test the integration.

## Source handoff

This work was prepared against the team's GitHub `main` source snapshot at `3590e481101817956d1a957cb387fb8c72fe48e8`. The local destination initially contained handoff documents rather than a Git checkout, and the available Git runtime lacked its HTTPS helper. Source files were imported from the pinned snapshot through the GitHub connection.

The accompanying `backend-integration.patch` is the portable change set for a teammate's normal checkout. From a compatible clean checkout, review and apply it with:

```powershell
git apply --check path\to\backend-integration.patch
git apply path\to\backend-integration.patch
```

Check the base revision and local changes first. A source import or patch handoff does not imply a commit or push to the team's repository.
