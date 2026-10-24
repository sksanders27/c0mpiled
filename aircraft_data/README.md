# Aircraft data service

`AircraftDataService` retrieves aircraft by normalized or spoken callsign from ADSB.lol, computes nearby airport distances from OurAirports CSV data, and can optionally retrieve METAR data from AviationWeather.gov.

```python
context = await service.get_aircraft_context(
    "United twelve thirty-four",
    nearest_count=5,
    radius_nm=150,
    include_weather=False,
    allow_sample_fallback=False,
)
```

Statuses are `live`, `sample`, `not_found`, `position_unavailable`, and `unavailable`. A stale or missing position never produces live map coordinates. Sample data is returned only when the caller explicitly enables `allow_sample_fallback`; it is marked `aircraft.simulated = true`.

`ground_track_deg` is ground track, not magnetic heading. `speed_knots` is ground speed, not airspeed. Squawk is a string so leading zeroes are preserved. Airport distance is great-circle proximity only; it does not indicate runway compatibility, operating status, emergency-service availability, or ATC authorization.

The standalone endpoint can be mounted with `build_aircraft_router(service)`. `dev_main.py` mounts it at `/api/aircraft/{callsign}` and enriches each mock incident in a tracked background task. Enrichment failures emit `aircraft_lookup_completed` with status `unavailable`; they do not delay the voice response or replace the original pilot report.

## Commands

```bash
python -m aircraft_data.demo --warm-airports
python -m aircraft_data.demo "United twelve thirty-four"
python -m aircraft_data.demo UAL1234 --weather
python -m aircraft_data.demo UAL1234 --demo
python -m pytest -q tests/test_aircraft_data.py
```

Airport CSV caching is atomic. For rehearsal, warm it before launching the demo. The local observer app disables airport downloads unless `WARM_AIRPORT_CACHE=1`; with no cache it uses the bundled sample airport list and reports `airports_source = sample_airports`.
