# SkyWatch

Voice-first aircraft emergency coordination prototype. Aircraft telemetry and airport distances are informational context only; they do not establish airport suitability or authorize a landing, diversion, or dispatch.

## Run the local demo

```bash
pip install -r requirements.txt
python -m aircraft_data.demo --warm-airports
MOCK_DISPATCH=1 SIM_UNITS=1 uvicorn dev_main:app --port 8000
```

Open `http://127.0.0.1:8000/observer` for the map and event feed. `http://127.0.0.1:8000/health` checks the app. The voice demo uses simulated units and never places outbound calls.

For an explicit offline aircraft demonstration, set `AIRCRAFT_DEMO_FALLBACK=1` before starting the app. Sample aircraft are labeled simulated in the observer. Live lookups do not silently substitute sample coordinates.

## Aircraft lookup

```bash
python -m aircraft_data.demo "United twelve thirty-four"
python -m aircraft_data.demo UAL1234 --weather
python -m aircraft_data.demo UAL1234 --demo
python -m pytest -q
```

The optional HTTP endpoint is `GET /api/aircraft/{callsign}`. It returns aircraft telemetry, up to five nearby airports within 150 NM, source/status fields, and optional METAR for the nearest airport with `?include_weather=true`. Sample substitution is opt-in with `?demo_fallback=true`.
