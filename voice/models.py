"""Shared contract with Backend. Agreed up front; don't change field names."""
from __future__ import annotations

from enum import Enum

from pydantic import BaseModel


class EmergencyType(str, Enum):
    MEDICAL = "MEDICAL"
    FIRE_SMOKE = "FIRE_SMOKE"
    ENGINE = "ENGINE"
    FUEL = "FUEL"
    DEPRESSURIZATION = "DEPRESSURIZATION"
    SECURITY = "SECURITY"
    GEAR_STRUCTURAL = "GEAR_STRUCTURAL"
    OTHER = "OTHER"


class Severity(str, Enum):
    MAYDAY = "MAYDAY"
    PAN = "PAN"


class EmergencyReport(BaseModel):
    call_id: str
    callsign_raw: str  # as spoken
    callsign: str | None  # normalized ICAO, e.g. "UAL1234"
    emergency_type: EmergencyType
    severity: Severity
    souls_on_board: int | None = None
    fuel_minutes: int | None = None
    intentions: str | None = None
    summary: str  # one sentence


class UnitBrief(BaseModel):
    incident_id: str
    unit_id: str
    unit_name: str
    phone: str  # E.164
    callsign: str
    emergency_type: EmergencyType
    severity: Severity
    airport: str
    eta_min: int | None
    souls_on_board: int | None


SPOKEN_EMERGENCY = {
    EmergencyType.MEDICAL: "medical emergency",
    EmergencyType.FIRE_SMOKE: "fire or smoke",
    EmergencyType.ENGINE: "engine failure",
    EmergencyType.FUEL: "low fuel",
    EmergencyType.DEPRESSURIZATION: "depressurization",
    EmergencyType.SECURITY: "security threat",
    EmergencyType.GEAR_STRUCTURAL: "gear or structural problem",
    EmergencyType.OTHER: "emergency",
}

SPOKEN_AIRPORT = {
    "SAN": "San Diego",
    "LAX": "Los Angeles",
    "SFO": "San Francisco",
    "SJC": "San Jose",
    "OAK": "Oakland",
    "ONT": "Ontario",
    "SNA": "Santa Ana",
    "BUR": "Burbank",
    "LGB": "Long Beach",
    "PSP": "Palm Springs",
    "PHX": "Phoenix",
    "LAS": "Las Vegas",
    "SEA": "Seattle",
    "PDX": "Portland",
    "SLC": "Salt Lake City",
    "DEN": "Denver",
    "NKX": "Miramar",
    "NZY": "North Island",
    "MYF": "Montgomery Field",
    "CRQ": "Carlsbad",
    "SEE": "Gillespie Field",
    "SDM": "Brown Field",
    "RNM": "Ramona",
}


def spoken_airport(code: str) -> str:
    code = (code or "").strip().upper()
    if code.startswith("K") and len(code) == 4:
        code = code[1:]
    return SPOKEN_AIRPORT.get(code) or " ".join(code) or "the nearest airport"
