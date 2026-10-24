"""Swappable aircraft intelligence adapter for the dispatch backend.

Person 2 can pass an async ``provider(callsign)`` returning the documented
lookup result. Mock mode performs no network requests. Local airport coordinates
are intentionally loaded from disk rather than fetched when the app starts.
"""

from __future__ import annotations

import asyncio
import csv
import inspect
import json
import math
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Awaitable, Callable

import httpx

from voice.callsign import normalize_callsign as _voice_normalize_callsign


MAX_POSITION_AGE_SECONDS = 120.0
DEFAULT_AIRPORTS_PATH = Path(__file__).resolve().parents[1] / "data" / "airports.json"
AircraftProvider = Callable[[str], Awaitable[dict[str, Any]]]


class AircraftLookupError(RuntimeError):
    """The aircraft could not be located safely enough for this demonstration."""


def normalize_callsign(raw: str) -> str:
    """Use Voice's shared parser; uncertain identities require clarification."""
    if not isinstance(raw, str) or not raw.strip():
        raise AircraftLookupError("An aircraft callsign is required.")
    normalized = _voice_normalize_callsign(raw)
    if normalized is None:
        raise AircraftLookupError("Unable to normalize aircraft callsign; clarification is required.")
    return normalized


def _number(value: Any, label: str, *, minimum: float | None = None,
            maximum: float | None = None) -> float:
    if isinstance(value, bool):
        raise AircraftLookupError(f"Invalid {label} in aircraft data.")
    try:
        result = float(value)
    except (ValueError, TypeError):
        raise AircraftLookupError(f"Missing or invalid {label} in aircraft data.") from None
    if not math.isfinite(result) or (minimum is not None and result < minimum) or (
        maximum is not None and result > maximum
    ):
        raise AircraftLookupError(f"Invalid {label} in aircraft data.")
    return result


def _optional_number(value: Any, label: str, *, minimum: float | None = None,
                     maximum: float | None = None) -> float | None:
    return None if value is None else _number(value, label, minimum=minimum, maximum=maximum)


def _distance_km(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    lat_a, lat_b = math.radians(lat1), math.radians(lat2)
    dlat = lat_b - lat_a
    dlon = math.radians(lon2 - lon1)
    haversine = math.sin(dlat / 2) ** 2 + math.cos(lat_a) * math.cos(lat_b) * math.sin(dlon / 2) ** 2
    return 6371.0088 * 2 * math.asin(math.sqrt(min(1.0, max(0.0, haversine))))


class AircraftService:
    def __init__(self, client: httpx.AsyncClient, mode: str = "mock",
                 allow_fallback: bool = True, provider: AircraftProvider | None = None,
                 airports_path: Path | None = None) -> None:
        if mode not in {"mock", "live"}:
            raise ValueError("Aircraft mode must be 'mock' or 'live'.")
        self.client = client
        self.mode = mode
        self.allow_fallback = allow_fallback
        self.provider = provider
        self.airports = self._load_airports(Path(airports_path) if airports_path else DEFAULT_AIRPORTS_PATH)

    @staticmethod
    def _load_airports(path: Path) -> list[dict[str, Any]]:
        try:
            if path.suffix.lower() == ".csv":
                with path.open(encoding="utf-8-sig", newline="") as source:
                    rows = list(csv.DictReader(source))
            else:
                content = json.loads(path.read_text(encoding="utf-8-sig"))
                rows = content.get("airports", []) if isinstance(content, dict) else content
            airports = []
            for row in rows:
                if row.get("type") in {"closed", "closed_airport", "heliport", "seaplane_base", "balloonport"}:
                    continue
                airports.append({
                    "icao": str(row.get("icao") or row.get("ident") or "").strip(),
                    "name": str(row["name"]),
                    "latitude": _number(row.get("latitude", row.get("latitude_deg")), "airport latitude", minimum=-90, maximum=90),
                    "longitude": _number(row.get("longitude", row.get("longitude_deg")), "airport longitude", minimum=-180, maximum=180),
                })
            if not airports or any(not airport["icao"] for airport in airports):
                raise ValueError("No valid airports found")
            return airports
        except (OSError, ValueError, KeyError, TypeError, AircraftLookupError) as exc:
            raise AircraftLookupError(f"Unable to load local airport data: {exc}") from exc

    def _nearby(self, latitude: float, longitude: float) -> list[dict[str, Any]]:
        airports = [{**airport, "distance_km": round(_distance_km(latitude, longitude, airport["latitude"], airport["longitude"]), 2)}
                    for airport in self.airports]
        return sorted(airports, key=lambda airport: airport["distance_km"])[:5]

    def _mock(self, callsign: str, *, source: str = "mock", warning: str | None = None) -> dict[str, Any]:
        aircraft = {
            "callsign": callsign,
            "icao_hex": "a1b2c3",
            "latitude": 32.80,
            "longitude": -117.20,
            "altitude_ft": 7200.0,
            "ground_speed_kt": 210.0,
            "heading_deg": 155.0,
            "squawk": "1200",
            "observed_at": datetime.now(timezone.utc).isoformat(),
            "source": source,
            "is_mock": True,
            "position_age_seconds": 0.0,
        }
        warnings = ["Simulated aircraft position near San Diego; this is not the requested aircraft's live position."]
        if warning:
            warnings.insert(0, warning)
        return {"aircraft": aircraft, "nearby_airports": self._nearby(aircraft["latitude"], aircraft["longitude"]), "warnings": warnings}

    def _validate_provider(self, result: Any, callsign: str) -> dict[str, Any]:
        if not isinstance(result, dict) or not isinstance(result.get("aircraft"), dict):
            raise AircraftLookupError("Aircraft provider must return an aircraft result object.")
        aircraft = dict(result["aircraft"])
        latitude = _number(aircraft.get("latitude"), "latitude", minimum=-90, maximum=90)
        longitude = _number(aircraft.get("longitude"), "longitude", minimum=-180, maximum=180)
        age = _number(aircraft.get("position_age_seconds"), "position age", minimum=0)
        if age > MAX_POSITION_AGE_SECONDS:
            raise AircraftLookupError(f"Aircraft position is stale ({age:.0f} seconds old).")
        provided_callsign = normalize_callsign(aircraft.get("callsign", ""))
        if provided_callsign != callsign:
            raise AircraftLookupError(f"Aircraft provider returned {provided_callsign} for requested callsign {callsign}.")
        try:
            observed_at = datetime.fromisoformat(str(aircraft["observed_at"]).replace("Z", "+00:00"))
        except (KeyError, ValueError):
            raise AircraftLookupError("Aircraft provider returned an invalid observed_at timestamp.") from None
        if observed_at.tzinfo is None:
            raise AircraftLookupError("Aircraft provider observed_at must include a UTC offset.")
        current_time = datetime.now(timezone.utc)
        elapsed = (current_time - observed_at).total_seconds()
        if elapsed < 0:
            raise AircraftLookupError("Aircraft provider observed_at is in the future.")
        actual_age = max(0.0, elapsed)
        if actual_age > MAX_POSITION_AGE_SECONDS:
            raise AircraftLookupError(f"Aircraft position is stale ({actual_age:.0f} seconds old).")
        # Allow the bounded provider request's processing time, but reject a
        # reported age that contradicts its timestamp. Canonicalize to the
        # more conservative age so the output fields describe one observation.
        if abs(age - actual_age) > 5:
            raise AircraftLookupError("Aircraft provider position age disagrees with observed_at.")
        age = max(age, actual_age)
        is_mock = aircraft.get("is_mock")
        if not isinstance(is_mock, bool):
            raise AircraftLookupError("Aircraft provider must explicitly label is_mock true or false.")
        source = str(aircraft.get("source") or "provider").strip()
        if source.lower() in {"mock", "fallback", "adsb.lol"}:
            source = source.lower()
        if source in {"mock", "fallback"} and not is_mock:
            raise AircraftLookupError("Aircraft provider source mock/fallback requires is_mock=true.")
        if source == "adsb.lol" and is_mock:
            raise AircraftLookupError("Aircraft provider source adsb.lol requires is_mock=false.")
        aircraft.update({
            "callsign": callsign, "icao_hex": aircraft.get("icao_hex"),
            "latitude": latitude, "longitude": longitude,
            "altitude_ft": _optional_number(aircraft.get("altitude_ft"), "altitude"),
            "ground_speed_kt": _optional_number(aircraft.get("ground_speed_kt"), "ground speed", minimum=0),
            "heading_deg": _optional_number(aircraft.get("heading_deg"), "heading", minimum=0, maximum=360),
            "squawk": str(aircraft["squawk"]) if aircraft.get("squawk") is not None else None,
            "observed_at": (current_time - timedelta(seconds=age)).isoformat(),
            "source": source, "is_mock": is_mock,
            "position_age_seconds": age,
        })
        warnings = result.get("warnings", [])
        if not isinstance(warnings, list) or any(not isinstance(warning, str) for warning in warnings):
            raise AircraftLookupError("Aircraft provider warnings must be a list of strings.")
        if is_mock and not warnings:
            warnings = ["Aircraft provider returned simulated aircraft data."]
        nearby = self._nearby(latitude, longitude)
        if result.get("nearby_airports") is not None:
            provider_airports = result["nearby_airports"]
            if not isinstance(provider_airports, list) or not provider_airports:
                raise AircraftLookupError("Aircraft provider nearby_airports must be a nonempty list.")
            nearby = []
            for airport in provider_airports:
                if not isinstance(airport, dict) or not airport.get("icao") or not airport.get("name"):
                    raise AircraftLookupError("Aircraft provider returned an invalid nearby airport.")
                airport_latitude = _number(airport.get("latitude"), "airport latitude", minimum=-90, maximum=90)
                airport_longitude = _number(airport.get("longitude"), "airport longitude", minimum=-180, maximum=180)
                nearby.append({
                    "icao": str(airport["icao"]), "name": str(airport["name"]),
                    "latitude": airport_latitude, "longitude": airport_longitude,
                    "distance_km": round(_distance_km(latitude, longitude, airport_latitude, airport_longitude), 2),
                })
            nearby = sorted(nearby, key=lambda airport: airport["distance_km"])[:5]
        return {"aircraft": aircraft, "nearby_airports": nearby, "warnings": warnings}

    async def _live(self, callsign: str) -> dict[str, Any]:
        response = await asyncio.wait_for(
            self.client.get(f"https://api.adsb.lol/v2/callsign/{callsign}", timeout=5.0),
            timeout=5.0,
        )
        response.raise_for_status()
        try:
            payload = response.json()
        except ValueError as exc:
            raise AircraftLookupError("ADSB.lol returned invalid JSON.") from exc
        if not isinstance(payload, dict):
            raise AircraftLookupError("ADSB.lol returned an invalid aircraft response.")
        candidates = payload.get("ac", payload.get("aircraft", []))
        if not isinstance(candidates, list):
            raise AircraftLookupError("ADSB.lol returned an invalid aircraft list.")
        candidates = [candidate for candidate in candidates if isinstance(candidate, dict)]
        matches = [candidate for candidate in candidates if str(candidate.get("flight", "")).strip().upper() == callsign]
        if not matches:
            raise AircraftLookupError(f"No live aircraft found for callsign {callsign}.")
        if len(matches) > 1:
            raise AircraftLookupError(f"Multiple aircraft match callsign {callsign}; aircraft identity needs clarification.")
        match = matches[0]
        latitude = _number(match.get("lat"), "latitude", minimum=-90, maximum=90)
        longitude = _number(match.get("lon"), "longitude", minimum=-180, maximum=180)
        age = _number(match.get("seen_pos"), "position age", minimum=0)
        current_time = datetime.now(timezone.utc)
        if payload.get("now") is not None:
            feed_time = _number(payload["now"], "feed timestamp", minimum=0)
            if feed_time > 100_000_000_000:
                feed_time /= 1000
            feed_delay = current_time.timestamp() - feed_time
            if feed_delay < -30:
                raise AircraftLookupError("ADSB.lol returned a future feed timestamp.")
            age += max(0.0, feed_delay)
        if age > MAX_POSITION_AGE_SECONDS:
            raise AircraftLookupError(f"Aircraft position is stale ({age:.0f} seconds old).")
        altitude = match.get("alt_baro", match.get("alt_geom"))
        if altitude == "ground":
            altitude = 0
        aircraft = {
            "callsign": callsign,
            "icao_hex": match.get("hex"),
            "latitude": latitude,
            "longitude": longitude,
            "altitude_ft": _optional_number(altitude, "altitude"),
            "ground_speed_kt": _optional_number(match.get("gs"), "ground speed", minimum=0),
            "heading_deg": _optional_number(match.get("track"), "heading", minimum=0, maximum=360),
            "squawk": str(match["squawk"]) if match.get("squawk") is not None else None,
            "observed_at": (current_time - timedelta(seconds=age)).isoformat(),
            "source": "adsb.lol",
            "is_mock": False,
            "position_age_seconds": age,
        }
        return {"aircraft": aircraft, "nearby_airports": self._nearby(latitude, longitude), "warnings": []}

    async def lookup(self, callsign: str) -> dict[str, Any]:
        normalized = normalize_callsign(callsign)
        if self.mode == "mock":
            return self._mock(normalized)
        try:
            if self.provider is not None:
                provided = self.provider(normalized)
                if not inspect.isawaitable(provided):
                    raise AircraftLookupError("Aircraft provider must be an async callable.")
                return self._validate_provider(await asyncio.wait_for(provided, timeout=5.0), normalized)
            return await self._live(normalized)
        except (httpx.HTTPError, AircraftLookupError, ValueError, TypeError, TimeoutError) as exc:
            if not self.allow_fallback:
                if isinstance(exc, AircraftLookupError):
                    raise
                raise AircraftLookupError(f"Aircraft lookup failed: {exc or 'lookup timed out after 5 seconds'}") from exc
            return self._mock(normalized, source="fallback", warning=f"Live aircraft lookup unavailable: {exc or 'lookup timed out after 5 seconds'}")
