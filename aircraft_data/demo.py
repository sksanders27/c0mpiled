"""Command-line aircraft lookup and airport-cache warmup."""
from __future__ import annotations

import argparse
import asyncio
import json

import httpx

from .service import AircraftDataService


async def _run(args: argparse.Namespace) -> None:
    async with httpx.AsyncClient() as client:
        service = AircraftDataService(client)
        if args.warm_airports:
            print(f"Airport data source: {await service.warm_airports()}")
            if not args.callsign:
                return
        result = await service.get_aircraft_context(
            args.callsign,
            nearest_count=args.nearest_count,
            include_weather=args.weather,
            allow_sample_fallback=args.demo,
        )
        print(json.dumps(result, indent=2))


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("callsign", nargs="?", help="ICAO or spoken callsign")
    parser.add_argument("--warm-airports", action="store_true", help="download and cache OurAirports data")
    parser.add_argument("--nearest-count", type=int, default=5)
    parser.add_argument("--weather", action="store_true", help="include METAR for the nearest airport")
    parser.add_argument("--demo", action="store_true", help="allow clearly labeled sample aircraft when live lookup fails")
    args = parser.parse_args()
    if not args.callsign and not args.warm_airports:
        parser.error("provide a callsign or --warm-airports")
    asyncio.run(_run(args))


if __name__ == "__main__":
    main()
