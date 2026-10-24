"""Simulated units for SIM_UNITS=1: no real calls. Attempt 1 declines, later attempts accept."""
from __future__ import annotations

import asyncio
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from .ports import VoiceService


async def run_sim_unit(service: "VoiceService", call_id: str, attempt: int) -> None:
    await asyncio.sleep(service.settings.sim_unit_delay_s)
    uc = service.registry.unit_calls.get(call_id)
    if uc is None or uc.decided:
        return
    accepted = attempt >= 2
    reply = "Accept, responding." if accepted else "Unable, committed to another call."
    service.emit("transcript", leg="unit", call_id=call_id, incident_id=uc.incident_id, role="user",
                 text=reply, simulated=True)
    service.unit_decision(call_id, accepted, None if accepted else "committed to another call")
    uc.ended = True
    service.emit("call_status", leg="unit", call_id=call_id, incident_id=uc.incident_id, status="ended",
                 ended_reason="simulated", simulated=True)
