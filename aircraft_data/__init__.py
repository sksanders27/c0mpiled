"""Aircraft telemetry and nearby airport lookup for SkyWatch."""

from .router import build_aircraft_router
from .service import AircraftDataService, get_aircraft_context

__all__ = ["AircraftDataService", "build_aircraft_router", "get_aircraft_context"]
