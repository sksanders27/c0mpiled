import asyncio
from datetime import datetime, timedelta, timezone

import httpx
import pytest

from backend.aircraft import AircraftLookupError, AircraftService, normalize_callsign


@pytest.mark.parametrize("raw, expected", [
    ("United twelve thirty-four", "UAL1234"),
    ("Delta one two three four", "DAL1234"),
    ("ual 1234 ", "UAL1234"),
    ("Southwest twenty one", "SWA21"),
    ("N123AB", "N123AB"),
])
def test_normalize_callsign(raw, expected):
    assert normalize_callsign(raw) == expected


def live_aircraft(**overrides):
    return {"flight": "UAL1234 ", "hex": "abc123", "lat": 32.8, "lon": -117.2,
            "alt_baro": 7000, "gs": 200, "track": 150, "squawk": "7700", "seen_pos": 1, **overrides}


def lookup_with_response(payload, *, fallback=False):
    async def run():
        async with httpx.AsyncClient(transport=httpx.MockTransport(lambda request: httpx.Response(200, json=payload))) as client:
            return await AircraftService(client, mode="live", allow_fallback=fallback).lookup("UAL1234")
    return asyncio.run(run())


def test_mock_mode_never_requests_network():
    def unexpected(request):
        pytest.fail("Mock mode must not issue an HTTP request")

    async def run():
        async with httpx.AsyncClient(transport=httpx.MockTransport(unexpected)) as client:
            return await AircraftService(client).lookup("UAL9999")

    result = asyncio.run(run())
    assert result["aircraft"]["source"] == "mock"
    assert result["aircraft"]["is_mock"] is True
    assert result["aircraft"]["callsign"] == "UAL9999"
    assert result["warnings"]
    distances = [airport["distance_km"] for airport in result["nearby_airports"]]
    assert len(distances) == 5
    assert distances == sorted(distances)


def test_coordinates_zero_are_valid():
    result = lookup_with_response({"ac": [live_aircraft(lat=0, lon=0)]})
    assert result["aircraft"]["latitude"] == 0
    assert result["aircraft"]["longitude"] == 0
    assert result["aircraft"]["source"] == "adsb.lol"
    assert result["aircraft"]["is_mock"] is False


@pytest.mark.parametrize("payload, message", [
    ({"ac": []}, "No live aircraft"),
    ({"ac": [live_aircraft(seen_pos=121)]}, "stale"),
    ({"ac": [live_aircraft(lat=None)]}, "latitude"),
    ({"ac": [live_aircraft(), live_aircraft(hex="def456")]}, "Multiple aircraft"),
])
def test_live_rejects_missing_stale_or_ambiguous_aircraft(payload, message):
    with pytest.raises(AircraftLookupError, match=message):
        lookup_with_response(payload)


def test_http_error_falls_back_with_explicit_labels():
    async def run():
        async with httpx.AsyncClient(transport=httpx.MockTransport(lambda request: httpx.Response(503))) as client:
            return await AircraftService(client, mode="live").lookup("UAL1234")

    result = asyncio.run(run())
    assert result["aircraft"]["source"] == "fallback"
    assert result["aircraft"]["is_mock"] is True
    assert "503" in result["warnings"][0]


def test_feed_timestamp_is_included_when_checking_staleness():
    with pytest.raises(AircraftLookupError, match="stale"):
        lookup_with_response({"now": (datetime.now(timezone.utc).timestamp() - 180) * 1000,
                              "ac": [live_aircraft(seen_pos=1)]})


def test_live_http_error_raises_when_fallback_disabled():
    async def run():
        async with httpx.AsyncClient(transport=httpx.MockTransport(lambda request: httpx.Response(503))) as client:
            return await AircraftService(client, mode="live", allow_fallback=False).lookup("UAL1234")

    with pytest.raises(AircraftLookupError, match="503"):
        asyncio.run(run())


def test_live_request_url_and_timeout():
    def respond(request):
        assert str(request.url) == "https://api.adsb.lol/v2/callsign/UAL1234"
        assert request.extensions["timeout"]["read"] == 5.0
        return httpx.Response(200, json={"ac": [live_aircraft()]})

    async def run():
        async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as client:
            return await AircraftService(client, mode="live").lookup("United twelve thirty-four")

    assert asyncio.run(run())["aircraft"]["callsign"] == "UAL1234"


def test_custom_provider_is_awaited_and_validated():
    async def provider(callsign):
        return {"aircraft": {
            "callsign": callsign, "latitude": 0, "longitude": 0,
            "position_age_seconds": 0, "observed_at": datetime.now(timezone.utc).isoformat(),
            "source": "teammate", "is_mock": False,
        }, "warnings": []}

    async def run():
        async with httpx.AsyncClient() as client:
            return await AircraftService(client, mode="live", provider=provider, allow_fallback=False).lookup("UAL1234")

    result = asyncio.run(run())
    assert result["aircraft"]["source"] == "teammate"
    assert len(result["nearby_airports"]) == 5


def test_mock_mode_ignores_provider():
    async def provider(callsign):
        pytest.fail("Mock mode must not invoke an external provider")

    async def run():
        async with httpx.AsyncClient() as client:
            return await AircraftService(client, provider=provider).lookup("UAL1234")

    assert asyncio.run(run())["aircraft"]["source"] == "mock"


def test_provider_airports_are_preserved_sorted_and_distances_recalculated():
    async def provider(callsign):
        return {"aircraft": {
            "callsign": callsign, "latitude": 0, "longitude": 0,
            "position_age_seconds": 0, "observed_at": datetime.now(timezone.utc).isoformat(),
            "source": "teammate", "is_mock": False,
        }, "nearby_airports": [
            {"icao": "FAR", "name": "Far Airport", "latitude": 0, "longitude": 1, "distance_km": 0},
            {"icao": "NEAR", "name": "Near Airport", "latitude": 0, "longitude": 0, "distance_km": 999},
        ]}

    async def run():
        async with httpx.AsyncClient() as client:
            return await AircraftService(client, mode="live", provider=provider, allow_fallback=False).lookup("UAL1234")

    airports = asyncio.run(run())["nearby_airports"]
    assert [airport["icao"] for airport in airports] == ["NEAR", "FAR"]
    assert airports[0]["distance_km"] == 0
    assert 110 < airports[1]["distance_km"] < 112


@pytest.mark.parametrize("source, is_mock", [("mock", False), ("fallback", False), ("adsb.lol", True)])
def test_provider_rejects_contradictory_provenance_labels(source, is_mock):
    async def provider(callsign):
        return {"aircraft": {
            "callsign": callsign, "latitude": 0, "longitude": 0,
            "position_age_seconds": 0, "observed_at": datetime.now(timezone.utc).isoformat(),
            "source": source, "is_mock": is_mock,
        }}

    async def run():
        async with httpx.AsyncClient() as client:
            return await AircraftService(client, mode="live", provider=provider, allow_fallback=False).lookup("UAL1234")

    with pytest.raises(AircraftLookupError, match="requires is_mock"):
        asyncio.run(run())


def test_provider_must_include_position_age():
    async def provider(callsign):
        return {"aircraft": {
            "callsign": callsign, "latitude": 0, "longitude": 0,
            "observed_at": datetime.now(timezone.utc).isoformat(),
            "source": "teammate", "is_mock": False,
        }}

    async def run():
        async with httpx.AsyncClient() as client:
            return await AircraftService(client, mode="live", provider=provider, allow_fallback=False).lookup("UAL1234")

    with pytest.raises(AircraftLookupError, match="position age"):
        asyncio.run(run())


@pytest.mark.parametrize("overrides", [
    {"ground_speed_kt": -1}, {"heading_deg": -1}, {"heading_deg": 999},
    {"ground_speed_kt": True}, {"heading_deg": False},
])
def test_provider_rejects_out_of_range_or_boolean_telemetry(overrides):
    async def provider(callsign):
        return {"aircraft": {
            "callsign": callsign, "latitude": 0, "longitude": 0,
            "position_age_seconds": 0, "observed_at": datetime.now(timezone.utc).isoformat(),
            "source": "teammate", "is_mock": False, **overrides,
        }}

    async def run():
        async with httpx.AsyncClient() as client:
            return await AircraftService(client, mode="live", provider=provider, allow_fallback=False).lookup("UAL1234")

    with pytest.raises(AircraftLookupError, match="Invalid (ground speed|heading)"):
        asyncio.run(run())


@pytest.mark.parametrize("overrides", [
    {"gs": -1}, {"track": -1}, {"track": 999}, {"gs": True}, {"track": False},
])
def test_live_rejects_out_of_range_or_boolean_telemetry(overrides):
    with pytest.raises(AircraftLookupError, match="Invalid (ground speed|heading)"):
        lookup_with_response({"ac": [live_aircraft(**overrides)]})


@pytest.mark.parametrize("age, observed_offset, message", [
    (0, 1, "in the future"),
    (60, -1, "disagrees"),
    (0, -60, "disagrees"),
])
def test_provider_rejects_future_or_inconsistent_timestamps(age, observed_offset, message):
    async def provider(callsign):
        return {"aircraft": {
            "callsign": callsign, "latitude": 0, "longitude": 0,
            "position_age_seconds": age,
            "observed_at": (datetime.now(timezone.utc) + timedelta(seconds=observed_offset)).isoformat(),
            "source": "teammate", "is_mock": False,
        }}

    async def run():
        async with httpx.AsyncClient() as client:
            return await AircraftService(client, mode="live", provider=provider, allow_fallback=False).lookup("UAL1234")

    with pytest.raises(AircraftLookupError, match=message):
        asyncio.run(run())


@pytest.mark.parametrize("gs, track", [(0, 0), (0, 360), (None, None)])
def test_live_allows_telemetry_boundaries_and_missing_values(gs, track):
    aircraft = lookup_with_response({"ac": [live_aircraft(gs=gs, track=track)]})["aircraft"]
    assert aircraft["ground_speed_kt"] == gs
    assert aircraft["heading_deg"] == track


def test_ourairports_csv_can_replace_local_seed(tmp_path):
    airports = tmp_path / "airports.csv"
    airports.write_text("ident,type,name,latitude_deg,longitude_deg\nTEST,small_airport,Test Airport,32.8,-117.2\nCLOSED,closed,Closed Airport,32.8,-117.2\nCLOSED2,closed_airport,Closed Airport Two,32.8,-117.2\n", encoding="utf-8")

    async def run():
        async with httpx.AsyncClient() as client:
            return await AircraftService(client, airports_path=airports).lookup("UAL1234")

    result = asyncio.run(run())
    assert [airport["icao"] for airport in result["nearby_airports"]] == ["TEST"]
    assert result["nearby_airports"][0]["distance_km"] == 0


@pytest.mark.parametrize("raw, expected", [
    ("November one two three alpha bravo", "N123AB"),
    ("Speedbird two eight kilo heavy", "BAW28K"),
    ("United niner zero", "UAL90"),
])
def test_backend_reuses_voice_callsign_parser(raw, expected):
    assert normalize_callsign(raw) == expected


@pytest.mark.parametrize("raw", [None, "", "United", "unknown aircraft", "9999"])
def test_uncertain_callsigns_require_clarification(raw):
    with pytest.raises(AircraftLookupError, match="callsign"):
        normalize_callsign(raw)