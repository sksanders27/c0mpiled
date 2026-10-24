# SkyWatch

Voice-first aircraft emergency coordination prototype. Aircraft telemetry and airport distances are informational context only; they do not establish airport suitability or authorize a landing, diversion, or dispatch.

## Run the integrated backend demo

```bash
pip install -r requirements.txt
OFFLINE_VOICE=1 SIM_UNITS=1 MOCK_DISPATCH=0 AIRCRAFT_MODE=mock uvicorn main:app --port 8000
python scripts/demo_backend.py --base-url http://127.0.0.1:8000
```

Open `http://127.0.0.1:8000/observer` for the map and event feed. The integrated app runs Voice and the dispatch backend in one FastAPI process. Offline mode uses a no-network Vapi substitute and simulated units, so the rehearsal makes no phone calls. `http://127.0.0.1:8000/health` reports the active modes.

To use the standalone Voice rehearsal with `MockDispatch`, run `MOCK_DISPATCH=1 SIM_UNITS=1 uvicorn dev_main:app --port 8000` instead.

For an explicit offline aircraft demonstration, set `AIRCRAFT_DEMO_FALLBACK=1` before starting the app. Sample aircraft are labeled simulated in the observer. Live lookups do not silently substitute sample coordinates.

## Aircraft lookup

```bash
python -m aircraft_data.demo "United twelve thirty-four"
python -m aircraft_data.demo UAL1234 --weather
python -m aircraft_data.demo UAL1234 --demo
python -m pytest -q
```

The integrated backend exposes `GET /api/aircraft/{callsign}` and the incident, unit, airport, event, and snapshot APIs described in [the backend integration guide](docs/BACKEND_INTEGRATION.md). Configure `API_TOKEN` to require a bearer token for dashboard/API access. Configure `VAPI_WEBHOOK_SECRET` separately to authenticate Vapi webhooks.

The standalone aircraft utility also exposes `GET /api/aircraft/{callsign}` from `dev_main:app`; it returns telemetry, nearby airports, and optional METAR. Its sample substitution is opt-in with `?demo_fallback=true`.
