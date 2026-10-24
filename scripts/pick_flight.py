"""Pick a real airline flight that will stay inside the San Diego demo coverage for the whole call.

    .venv/bin/python scripts/pick_flight.py            # best candidates + the line to say
    .venv/bin/python scripts/pick_flight.py --check    # also confirm via the running app (/api/aircraft)

Prefers low, descending arrivals near KSAN: a cruising jet can be 100+ km away (e.g. over LAX) by the
time you dial, which puts the incident outside responder coverage and escalates it after one decline.
"""
from __future__ import annotations

import argparse
import json
import math
import sys
from pathlib import Path

import httpx

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from voice.callsign import ICAO_TO_TELEPHONY, normalize_callsign, spoken_callsign  # noqa: E402

SAN = (32.7336, -117.1897)
UA = {"User-Agent": "SkyWatch/0.2 (c0mpiled-16 hackathon; github.com/sksanders27/c0mpiled)"}
SD_AIRPORTS = {"KSAN", "KMYF", "KSEE", "KSDM", "KCRQ", "KRNM"}
NUMBER_WORDS = {
    "0": "zero", "1": "one", "2": "two", "3": "three", "4": "four", "5": "five",
    "6": "six", "7": "seven", "8": "eight", "9": "niner",
}


def km(a, b):
    la1, lo1, la2, lo2 = map(math.radians, (*a, *b))
    h = math.sin((la2 - la1) / 2) ** 2 + math.cos(la1) * math.cos(la2) * math.sin((lo2 - lo1) / 2) ** 2
    return 6371 * 2 * math.asin(math.sqrt(h))


def say_digits(cs: str) -> str:
    """'DAL1876' -> 'Delta one eight seven six' (digit by digit is the most robust for STT)."""
    prefix = ICAO_TO_TELEPHONY[cs[:3]]
    return prefix + " " + " ".join(NUMBER_WORDS.get(c, c) for c in cs[3:])


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--check", action="store_true", help="confirm with the running app at --base")
    ap.add_argument("--base", default="http://127.0.0.1:8000")
    args = ap.parse_args()

    data = httpx.get(f"https://api.adsb.lol/v2/point/{SAN[0]}/{SAN[1]}/40", headers=UA, timeout=15).json()
    rows = []
    for a in data.get("ac", []):
        cs = (a.get("flight") or "").strip()
        alt = a.get("alt_baro")
        if cs[:3] not in ICAO_TO_TELEPHONY or normalize_callsign(cs) != cs or not isinstance(alt, (int, float)):
            continue
        if not (1500 <= alt <= 15000) or a.get("lat") is None:
            continue
        dist = km(SAN, (a["lat"], a["lon"]))
        descending = (a.get("baro_rate") or 0) < -300
        # Lower score is better: close to SAN, descending arrivals strongly preferred.
        rows.append((dist - (40 if descending else 0), cs, alt, round(dist), descending))
    rows.sort()
    if not rows:
        sys.exit("No suitable airline flight near San Diego right now. Re-run in a minute, or let the "
                 "labeled fallback run (ALLOW_AIRCRAFT_FALLBACK=1) with a made-up callsign.")

    print(f"{'callsign':9s} {'alt ft':>7s} {'km to SAN':>9s}  descending")
    for _, cs, alt, dist, desc in rows[:6]:
        print(f"{cs:9s} {alt:7.0f} {dist:9d}  {'yes' if desc else 'no'}")

    best = None
    for _, cs, *_ in rows[:6]:
        if not args.check:
            best = cs
            break
        try:
            d = httpx.get(f"{args.base}/api/aircraft/{cs}", timeout=15).json()
        except httpx.HTTPError as e:
            sys.exit(f"Could not reach the app at {args.base}: {e}")
        nearest = (d.get("nearby_airports") or [{}])[0].get("icao")
        live = d.get("aircraft", {}).get("is_mock") is False
        print(f"  check {cs}: live={live} nearest={nearest}")
        if live and nearest in SD_AIRPORTS:
            best = cs
            break
    if not best:
        sys.exit("No candidate passed the app check; re-run in a minute.")

    print(f"\nUse {best}  ({spoken_callsign(best)}). Call within ~2 minutes and say:\n")
    print(f'  "Mayday mayday mayday, {say_digits(best)}, passenger in cardiac arrest, '
          f'one forty souls on board, forty-five minutes fuel, request San Diego."')
    print("\nReset first if you've called before:  curl -X POST localhost:8000/dev/reset")


if __name__ == "__main__":
    main()
