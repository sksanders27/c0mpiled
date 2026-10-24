"""Spoken callsign -> ICAO callsign.

    "United twelve thirty-four"           -> "UAL1234"
    "Speedbird two eight kilo heavy"      -> "BAW28K"
    "November one two three alpha bravo"  -> "N123AB"
    "UAL1234"                             -> "UAL1234" (pass-through)

Returns None whenever the input can't be parsed with confidence. Never guess.
"""
from __future__ import annotations

import re

# Telephony designator -> ICAO prefix. Multi-word telephonies are matched with the spaces removed.
AIRLINES = {
    "united": "UAL",
    "american": "AAL",
    "delta": "DAL",
    "southwest": "SWA",
    "alaska": "ASA",
    "jetblue": "JBU",
    "spirit": "NKS",
    "spiritwings": "NKS",
    "frontier": "FFT",
    "frontierflight": "FFT",
    "skywest": "SKW",
    "hawaiian": "HAL",
    "fedex": "FDX",
    "speedbird": "BAW",
    "allegiant": "AAY",
}
ICAO_TO_TELEPHONY = {
    "UAL": "United", "AAL": "American", "DAL": "Delta", "SWA": "Southwest", "ASA": "Alaska",
    "JBU": "JetBlue", "NKS": "Spirit Wings", "FFT": "Frontier Flight", "SKW": "SkyWest",
    "HAL": "Hawaiian", "FDX": "FedEx", "BAW": "Speedbird", "AAY": "Allegiant",
}
# US civil aircraft may use the manufacturer or model in place of "November".
N_PREFIXES = {"november", "cessna", "piper", "cirrus", "beechcraft", "bonanza", "mooney", "diamond", "skyhawk", "cherokee"}

UNITS = {
    "zero": 0, "oh": 0, "o": 0, "one": 1, "two": 2, "three": 3, "tree": 3, "four": 4, "fower": 4,
    "five": 5, "fife": 5, "six": 6, "seven": 7, "eight": 8, "nine": 9, "niner": 9,
}
TEENS = {
    "ten": 10, "eleven": 11, "twelve": 12, "thirteen": 13, "fourteen": 14, "fifteen": 15,
    "sixteen": 16, "seventeen": 17, "eighteen": 18, "nineteen": 19,
}
TENS = {"twenty": 20, "thirty": 30, "forty": 40, "fifty": 50, "sixty": 60, "seventy": 70, "eighty": 80, "ninety": 90}
NATO = {
    "alpha": "A", "alfa": "A", "bravo": "B", "charlie": "C", "delta": "D", "echo": "E", "foxtrot": "F",
    "golf": "G", "hotel": "H", "india": "I", "juliet": "J", "juliett": "J", "kilo": "K", "lima": "L",
    "mike": "M", "november": "N", "oscar": "O", "papa": "P", "quebec": "Q", "romeo": "R", "sierra": "S",
    "tango": "T", "uniform": "U", "victor": "V", "whiskey": "W", "xray": "X", "x-ray": "X",
    "yankee": "Y", "zulu": "Z",
}
FILLER_LEAD = {"mayday", "pan", "panpan", "this", "is", "uh", "um", "emergency", "calling", "skywatch"}
IGNORED = {"flight", "heavy", "super", "and"}

AIRLINE_ICAO_RE = re.compile(r"^[A-Z]{3}[1-9]\d{0,3}[A-Z]{0,2}$")
N_NUMBER_RE = re.compile(r"^N(?:[1-9]\d{0,4}|[1-9]\d{0,3}[A-HJ-NP-Z]|[1-9]\d{0,2}[A-HJ-NP-Z]{2})$")


def is_valid_icao(cs: str | None) -> bool:
    return bool(cs) and bool(AIRLINE_ICAO_RE.match(cs) or N_NUMBER_RE.match(cs))


def _tokens(raw: str) -> list[str]:
    s = raw.lower().replace("x-ray", "xray")
    s = re.sub(r"[-_/,.!?;:]", " ", s)
    return [t for t in s.split() if t]


def _small_number(toks: list[str], i: int) -> tuple[int | None, int]:
    """Parse a number word below 100 at toks[i]: 'seven', 'twelve', 'thirty four', '34'."""
    if i >= len(toks):
        return None, i
    t = toks[i]
    if t in UNITS:
        return UNITS[t], i + 1
    if t in TEENS:
        return TEENS[t], i + 1
    if t in TENS:
        if i + 1 < len(toks) and toks[i + 1] in UNITS and UNITS[toks[i + 1]] > 0:
            return TENS[t] + UNITS[toks[i + 1]], i + 2
        return TENS[t], i + 1
    return None, i


def _parse_tail(toks: list[str]) -> tuple[str, str] | None:
    """Parse the flight/registration part after the prefix into (digits, letters)."""
    digits = ""
    letters = ""
    i = 0
    while i < len(toks):
        t = toks[i]
        if t in IGNORED:
            i += 1
            continue
        # Letters only after digits, and nothing numeric after letters.
        if digits and t in NATO:
            letters += NATO[t]
            i += 1
            continue
        if letters:
            break
        m = re.fullmatch(r"(\d+)([a-z]{0,2})", t)
        if m:
            digits += m.group(1)
            letters += m.group(2).upper()
            i += 1
            continue
        if t in ("double", "triple") and i + 1 < len(toks) and toks[i + 1] in UNITS:
            digits += str(UNITS[toks[i + 1]]) * (2 if t == "double" else 3)
            i += 2
            continue
        val, j = _small_number(toks, i)
        if val is not None:
            if j < len(toks) and toks[j] in ("hundred", "thousand"):
                mult = 100 if toks[j] == "hundred" else 1000
                rest, k = _small_number(toks, j + 1 + (j + 1 < len(toks) and toks[j + 1] == "and"))
                val = val * mult + (rest or 0)
                j = k if rest is not None else j + 1
            digits += str(val)
            i = j
            continue
        break  # anything else ends the callsign ("... with an engine fire")
    if not digits:
        return None
    return digits, letters


def normalize_callsign(raw: str | None) -> str | None:
    if not raw or not raw.strip():
        return None
    compact = re.sub(r"[\s\-]", "", raw).upper()
    if is_valid_icao(compact):
        return compact

    toks = _tokens(raw)
    while toks and toks[0] in FILLER_LEAD:
        toks.pop(0)
    if not toks:
        return None

    prefix: str | None = None
    rest: list[str] = []
    two = toks[0] + toks[1] if len(toks) > 1 else None
    if two and two in AIRLINES:
        prefix, rest = AIRLINES[two], toks[2:]
    elif toks[0] in AIRLINES:
        prefix, rest = AIRLINES[toks[0]], toks[1:]
    elif toks[0] in N_PREFIXES:
        prefix, rest = "N", toks[1:]
    elif re.fullmatch(r"[a-z]{3}", toks[0]) and toks[0].upper() in ICAO_TO_TELEPHONY:
        prefix, rest = toks[0].upper(), toks[1:]
    if prefix is None:
        return None

    parsed = _parse_tail(rest)
    if parsed is None:
        return None
    digits, letters = parsed
    cs = f"{prefix}{digits}{letters}"
    return cs if is_valid_icao(cs) else None


def spoken_callsign(cs: str | None) -> str:
    """ICAO -> something a TTS voice says the aviation way: UAL1234 -> 'United 12 34'."""
    if not cs:
        return "unknown callsign"
    m = AIRLINE_ICAO_RE.match(cs)
    if m and cs[:3] in ICAO_TO_TELEPHONY:
        num = re.match(r"\d+", cs[3:]).group(0)
        suffix = " ".join(_NATO_WORD[c] for c in cs[3 + len(num):])
        if len(num) == 4:
            num = f"{num[:2]} {num[2:]}"
        elif len(num) == 3:
            num = f"{num[0]} {num[1:]}"
        return " ".join(p for p in (ICAO_TO_TELEPHONY[cs[:3]], num, suffix) if p)
    if N_NUMBER_RE.match(cs):
        return "November " + " ".join(c if c.isdigit() else _NATO_WORD[c] for c in cs[1:])
    return " ".join(cs)


_NATO_WORD = {
    "A": "Alpha", "B": "Bravo", "C": "Charlie", "D": "Delta", "E": "Echo", "F": "Foxtrot", "G": "Golf",
    "H": "Hotel", "I": "India", "J": "Juliett", "K": "Kilo", "L": "Lima", "M": "Mike", "N": "November",
    "O": "Oscar", "P": "Papa", "Q": "Quebec", "R": "Romeo", "S": "Sierra", "T": "Tango", "U": "Uniform",
    "V": "Victor", "W": "Whiskey", "X": "X-ray", "Y": "Yankee", "Z": "Zulu",
}
