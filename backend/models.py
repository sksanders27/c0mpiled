"""Dashboard models wrap, and do not rename, the agreed voice contract."""

from datetime import datetime, timezone
from enum import StrEnum
from typing import Any, Literal
from uuid import uuid4

from pydantic import BaseModel, Field
from voice.models import EmergencyReport


def utcnow():
    return datetime.now(timezone.utc)


def new_id(prefix):
    return f"{prefix}_{uuid4().hex[:12]}"


class IncidentStatus(StrEnum):
    REPORTED = "REPORTED"
    LOCATED = "LOCATED"
    DISPATCHER_ASSIGNED = "DISPATCHER_ASSIGNED"
    UNIT_PENDING = "UNIT_PENDING"
    UNIT_ACCEPTED = "UNIT_ACCEPTED"
    PILOT_BRIEFED = "PILOT_BRIEFED"
    CONFIRMED = "CONFIRMED"
    ESCALATED = "ESCALATED"
    NEEDS_CLARIFICATION = "NEEDS_CLARIFICATION"
    FAILED = "FAILED"
    CANCELLED = "CANCELLED"
    RESOLVED = "RESOLVED"


class UnitType(StrEnum):
    FIRE = "FIRE"
    AMBULANCE = "AMBULANCE"
    POLICE = "POLICE"


class Aircraft(BaseModel):
    callsign: str
    icao_hex: str | None = None
    latitude: float = Field(ge=-90, le=90)
    longitude: float = Field(ge=-180, le=180)
    altitude_ft: float | None = None
    ground_speed_kt: float | None = Field(default=None, ge=0)
    heading_deg: float | None = Field(default=None, ge=0, le=360)
    squawk: str | None = None
    observed_at: datetime
    source: str
    is_mock: bool
    position_age_seconds: float = Field(ge=0)


class Airport(BaseModel):
    icao: str
    name: str
    latitude: float = Field(ge=-90, le=90)
    longitude: float = Field(ge=-180, le=180)
    distance_km: float = Field(default=0, ge=0)


class AircraftContext(BaseModel):
    aircraft: Aircraft
    nearby_airports: list[Airport] = Field(min_length=1)
    warnings: list[str] = Field(default_factory=list)


class Dispatcher(BaseModel):
    id: str
    name: str
    airport_icao: str
    latitude: float
    longitude: float
    simulated: bool = True


class Unit(BaseModel):
    id: str
    name: str
    unit_type: UnitType
    capabilities: list[str]
    latitude: float
    longitude: float
    status: Literal["AVAILABLE", "RESERVED", "ASSIGNED"] = "AVAILABLE"
    incident_id: str | None = None
    simulated: bool = True


class Assignment(BaseModel):
    id: str = Field(default_factory=lambda: new_id("asg"))
    unit_id: str
    unit_name: str
    unit_type: UnitType
    status: Literal["PENDING", "ACCEPTED", "DECLINED", "TIMED_OUT", "CANCELLED"] = "PENDING"
    reason: str | None = None
    created_at: datetime = Field(default_factory=utcnow)
    deadline: datetime
    responded_at: datetime | None = None
    distance_km: float
    simulated: bool = True


class Incident(BaseModel):
    id: str = Field(default_factory=lambda: new_id("inc"))
    callsign: str | None
    report: EmergencyReport
    status: IncidentStatus = IncidentStatus.REPORTED
    version: int = 0
    created_at: datetime = Field(default_factory=utcnow)
    updated_at: datetime = Field(default_factory=utcnow)
    closed_at: datetime | None = None
    aircraft: Aircraft | None = None
    nearby_airports: list[Airport] = Field(default_factory=list)
    selected_airport: Airport | None = None
    destination_basis: str | None = None
    dispatcher: Dispatcher | None = None
    required_unit_types: list[UnitType] = Field(default_factory=list)
    assignments: list[Assignment] = Field(default_factory=list)
    warnings: list[str] = Field(default_factory=list)
    plan: str | None = None
    plan_version: int = 0
    plan_delivered: bool = False
    pilot_confirmed: bool = False
    pilot_readback: str | None = None
    simulated_emergency: bool = True


class DispatchEvent(BaseModel):
    event_id: str = Field(default_factory=lambda: new_id("evt"))
    sequence: int
    timestamp: datetime = Field(default_factory=utcnow)
    type: str
    kind: str
    incident_id: str | None
    incident_version: int | None = None
    message: str
    data: dict[str, Any] = Field(default_factory=dict)
