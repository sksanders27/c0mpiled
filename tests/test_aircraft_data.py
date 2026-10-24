from __future__ import annotations

import asyncio
import json

import httpx
import pytest

from aircraft_data.service import AircraftDataService, _parse_airports
from aircraft_data.integration import AircraftEnrichmentDispatch
from voice.models import EmergencyReport, EmergencyType, Severity


AIRPORTS = """ident,iata_code,name,type,latitude_deg,longitude_deg,closed
KSAN,SAN,San Diego International Airport,large_airport,32.7338,-117.1933,0
KMYF,MYF,Montgomery-Gibbs Executive Airport,medium_airport,32.8157,-117.1396,0
KHEL,,Not an airport,heliport,32.8,-117.1,0
KSEA,SEA,Closed Airport,medium_airport,32.8,-117.2,1
"""


def live_row(**overrides):
    row = {
        "hex": "abc123", "flight": "UAL1234 ", "lat": 32.73, "lon": -117.19,
        "alt_baro": 8500, "alt_geom": 8725, "gs": 210.5, "track": 125.25,
        "squawk": "0077", "seen_pos": 12, "now": 1700000000,
    }
    row.update(overrides)
    return row


def make_service(tmp_path, handler, **options):
    transport = httpx.MockTransport(handler)
    client = httpx.AsyncClient(transport=transport)
    cache = tmp_path / "airports.csv"
    cache.write_text(AIRPORTS)
    return AircraftDataService(client, airport_cache=cache, **options), client


@pytest.mark.asyncio
async def test_live_lookup_normalizes_spoken_callsign_and_returns_nearest_airports(tmp_path):
    def handler(request):
        assert request.url.path.endswith("/callsign/UAL1234")
        return httpx.Response(200, json={"ac": [live_row()]})

    service, client = make_service(tmp_path, handler)
    try:
        result = await service.get_aircraft_context("United twelve thirty-four")
    finally:
        await client.aclose()

    assert result["status"] == "live"
    assert result["aircraft"]["callsign"].strip() == "UAL1234"
    assert result["aircraft"]["icao24"] == "abc123"
    assert result["aircraft"]["altitude_ft"] == 8500
    assert result["aircraft"]["altitude_geom_ft"] == 8725
    assert result["aircraft"]["speed_knots"] == 210.5
    assert result["aircraft"]["ground_track_deg"] == 125.25
    assert result["aircraft"]["squawk"] == "0077"
    assert len(result["nearby_airports"]) == 2
    assert result["nearby_airports"][0]["ident"] == "KSAN"
    assert [a["ident"] for a in result["nearby_airports"]] == ["KSAN", "KMYF"]


@pytest.mark.asyncio
async def test_empty_adsb_result_is_not_found_and_never_substitutes_sample(tmp_path):
    service, client = make_service(tmp_path, lambda _: httpx.Response(200, json={"ac": []}))
    try:
        result = await service.get_aircraft_context("UAL1234")
    finally:
        await client.aclose()
    assert result["status"] == "not_found"
    assert result["aircraft"] is None


@pytest.mark.asyncio
async def test_unparseable_spoken_callsign_does_not_query_or_invent_identifier(tmp_path):
    def handler(_):
        raise AssertionError("unparseable callsign must not reach ADSB")

    service, client = make_service(tmp_path, handler)
    try:
        result = await service.get_aircraft_context("United")
    finally:
        await client.aclose()
    assert result["status"] == "unavailable"
    assert result["aircraft"] is None


@pytest.mark.asyncio
async def test_404_is_not_found(tmp_path):
    service, client = make_service(tmp_path, lambda _: httpx.Response(404))
    try:
        result = await service.get_aircraft_context("UAL1234")
    finally:
        await client.aclose()
    assert result["status"] == "not_found"


@pytest.mark.asyncio
async def test_malformed_json_and_rate_limit_are_unavailable(tmp_path):
    async def run(response):
        service, client = make_service(tmp_path, lambda _: response)
        try:
            return await service.get_aircraft_context("UAL1234")
        finally:
            await client.aclose()

    malformed = await run(httpx.Response(200, content=b"not-json"))
    limited = await run(httpx.Response(429))
    assert malformed["status"] == "unavailable"
    assert limited["status"] == "unavailable"
    assert malformed["aircraft"] is None


@pytest.mark.asyncio
async def test_timeout_is_unavailable(tmp_path):
    def handler(_):
        raise httpx.ReadTimeout("timed out")

    service, client = make_service(tmp_path, handler)
    try:
        result = await service.get_aircraft_context("UAL1234")
    finally:
        await client.aclose()
    assert result["status"] == "unavailable"


@pytest.mark.asyncio
async def test_stale_position_is_not_returned_as_live_coordinates(tmp_path):
    service, client = make_service(
        tmp_path, lambda _: httpx.Response(200, json={"ac": [live_row(seen_pos=121)]})
    )
    try:
        result = await service.get_aircraft_context("UAL1234")
    finally:
        await client.aclose()
    assert result["status"] == "position_unavailable"
    assert result["aircraft"]["latitude"] is None
    assert result["aircraft"]["longitude"] is None
    assert result["aircraft"]["position_stale"] is True


@pytest.mark.asyncio
async def test_ground_altitude_and_missing_position_are_handled(tmp_path):
    service, client = make_service(
        tmp_path, lambda _: httpx.Response(200, json={"ac": [live_row(lat=None, lon=None, alt_baro="ground")]})
    )
    try:
        result = await service.get_aircraft_context("UAL1234")
    finally:
        await client.aclose()
    assert result["status"] == "position_unavailable"
    assert result["aircraft"]["altitude_ft"] == 0
    assert result["aircraft"]["on_ground"] is True


@pytest.mark.asyncio
async def test_duplicate_matches_choose_freshest_row(tmp_path):
    service, client = make_service(
        tmp_path,
        lambda _: httpx.Response(200, json={"ac": [live_row(seen_pos=50), live_row(seen_pos=3, lat=32.8)]}),
    )
    try:
        result = await service.get_aircraft_context("UAL1234")
    finally:
        await client.aclose()
    assert result["status"] == "live"
    assert result["aircraft"]["position_age_seconds"] == 3
    assert result["aircraft"]["latitude"] == 32.8


@pytest.mark.asyncio
async def test_missing_airport_cache_uses_bundled_airports_after_fetch_failure(tmp_path):
    service = AircraftDataService(
        httpx.AsyncClient(transport=httpx.MockTransport(lambda _: httpx.Response(503))),
        airport_cache=tmp_path / "missing" / "airports.csv",
    )
    try:
        airports, source = await service._load_airports()
    finally:
        await service.client.aclose()
    assert source == "sample_airports"
    assert airports


@pytest.mark.asyncio
async def test_sample_fallback_is_explicit_and_marked_simulated(tmp_path):
    service, client = make_service(tmp_path, lambda _: httpx.Response(503))
    try:
        result = await service.get_aircraft_context("UAL1234", allow_sample_fallback=True)
    finally:
        await client.aclose()
    assert result["status"] == "sample"
    assert result["aircraft"]["simulated"] is True
    assert "Simulated" in result["warning"]
    assert result["nearby_airports"]


def test_airport_parser_excludes_heliports_and_closed_airports():
    airports = _parse_airports(AIRPORTS)
    assert {airport["ident"] for airport in airports} == {"KSAN", "KMYF"}


@pytest.mark.asyncio
async def test_incident_enrichment_is_backgrounded_and_emits_observer_event():
    class Backend:
        def __init__(self):
            self.events = []
            self.responses = []
            self.confirms = []

        async def create_incident(self, report):
            return "INC-TEST"

        async def on_unit_response(self, incident_id, unit_id, accepted, reason):
            self.responses.append((incident_id, unit_id, accepted, reason))

        async def on_pilot_confirm(self, incident_id, readback):
            self.confirms.append((incident_id, readback))

        async def on_event(self, event):
            self.events.append(event)

    class Aircraft:
        async def get_aircraft_context(self, callsign, **kwargs):
            await asyncio.sleep(0)
            return {"status": "live", "callsign": callsign, "aircraft": {"latitude": 32.7, "longitude": -117.2, "simulated": False},
                    "nearby_airports": [{"ident": "KSAN", "distance_nm": 1.2}], "airports_source": "ourairports_cache"}

    backend = Backend()
    dispatch = AircraftEnrichmentDispatch(backend, Aircraft())
    report = EmergencyReport(
        call_id="call-1", callsign_raw="United twelve thirty-four", callsign="UAL1234",
        emergency_type=EmergencyType.ENGINE, severity=Severity.MAYDAY, summary="Engine problem.",
    )
    incident_id = await dispatch.create_incident(report)
    assert incident_id == "INC-TEST"  # lookup has not been awaited by incident creation
    await asyncio.gather(*dispatch._tasks)
    event = backend.events[0]
    assert event["kind"] == "aircraft_lookup_completed"
    assert event["incident_id"] == incident_id
    assert event["callsign"] == "UAL1234"
    assert event["status"] == "live"
    assert event["aircraft"]["latitude"] == 32.7
    assert event["nearby_airports"][0]["ident"] == "KSAN"
    assert event["simulated"] is False
    await dispatch.on_unit_response(incident_id, "unit-1", True, None)
    await dispatch.on_pilot_confirm(incident_id, "wilco")
    assert backend.responses == [(incident_id, "unit-1", True, None)]
    assert backend.confirms == [(incident_id, "wilco")]
