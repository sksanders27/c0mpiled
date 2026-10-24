"""Handlers for Vapi function-tool calls. Each returns a short string the assistant can say.

Handlers must return fast (Vapi waits on the webhook); slow work goes through service.spawn().
"""
from __future__ import annotations

import asyncio
import logging
from enum import Enum
from typing import TYPE_CHECKING, Awaitable, Callable

from .callsign import normalize_callsign
from .models import EmergencyReport, EmergencyType, Severity

if TYPE_CHECKING:
    from .ports import VoiceService

log = logging.getLogger("voice.tools")
CONFIRM_TIMEOUT_S = 1.5

_TYPE_SYNONYMS = {
    "FIRE": EmergencyType.FIRE_SMOKE, "SMOKE": EmergencyType.FIRE_SMOKE,
    "ENGINE_FAILURE": EmergencyType.ENGINE, "LOW_FUEL": EmergencyType.FUEL,
    "DEPRESSURISATION": EmergencyType.DEPRESSURIZATION, "PRESSURIZATION": EmergencyType.DEPRESSURIZATION,
    "GEAR": EmergencyType.GEAR_STRUCTURAL, "STRUCTURAL": EmergencyType.GEAR_STRUCTURAL,
}
_SEVERITY_SYNONYMS = {"PAN_PAN": Severity.PAN, "PANPAN": Severity.PAN, "URGENT": Severity.PAN, "DISTRESS": Severity.MAYDAY}


def _enum(cls: type[Enum], value, default, synonyms: dict):
    key = str(value or "").strip().upper().replace("-", "_").replace(" ", "_")
    try:
        return cls(key)
    except ValueError:
        return synonyms.get(key, default)


def _opt_int(value) -> int | None:
    try:
        return int(float(value)) if value not in (None, "") else None
    except (TypeError, ValueError):
        return None


def _opt_str(value) -> str | None:
    s = str(value).strip() if value is not None else ""
    return s or None


def build_report(call_id: str, args: dict) -> EmergencyReport:
    raw = _opt_str(args.get("callsign_raw")) or _opt_str(args.get("callsign")) or ""
    # Deterministic parse of what was spoken wins; the LLM's ICAO guess is only a fallback.
    callsign = normalize_callsign(raw) or normalize_callsign(_opt_str(args.get("callsign")))
    return EmergencyReport(
        call_id=call_id,
        callsign_raw=raw,
        callsign=callsign,
        emergency_type=_enum(EmergencyType, args.get("emergency_type"), EmergencyType.OTHER, _TYPE_SYNONYMS),
        # Missing severity is treated as distress: over-prioritising is the safe failure.
        severity=_enum(Severity, args.get("severity"), Severity.MAYDAY, _SEVERITY_SYNONYMS),
        souls_on_board=_opt_int(args.get("souls_on_board")),
        fuel_minutes=_opt_int(args.get("fuel_minutes")),
        intentions=_opt_str(args.get("intentions")),
        summary=_opt_str(args.get("summary")) or "Emergency reported.",
    )


async def report_emergency(service: "VoiceService", call_id: str, args: dict) -> str:
    pc = service.registry.pilot(call_id)
    if pc.incident_id or pc.reporting:
        service.emit("duplicate_report_ignored", call_id=call_id, incident_id=pc.incident_id)
        return "Already reported. Dispatch is coordinating, stand by."
    report = build_report(call_id, args)
    pc.reporting = True
    service.emit("report_received", call_id=call_id, report=report.model_dump(mode="json"))
    if report.callsign is None:
        log.warning("callsign not normalized: %r", report.callsign_raw)
    service.spawn(_create_incident(service, report), name=f"create_incident:{call_id}")
    return "Copy, coordinating now."


async def _create_incident(service: "VoiceService", report: EmergencyReport) -> None:
    try:
        incident_id = await service.dispatch.create_incident(report)
    except Exception as e:
        log.exception("create_incident failed")
        service.registry.pilot(report.call_id).reporting = False
        service.emit("dispatch_error", call_id=report.call_id, op="create_incident", error=str(e)[:300])
        return
    service.registry.bind_incident(report.call_id, incident_id)
    service.emit("incident_created", incident_id=incident_id, call_id=report.call_id, callsign=report.callsign,
                 emergency_type=report.emergency_type.value, severity=report.severity.value)


async def confirm_plan(service: "VoiceService", call_id: str, args: dict) -> str:
    pc = service.registry.pilot_calls.get(call_id)
    if pc is None or pc.incident_id is None:
        return "No emergency on file yet. Stand by."
    if pc.ended:
        return "The pilot call has ended. The plan has not been confirmed."
    if pc.plan is None or not pc.plan_delivered:
        return "No plan has been issued yet. Tell the pilot to stand by for dispatch."
    if pc.confirmed:
        return "Plan already confirmed."
    if pc.confirming:
        return "Confirmation is being recorded. Stand by."
    readback = _opt_str(args.get("readback")) or "wilco"
    pc.confirming = True
    try:
        # Same-process backend validation is authoritative. Bound the wait so
        # the webhook still responds promptly if the backend stops responding.
        await asyncio.wait_for(
            service.dispatch.on_pilot_confirm(pc.incident_id, readback),
            timeout=CONFIRM_TIMEOUT_S,
        )
    except Exception as e:
        log.error("dispatch.on_pilot_confirm failed: %s", e)
        service.emit("dispatch_error", incident_id=pc.incident_id,
                     op="on_pilot_confirm", error=str(e)[:300])
        return "The plan could not be confirmed. Stand by for dispatch."
    finally:
        pc.confirming = False
    pc.confirmed = True
    service.emit("pilot_confirmed", incident_id=pc.incident_id, call_id=call_id, readback=readback)
    return "Plan confirmed. Emergency services are standing by for your arrival. SkyWatch standing by."


async def _unit_answer(service: "VoiceService", call_id: str, accepted: bool, reason: str | None) -> str:
    uc = service.registry.unit_calls.get(call_id)
    if uc is None:
        return "No active assignment on this call. Say: Copy, dispatch out."
    if not service.unit_decision(call_id, accepted, reason):
        return "Already recorded. Say: Copy, dispatch out."
    service.spawn(service.hangup_unit_later(call_id), name=f"hangup:{call_id}")
    if accepted:
        return "Assignment confirmed. Say: Copy, dispatch out."
    return "Copy, unable. Say: Copy, dispatch out."


async def accept_assignment(service: "VoiceService", call_id: str, args: dict) -> str:
    return await _unit_answer(service, call_id, True, None)


async def decline_assignment(service: "VoiceService", call_id: str, args: dict) -> str:
    return await _unit_answer(service, call_id, False, _opt_str(args.get("reason")) or "unable")


HANDLERS: dict[str, Callable[["VoiceService", str, dict], Awaitable[str]]] = {
    "report_emergency": report_emergency,
    "confirm_plan": confirm_plan,
    "accept_assignment": accept_assignment,
    "decline_assignment": decline_assignment,
}


async def handle_tool(service: "VoiceService", name: str, args: dict, call: dict) -> str:
    handler = HANDLERS.get(name)
    if handler is None:
        log.warning("unknown tool %r", name)
        return f"Unknown tool {name}."
    return await handler(service, call.get("id") or "unknown-call", args)
