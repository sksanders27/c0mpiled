"""In-memory call/incident bookkeeping. One process, no persistence (hackathon)."""
from __future__ import annotations

import asyncio
from dataclasses import dataclass, field


@dataclass
class PilotCall:
    call_id: str
    control_url: str | None = None
    incident_id: str | None = None
    reporting: bool = False  # report_emergency accepted, incident not created yet
    plan: str | None = None  # last plan briefed via brief_pilot
    plan_delivered: bool = False  # live call control accepted the final plan
    confirmed: bool = False
    confirming: bool = False  # prevent concurrent callbacks while backend decides
    ended: bool = False


@dataclass
class UnitCall:
    call_id: str
    incident_id: str
    unit_id: str
    unit_name: str
    attempt: int
    simulated: bool = False
    control_url: str | None = None
    decided: bool = False
    accepted: bool | None = None
    reason: str | None = None
    ended: bool = False
    timeout_task: asyncio.Task | None = None


@dataclass
class WebDispatch:
    """A unit call waiting for the browser at /unit?u=<slot> (UNIT_TRANSPORT=web)."""
    dispatch_id: str
    slot: int  # 1 = UNIT_PHONE_1's slot, 2 = UNIT_PHONE_2's, ...
    variables: dict
    created: float
    call_id: str | None = None  # Vapi web-call id once the browser binds


@dataclass
class Registry:
    pilot_calls: dict[str, PilotCall] = field(default_factory=dict)
    incident_to_pilot: dict[str, str] = field(default_factory=dict)
    unit_calls: dict[str, UnitCall] = field(default_factory=dict)
    outbound_count: dict[str, int] = field(default_factory=dict)  # incident_id -> unit calls placed
    tool_results: dict[str, str] = field(default_factory=dict)  # toolCallId -> result (idempotency)
    # Web transport: unit_calls holds each web UnitCall under its dispatch_id and, once bound, also
    # under its Vapi call id (same object), so timeouts, tools and status updates all find it.
    web_dispatches: dict[str, WebDispatch] = field(default_factory=dict)
    early_unit_control: dict[str, str] = field(default_factory=dict)  # call_id -> controlUrl seen before bind

    def reset(self) -> None:
        """Clear all call state in place (POST /dev/reset between rehearsals)."""
        for uc in self.unit_calls.values():
            if uc.timeout_task:
                uc.timeout_task.cancel()
        self.pilot_calls.clear()
        self.incident_to_pilot.clear()
        self.unit_calls.clear()
        self.outbound_count.clear()
        self.tool_results.clear()
        self.web_dispatches.clear()
        self.early_unit_control.clear()

    def pilot(self, call_id: str) -> PilotCall:
        if call_id not in self.pilot_calls:
            self.pilot_calls[call_id] = PilotCall(call_id)
        return self.pilot_calls[call_id]

    def bind_incident(self, call_id: str, incident_id: str) -> None:
        pc = self.pilot(call_id)
        pc.incident_id = incident_id
        pc.reporting = False
        self.incident_to_pilot[incident_id] = call_id

    def pilot_for_incident(self, incident_id: str) -> PilotCall | None:
        cid = self.incident_to_pilot.get(incident_id)
        return self.pilot_calls.get(cid) if cid else None

    def is_unit_call(self, call_id: str | None) -> bool:
        return bool(call_id) and call_id in self.unit_calls

    def note_control_url(self, call_id: str, control_url: str | None) -> None:
        if not control_url:
            return
        if call_id in self.unit_calls:
            self.unit_calls[call_id].control_url = control_url
        else:
            self.pilot(call_id).control_url = control_url

    def snapshot(self) -> dict:
        return {
            "pilot_calls": {k: {kk: vv for kk, vv in vars(v).items()} for k, v in self.pilot_calls.items()},
            "unit_calls": {
                k: {kk: vv for kk, vv in vars(v).items() if kk != "timeout_task"}
                for k, v in self.unit_calls.items() if k == v.call_id
            },
            "outbound_count": dict(self.outbound_count),
        }
