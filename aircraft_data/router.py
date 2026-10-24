"""Optional FastAPI router for aircraft lookup."""
from __future__ import annotations

from fastapi import APIRouter, Query

from .service import AircraftDataService


def build_aircraft_router(service: AircraftDataService) -> APIRouter:
    router = APIRouter(prefix="/api/aircraft", tags=["aircraft"])

    @router.get("/{callsign}")
    async def aircraft_lookup(
        callsign: str,
        nearest_count: int = Query(5, ge=0, le=20),
        radius_nm: float = Query(150, gt=0, le=1000),
        include_weather: bool = False,
        demo_fallback: bool = False,
    ):
        return await service.get_aircraft_context(
            callsign,
            nearest_count=nearest_count,
            radius_nm=radius_nm,
            include_weather=include_weather,
            allow_sample_fallback=demo_fallback,
        )

    return router
