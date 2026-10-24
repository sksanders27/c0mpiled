"""Best-effort aircraft telemetry and airport proximity data.

The service reports data and data quality only. It does not recommend a landing,
diversion, airport suitability, or emergency response.
"""
from __future__ import annotations

import csv
import json
import logging
import math
import os
import re
import tempfile
import time
from pathlib import Path
from typing import Any

import httpx

log = logging.getLogger(__name__)

ADSB_BASE = "https://api.adsb.lol/v2"
AIRPORTS_CSV_URL = "https://ourairports.com/data/airports.csv"
METAR_URL = "https://aviationweather.gov/api/data/metar"
DEFAULT_CACHE = Path(os.getenv("AIRPORTS_CACHE_PATH", ".cache/ourairports-airports.csv"))
MAX_POSITION_AGE_SECONDS = 120
EARTH_RADIUS_NM = 3440.065


def _as_float(value: Any) -> float | None:
    try:
        if value is None or isinstance(value, bool) or value == "ground":
            return None
        result = float(value)
        return result if math.isfinite(result) else None
    except (TypeError, ValueError):
        return None


def _as_int(value: Any) -> int | None:
    number = _as_float(value)
    return int(number) if number is not None else None


def _icao(value: str) -> str:
    return re.sub(r"[^A-Z0-9]", "", value.upper())


def _distance_nm(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dp = math.radians(lat2 - lat1)
    dl = math.radians(lon2 - lon1)
    a = math.sin(dp / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    return 2 * EARTH_RADIUS_NM * math.asin(min(1.0, math.sqrt(a)))


def _parse_airports(text: str) -> list[dict[str, Any]]:
    airports: list[dict[str, Any]] = []
    for row in csv.DictReader(text.splitlines()):
        if str(row.get("closed") or "").strip().lower() in {"1", "true", "yes"}:
            continue
        if row.get("type", "").strip().lower() in {"heliport", "seaplane_base", "closed"}:
            continue
        lat, lon = _as_float(row.get("latitude_deg")), _as_float(row.get("longitude_deg"))
        ident = (row.get("ident") or "").strip().upper()
        if not ident or lat is None or lon is None or not (-90 <= lat <= 90 and -180 <= lon <= 180):
            continue
        airports.append({
            "ident": ident,
            "iata": (row.get("iata_code") or "").strip().upper() or None,
            "icao": ident if len(ident) == 4 else None,
            "name": (row.get("name") or ident).strip(),
            "type": (row.get("type") or "").strip(),
            "latitude": lat,
            "longitude": lon,
        })
    return airports


class AircraftDataService:
    def __init__(
        self,
        client: httpx.AsyncClient,
        *,
        airport_cache: Path | str = DEFAULT_CACHE,
        sample_aircraft_path: Path | str | None = None,
        sample_airports_path: Path | str | None = None,
        max_position_age_seconds: int = MAX_POSITION_AGE_SECONDS,
        download_airports: bool = True,
    ):
        base = Path(__file__).parent
        self.client = client
        self.airport_cache = Path(airport_cache)
        self.sample_aircraft_path = Path(sample_aircraft_path or base / "sample_aircraft.json")
        self.sample_airports_path = Path(sample_airports_path or base / "sample_airports.csv")
        self.max_position_age_seconds = max_position_age_seconds
        self.download_airports = download_airports
        self._airports: list[dict[str, Any]] | None = None

    async def warm_airports(self) -> str:
        """Download/cache the public airport list. Return the data source used."""
        airports, source = await self._load_airports()
        self._airports = airports
        return source

    async def _load_airports(self) -> tuple[list[dict[str, Any]], str]:
        if self._airports is not None:
            return self._airports, "ourairports_cache"
        if self.airport_cache.exists():
            try:
                airports = _parse_airports(self.airport_cache.read_text(encoding="utf-8"))
                if airports:
                    self._airports = airports
                    return airports, "ourairports_cache"
            except OSError as exc:
                log.warning("Could not read airport cache: %s", exc)
        try:
            if not self.download_airports:
                raise RuntimeError("airport download disabled for this run")
            response = await self.client.get(AIRPORTS_CSV_URL, timeout=20)
            response.raise_for_status()
            airports = _parse_airports(response.text)
            if not airports:
                raise ValueError("OurAirports returned no usable airport rows")
            self.airport_cache.parent.mkdir(parents=True, exist_ok=True)
            fd, temp_name = tempfile.mkstemp(prefix="airports-", suffix=".csv", dir=self.airport_cache.parent)
            try:
                with os.fdopen(fd, "w", encoding="utf-8", newline="") as temp:
                    temp.write(response.text)
                os.replace(temp_name, self.airport_cache)
            finally:
                if os.path.exists(temp_name):
                    os.unlink(temp_name)
            self._airports = airports
            return airports, "ourairports_download"
        except (httpx.HTTPError, OSError, ValueError, RuntimeError) as exc:
            log.warning("OurAirports fetch failed; using bundled airport sample: %s", exc)
        try:
            airports = _parse_airports(self.sample_airports_path.read_text(encoding="utf-8"))
        except OSError:
            airports = []
        self._airports = airports
        return airports, "sample_airports"

    def _parse_aircraft(self, row: dict[str, Any], callsign: str, fetched_at: float) -> dict[str, Any]:
        age = _as_float(row.get("seen_pos"))
        lat, lon = _as_float(row.get("lat")), _as_float(row.get("lon"))
        stale = age is not None and age > self.max_position_age_seconds
        position_valid = lat is not None and lon is not None and -90 <= lat <= 90 and -180 <= lon <= 180
        result = {
            "callsign": (row.get("flight") or callsign).strip(),
            "icao24": (row.get("hex") or "").strip().lower() or None,
            "latitude": lat if position_valid and not stale else None,
            "longitude": lon if position_valid and not stale else None,
            "altitude_ft": _as_int(row.get("alt_baro")) if row.get("alt_baro") != "ground" else 0,
            "altitude_geom_ft": _as_int(row.get("alt_geom")),
            "speed_knots": _as_float(row.get("gs")),
            "ground_track_deg": _as_float(row.get("track")),
            "squawk": str(row["squawk"]).zfill(4) if row.get("squawk") not in (None, "") else None,
            "position_age_seconds": age,
            "on_ground": row.get("alt_baro") == "ground" or row.get("ground") is True,
            "simulated": False,
            "position_stale": stale,
            "last_seen_at_epoch": int(fetched_at - age) if age is not None else None,
            "api_now_epoch": _as_int(row.get("now")),
        }
        return result

    async def _live_aircraft(self, callsign: str) -> tuple[dict[str, Any] | None, str]:
        try:
            response = await self.client.get(f"{ADSB_BASE}/callsign/{callsign}", timeout=8)
            if response.status_code == 404:
                return None, "not_found"
            response.raise_for_status()
            payload = response.json()
            rows = payload.get("ac", []) if isinstance(payload, dict) else []
            if not isinstance(rows, list):
                return None, "unavailable"
            candidates = [row for row in rows if isinstance(row, dict) and _icao(str(row.get("flight") or "")) == callsign]
            if not candidates:
                return None, "not_found"
            # Prefer the freshest row, with usable coordinates breaking ties.
            row = min(candidates, key=lambda r: (_as_float(r.get("seen_pos")) if _as_float(r.get("seen_pos")) is not None else float("inf"), _as_float(r.get("lat")) is None))
            aircraft = self._parse_aircraft(row, callsign, time.time())
            if aircraft["position_stale"] or aircraft["latitude"] is None:
                return aircraft, "position_unavailable"
            return aircraft, "live"
        except (httpx.HTTPError, ValueError, TypeError, json.JSONDecodeError) as exc:
            log.warning("ADSB lookup failed for %s: %s", callsign, exc)
            return None, "unavailable"

    def _sample_aircraft(self, callsign: str) -> dict[str, Any] | None:
        try:
            payload = json.loads(self.sample_aircraft_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return None
        rows = payload if isinstance(payload, list) else [payload]
        for item in rows:
            if not isinstance(item, dict):
                continue
            aircraft = item.get("aircraft", item)
            if not isinstance(aircraft, dict):
                continue
            result = dict(aircraft)
            result["callsign"] = callsign
            result["simulated"] = True
            result["position_stale"] = False
            return result
        return None

    async def get_aircraft_context(
        self,
        callsign: str | None,
        *,
        nearest_count: int = 5,
        radius_nm: float = 150,
        include_weather: bool = False,
        allow_sample_fallback: bool = False,
    ) -> dict[str, Any]:
        normalized = ""
        if callsign:
            try:
                from voice.callsign import is_valid_icao, normalize_callsign
                normalized = normalize_callsign(callsign) or ""
                if not normalized and not any(character.isspace() for character in callsign):
                    compact = _icao(callsign)
                    if is_valid_icao(compact):
                        normalized = compact
            except ImportError:
                normalized = ""
        if not normalized:
            return {"status": "unavailable", "callsign": callsign, "aircraft": None,
                    "nearby_airports": [], "airports_source": None, "warning": "No usable callsign was provided."}
        aircraft, status = await self._live_aircraft(normalized)
        if status == "unavailable" and allow_sample_fallback:
            aircraft = self._sample_aircraft(normalized)
            if aircraft:
                status = "sample"
        airports: list[dict[str, Any]] = []
        source = None
        if aircraft and status in {"live", "sample"} and aircraft.get("latitude") is not None and aircraft.get("longitude") is not None:
            airport_rows, source = await self._load_airports()
            for airport in airport_rows:
                distance = _distance_nm(aircraft["latitude"], aircraft["longitude"], airport["latitude"], airport["longitude"])
                if distance <= radius_nm:
                    airports.append({**airport, "distance_nm": round(distance, 1)})
            airports.sort(key=lambda item: item["distance_nm"])
            airports = airports[:max(0, nearest_count)]
            if include_weather and airports:
                airports[0]["metar"] = await self._metar(airports[0].get("icao") or airports[0]["ident"])
        result = {"status": status, "callsign": normalized, "aircraft": aircraft,
                  "nearby_airports": airports, "airports_source": source}
        if status == "unavailable":
            result["warning"] = "Live aircraft lookup failed; no live position is available."
        elif status == "not_found":
            result["warning"] = "No matching aircraft was found in the live feed."
        elif status == "position_unavailable":
            result["warning"] = "Aircraft matched, but its position is missing or older than the freshness limit."
        elif status == "sample":
            result["warning"] = "Simulated sample data; this is not a live aircraft position."
        return result

    async def _metar(self, ident: str) -> dict[str, Any] | None:
        try:
            response = await self.client.get(METAR_URL, params={"ids": ident, "format": "json"}, timeout=5)
            response.raise_for_status()
            payload = response.json()
            return payload[0] if isinstance(payload, list) and payload else None
        except (httpx.HTTPError, ValueError, TypeError) as exc:
            log.warning("METAR lookup failed for %s: %s", ident, exc)
            return None


async def get_aircraft_context(callsign: str | None, *, client: httpx.AsyncClient | None = None, **kwargs) -> dict[str, Any]:
    """Convenience function; pass a shared client in server applications."""
    if client is not None:
        return await AircraftDataService(client).get_aircraft_context(callsign, **kwargs)
    async with httpx.AsyncClient() as owned_client:
        return await AircraftDataService(owned_client).get_aircraft_context(callsign, **kwargs)
