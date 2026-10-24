"""DispatchPort wrapper that enriches incidents without delaying voice intake."""
from __future__ import annotations

import asyncio
import logging
from typing import Any

from voice.callsign import normalize_callsign
from voice.models import EmergencyReport
from voice.ports import DispatchPort, now_iso

from .service import AircraftDataService

log = logging.getLogger(__name__)


class AircraftEnrichmentDispatch:
    """Delegate dispatch operations and look up aircraft in a tracked task."""

    def __init__(self, backend: DispatchPort, aircraft: AircraftDataService, *, allow_sample_fallback: bool = False):
        self.backend = backend
        self.aircraft = aircraft
        self.allow_sample_fallback = allow_sample_fallback
        self.contexts: dict[str, dict[str, Any]] = {}
        self.events: list[dict[str, Any]] = []
        self._tasks: set[asyncio.Task] = set()

    async def create_incident(self, report: EmergencyReport) -> str:
        incident_id = await self.backend.create_incident(report)
        task = asyncio.create_task(self._enrich(incident_id, report), name=f"aircraft_enrichment:{incident_id}")
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)
        return incident_id

    async def _enrich(self, incident_id: str, report: EmergencyReport) -> None:
        callsign = report.callsign or normalize_callsign(report.callsign_raw)
        try:
            context = await self.aircraft.get_aircraft_context(
                callsign,
                nearest_count=5,
                radius_nm=150,
                include_weather=False,
                allow_sample_fallback=self.allow_sample_fallback,
            )
        except Exception as exc:  # enrichment must never affect voice dispatch
            log.exception("Aircraft enrichment failed for %s", incident_id)
            context = {
                "status": "unavailable", "callsign": callsign, "aircraft": None,
                "nearby_airports": [], "airports_source": None,
                "warning": f"Aircraft lookup failed: {str(exc)[:200]}",
            }
        self.contexts[incident_id] = context
        event = {
            "ts": now_iso(),
            "kind": "aircraft_lookup_completed",
            "incident_id": incident_id,
            "callsign": context.get("callsign") or report.callsign_raw,
            "status": context["status"],
            "aircraft": context.get("aircraft"),
            "nearby_airports": context.get("nearby_airports", []),
            "simulated": bool((context.get("aircraft") or {}).get("simulated")),
            "warning": context.get("warning"),
        }
        await self.on_event(event)

    async def on_unit_response(self, incident_id: str, unit_id: str, accepted: bool, reason: str | None) -> None:
        await self.backend.on_unit_response(incident_id, unit_id, accepted, reason)

    async def on_pilot_confirm(self, incident_id: str, readback: str) -> None:
        await self.backend.on_pilot_confirm(incident_id, readback)

    async def on_event(self, event: dict) -> None:
        self.events.append(event)
        del self.events[:-1000]
        await self.backend.on_event(event)

    def snapshot(self) -> dict[str, Any]:
        incidents = {}
        for incident_id, context in self.contexts.items():
            incidents[incident_id] = {"aircraft_context": context}
        return {"incidents": incidents, "events": list(self.events)}
