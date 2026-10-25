# SkyWatch emergency dispatch

The integrated FastAPI app connects the team's existing Vapi voice service to aircraft lookup, nearby airports, simulated responder selection, automatic reassignment, and observer APIs.

**Every emergency and responder is simulated.** An airport selected by distance is a demonstration coordination point. It is not a landing recommendation or ATC clearance. Pilots continue following their existing ATC instructions.

## Run an offline demonstration

Python 3.11 or newer is required.

```bash
python -m venv .venv
.venv/bin/pip install -r requirements.txt
cp -n .env.example .env

OFFLINE_VOICE=1 SIM_UNITS=1 MOCK_DISPATCH=0 AIRCRAFT_MODE=mock .venv/bin/uvicorn main:app --workers 1 --port 8000
```

In a second terminal:

```bash
.venv/bin/python scripts/demo_backend.py --base-url http://127.0.0.1:8000
```

Open **http://127.0.0.1:8000/observer** while it runs to watch the map, incident card and timeline.

<details><summary>Windows (PowerShell)</summary>

```powershell
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements.txt
if (-not (Test-Path .env)) { Copy-Item .env.example .env }
$env:OFFLINE_VOICE = "1"; $env:SIM_UNITS = "1"; $env:MOCK_DISPATCH = "0"; $env:AIRCRAFT_MODE = "mock"
.\.venv\Scripts\python.exe -m uvicorn main:app --workers 1 --port 8000
# second terminal
.\.venv\Scripts\python.exe scripts/demo_backend.py
```
</details>

Use one worker: incidents, reservations, call correlation and event history are stored in memory and reset when the process restarts.

The demo script checks that both offline voice and simulated units are enabled before sending anything. It then:
1. submits a synthetic pilot tool call,
2. prints the dispatch timeline,
3. waits for the first unit to decline and replacement units to accept,
4. submits the pilot's confirmation.

Confirmation closes the incident and releases its simulated responders. No real phone calls are made.

Useful URLs:
- [observer](http://127.0.0.1:8000/observer)
- [pilot browser fallback](http://127.0.0.1:8000/pilot): a web call to the same pilot assistant, if the phone line fails
- [API documentation](http://127.0.0.1:8000/docs)
- [health](http://127.0.0.1:8000/health)

Dashboards can build against `/api/snapshot` (also served as `/observer/state`), `/api/events` and `/ws/events`. Detailed contracts are in [the integration guide](docs/BACKEND_INTEGRATION.md).

## Environment modes (`.env`)

Switching modes means editing `.env` and restarting. Every live mode runs `uvicorn main:app --workers 1 --port 8000`.

| Variable | Offline rehearsal | **Live demo (sim units)** | Live demo (real unit calls) | Voice-only fallback (`dev_main:app`) |
|---|---|---|---|---|
| `UNIT_TRANSPORT` | phone | phone | **web** (browser units) or phone (needs Twilio number) | phone |
| `MOCK_DISPATCH` | 0 | **0** | 0 | 1 |
| `OFFLINE_VOICE` | 1 | **0** ← must be explicit | 0 | n/a |
| `SIM_UNITS` | 1 | **1** | 0 | 1 |
| `AIRCRAFT_MODE` | mock | **live** | live | n/a |
| `ALLOW_AIRCRAFT_FALLBACK` | 1 | **1** | 1 | n/a |
| `SCRIPTED_DECLINE_FIRST` | 0 | 0 | **1** | 0 |
| `API_TOKEN` | blank | **blank** | blank | n/a |
| `VAPI_OUTBOUND_PHONE_NUMBER_ID` | n/a | n/a | Twilio-imported number ID | n/a |

**`OFFLINE_VOICE` defaults to true.** If it isn't explicitly `0`, live call control goes to an offline stub, and a real pilot never hears dispatch updates.

### Which teammate's phone rings (real unit calls only)

`DispatchEngine` maps units in seed order onto `UNIT_PHONE_1..n`, cycling through them. `main:app` logs the mapping at startup, with phones masked to the last 4 digits. With two phones:

| Phone | Units |
|---|---|
| `UNIT_PHONE_1` | fire-1, police-1, ambulance-2, fire-3, police-3 |
| `UNIT_PHONE_2` | ambulance-1, fire-2, police-2, ambulance-3 |

For a MEDICAL emergency near San Diego, expect ambulance-1 (phone 2) first, then ambulance-2 (phone 1).

### Units as browser web calls (`UNIT_TRANSPORT=web`)

Our Vapi number can't dial out, so units can instead be reached through a browser. With `SIM_UNITS=0` and `UNIT_TRANSPORT=web`:
1. Each teammate opens `https://<public url>/unit?u=N` on their phone and taps **Go on duty**. Slot `N` is `UNIT_PHONE_N`'s slot in the table above.
2. When dispatch picks a unit in that slot, the page rings and starts a Vapi web call to the unit assistant.
3. The page binds that call to the dispatch. Accept/decline, `SCRIPTED_DECLINE_FIRST`, the 45 s timeout and hang-up = no-answer all behave as they do for phone calls.

Keep the page in the foreground with the screen on.

### Between rehearsals

**Automatic:** `main:app` runs the same reset `AUTO_RESET_AFTER_HANGUP_S` seconds (default 15) after a pilot call ends. The delay keeps the finished incident on the observer briefly. The reset is skipped if another pilot call is already live. Set the variable to `off` to disable it.

**Manual:** `POST /dev/reset` clears incidents, reservations, events and voice call state without a restart. It uses the same bearer auth as `/api/*`, so it's open when `API_TOKEN` is blank. Without a reset, re-using a callsign that still has an active incident returns 409, and the pilot hears "Dispatch could not open this incident…".

```bash
curl -X POST http://127.0.0.1:8000/dev/reset
```

## Connect the real pilot voice agent

The `voice/` package and the shared `voice.models` contracts remain the integration boundary. `main.py`:
- creates `VoiceService`,
- binds the real `DispatchEngine` through `DispatchPort`,
- exposes `/vapi/webhook` in the same process.

No HTTP adapter between Voice and Backend is required.

Before a live call, pick a flight that will stay in range. It prefers low, descending San Diego arrivals, and prints the line to say:

```bash
.venv/bin/python scripts/pick_flight.py --check
```

For a real pilot call:
1. Configure the Vapi credentials and public webhook URL in `.env`, using the "Live demo (sim units)" column above.
2. Run `.venv/bin/python scripts/setup_vapi.py`.
3. Restart `main:app`.

`OFFLINE_VOICE=0` requires `VAPI_API_KEY`.

The team's Vapi number is a free Vapi number and **cannot place outbound calls**. Keep `SIM_UNITS=1` unless a Twilio/Telnyx number has been imported. Real outbound calls also need allow-listed teammate numbers (`UNIT_PHONE_*`) and the explicit approval required by `CLAUDE.md`.

`dev_main:app` remains the voice-only application. It requires `MOCK_DISPATCH=1` and uses `MockDispatch`, plus `aircraft_data/` for enrichment. It does not run the real dispatch backend.

## Aircraft data and resource behavior

**Aircraft lookup:**
- `AIRCRAFT_MODE=mock` uses a clearly labeled San Diego sample, with no network requests.
- `AIRCRAFT_MODE=live` queries ADSB.lol by normalized callsign.
- Local airport coordinates (`data/airports.json`) provide up to five nearby airports.
- With `ALLOW_AIRCRAFT_FALLBACK=1`, live failures can return explicitly labeled sample data. Set it to `0` to require usable live data.
- Verify live access from the demo machine before presenting.

**Responders:**
- Emergency types determine the capabilities required. Fire/smoke needs both a fire unit and an ambulance; medical needs an ambulance.
- The final plan waits for every required capability to accept.
- Defaults allow three attempts per capability and four overall. The overall limit is also bounded by Voice's outbound-call cap.
- Failed, declined, expired and superseded responses cannot overwrite a newer assignment.

**Security:**
- `API_TOKEN` protects observer and JSON APIs with `Authorization: Bearer ...`.
- `VAPI_WEBHOOK_SECRET` separately protects `/vapi/webhook` with `X-Vapi-Secret`.
- Set both before exposing the service publicly. Leave `API_TOKEN` blank on stage, or the observer will prompt for it.

## Standalone aircraft utility (`aircraft_data/`)

`aircraft_data/` is a parallel aircraft/airport lookup, used only by the `dev_main.py` fallback. The integrated app uses `backend/aircraft.py`. Don't unify the two before the demo.

```bash
python -m aircraft_data.demo "United twelve thirty-four"
python -m aircraft_data.demo UAL1234 --weather
python -m aircraft_data.demo UAL1234 --demo
```

`dev_main:app` exposes `GET /api/aircraft/{callsign}` with telemetry, nearby airports and optional METAR. Sample substitution is opt-in with `?demo_fallback=true`, or with `AIRCRAFT_DEMO_FALLBACK=1` at startup. Sample aircraft are labeled simulated in the observer.

## Tests

```bash
.venv/bin/python -m pytest -q
```

No real-call script needs to run to test the integration.
