"""Integration ports with Backend.

- DispatchPort: what Voice calls on Backend (Backend implements it; MockDispatch stands in).
- VoicePort:    what Backend calls on Voice. VoiceService implements it.
"""
from __future__ import annotations

import asyncio
import json
import logging
import re
import uuid
from datetime import datetime, timezone
from typing import Awaitable, Protocol

from .assistants import UNIT_FIRST_MESSAGE
from .callsign import is_valid_icao, spoken_callsign
from .config import Settings, load_vapi_ids
from .models import SPOKEN_EMERGENCY, EmergencyReport, Severity, UnitBrief, spoken_airport
from .registry import Registry, UnitCall, WebDispatch
from .vapi_client import VapiClient

log = logging.getLogger("voice")

SCRIPTED_DECLINE_DIRECTIVE = (
    "DEMO INSTRUCTION: this unit is already committed to another call. Whatever the unit says, "
    "call decline_assignment with reason 'committed to another call', then say 'Copy, dispatch out.'"
)


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds")


class DispatchPort(Protocol):
    async def create_incident(self, report: EmergencyReport) -> str: ...
    async def on_unit_response(self, incident_id: str, unit_id: str, accepted: bool, reason: str | None) -> None: ...
    async def on_pilot_confirm(self, incident_id: str, readback: str) -> None: ...
    async def on_event(self, event: dict) -> None: ...


class VoicePort(Protocol):
    async def call_unit(self, brief: UnitBrief) -> None: ...
    async def brief_pilot(self, incident_id: str, text: str) -> None: ...
    async def update_pilot(self, incident_id: str, text: str) -> None: ...


class VoiceService:
    """VoicePort implementation + shared plumbing used by the router and tool handlers."""

    def __init__(
        self,
        settings: Settings,
        vapi: VapiClient,
        registry: Registry | None = None,
        dispatch: DispatchPort | None = None,
        unit_assistant_id: str | None = None,
    ):
        self.settings = settings
        self.vapi = vapi
        self.registry = registry or Registry()
        self.dispatch = dispatch
        self.unit_assistant_id = unit_assistant_id or load_vapi_ids().get("unit_assistant_id")
        self._tasks: set[asyncio.Task] = set()
        # Per pilot call: serialize `say` so back-to-back updates don't run together.
        self._say_locks: dict[str, asyncio.Lock] = {}
        self._say_free_at: dict[str, float] = {}  # loop time when the last message should have finished
        self.pilot_ended_hooks: list = []  # callables(call_id), run once when a pilot call ends
        settings.log_dir.mkdir(parents=True, exist_ok=True)

    def bind_dispatch(self, dispatch: DispatchPort) -> None:
        self.dispatch = dispatch

    async def aclose(self) -> None:
        """Cancel and await Voice-owned work before the caller closes Vapi.

        This does not place calls or close the injected Vapi client. Its owner
        must close that client after this method returns.
        """
        current = asyncio.current_task()
        while pending := [task for task in self._tasks if task is not current]:
            for task in pending:
                task.cancel()
            await asyncio.gather(*pending, return_exceptions=True)
            # Already-finished tasks can make gather return without yielding;
            # do not depend on their scheduled done callbacks to clear the set.
            self._tasks.difference_update(pending)

    # --- plumbing -------------------------------------------------------------
    def spawn(self, coro: Awaitable, name: str | None = None) -> asyncio.Task:
        t = asyncio.create_task(coro, name=name)
        self._tasks.add(t)
        t.add_done_callback(self._task_done)
        return t

    def _task_done(self, t: asyncio.Task) -> None:
        self._tasks.discard(t)
        if not t.cancelled() and t.exception() is not None:
            log.error("background task %s failed", t.get_name(), exc_info=t.exception())

    def emit(self, kind: str, **data) -> dict:
        """Timestamped state change: logged, appended to logs/events.jsonl, forwarded to Backend."""
        event = {"ts": now_iso(), "kind": kind, **data}
        line = json.dumps(event, default=str)
        log.info("EVENT %s", line)
        try:
            with open(self.settings.log_dir / "events.jsonl", "a") as f:
                f.write(line + "\n")
        except OSError as e:
            log.warning("could not write events.jsonl: %s", e)
        if self.dispatch is not None:
            self.spawn(self._forward_event(event), name=f"on_event:{kind}")
        return event

    async def _forward_event(self, event: dict) -> None:
        try:
            await self.dispatch.on_event(event)
        except Exception as e:  # fire-and-forget; never let the map break a call
            log.warning("dispatch.on_event failed: %s", e)

    def pilot_call_ended(self, call_id: str) -> None:
        pc = self.registry.pilot_calls.get(call_id)
        self.emit("pilot_call_ended", call_id=call_id, incident_id=pc.incident_id if pc else None)
        for hook in list(self.pilot_ended_hooks):
            try:
                hook(call_id)
            except Exception:
                log.exception("pilot_ended hook failed")

    def is_unit_call(self, call: dict) -> bool:
        cid = call.get("id")
        if self.registry.is_unit_call(cid):
            return True
        return bool(self.unit_assistant_id) and call.get("assistantId") == self.unit_assistant_id

    # --- VoicePort: units -----------------------------------------------------
    def unit_variables(self, brief: UnitBrief, attempt: int) -> dict[str, str]:
        directive = SCRIPTED_DECLINE_DIRECTIVE if (self.settings.scripted_decline_first and attempt == 1) else ""
        return {
            "unit_name": brief.unit_name,
            "callsign": spoken_callsign(brief.callsign) if is_valid_icao(brief.callsign) else brief.callsign,
            "emergency_type": SPOKEN_EMERGENCY[brief.emergency_type],
            "severity": "Mayday" if brief.severity == Severity.MAYDAY else "Pan-pan",
            "airport": spoken_airport(brief.airport),
            "eta_min": str(brief.eta_min) if brief.eta_min is not None else "several",
            "souls": str(brief.souls_on_board) if brief.souls_on_board is not None else "an unknown number of",
            "demo_directive": directive,
        }

    @staticmethod
    def render(template: str, variables: dict[str, str]) -> str:
        return re.sub(r"\{\{\s*(\w+)\s*\}\}", lambda m: variables.get(m.group(1), ""), template)

    async def call_unit(self, brief: UnitBrief) -> None:
        iid = brief.incident_id
        placed = self.registry.outbound_count.get(iid, 0)
        if placed >= self.settings.max_outbound_calls:
            self.emit("escalate_human", incident_id=iid, reason="max_outbound_calls", attempts=placed)
            await self.update_pilot(iid, "Stand by. Dispatch is escalating to a human coordinator.")
            return
        attempt = placed + 1
        self.registry.outbound_count[iid] = attempt
        variables = self.unit_variables(brief, attempt)
        base = dict(incident_id=iid, unit_id=brief.unit_id, unit_name=brief.unit_name, attempt=attempt)

        if self.settings.sim_units:
            from .sim import run_sim_unit

            call_id = f"sim-{iid}-{attempt}"
            self.registry.unit_calls[call_id] = UnitCall(
                call_id, iid, brief.unit_id, brief.unit_name, attempt, simulated=True
            )
            self.emit("unit_call_started", call_id=call_id, simulated=True, **base)
            self.emit("transcript", leg="unit", call_id=call_id, incident_id=iid, role="assistant",
                      text=self.render(UNIT_FIRST_MESSAGE, variables), simulated=True)
            self.spawn(run_sim_unit(self, call_id, attempt), name=f"sim:{call_id}")
            return

        # Unsupervised guards: only dial allow-listed numbers, and only with a configured assistant.
        web = self.settings.unit_transport == "web"
        blocked = None
        if brief.phone not in self.settings.unit_phones:
            blocked = "phone_not_allowlisted"
        elif not self.unit_assistant_id:
            blocked = "unit_assistant_missing"
        elif not web and not self.settings.vapi_phone_number_id:
            blocked = "phone_number_id_missing"
        if blocked:
            log.error("NOT dialing unit %s: %s", brief.unit_id, blocked)
            self.emit("unit_call_blocked", reason=blocked, **base)
            self.spawn(self._report_unit(iid, brief.unit_id, False, blocked))
            return

        if web:
            self._queue_web_unit(brief, attempt, variables, base)
            return

        try:
            call = await self.vapi.create_call(
                self.unit_assistant_id, self.settings.vapi_phone_number_id, brief.phone, variables
            )
        except Exception as e:
            log.error("outbound unit call failed: %s", e)
            self.emit("unit_call_failed", error=str(e)[:300], **base)
            self.spawn(self._report_unit(iid, brief.unit_id, False, "dial_failed"))
            return

        call_id = call["id"]
        uc = UnitCall(call_id, iid, brief.unit_id, brief.unit_name, attempt,
                      control_url=(call.get("monitor") or {}).get("controlUrl"))
        self.registry.unit_calls[call_id] = uc
        self.emit("unit_call_started", call_id=call_id, simulated=False, **base)
        uc.timeout_task = self.spawn(self._unit_timeout(call_id), name=f"timeout:{call_id}")

    # --- web transport (UNIT_TRANSPORT=web) ------------------------------------
    def _queue_web_unit(self, brief: UnitBrief, attempt: int, variables: dict, base: dict) -> None:
        """Instead of dialing, park the dispatch for the browser on duty in this phone's slot."""
        slot = self.settings.unit_phones.index(brief.phone) + 1
        dispatch_id = f"wd-{uuid.uuid4().hex[:12]}"
        # dispatch_id rides along in variableValues so a webhook can bind even if /unit/bind is late.
        variables = {**variables, "dispatch_id": dispatch_id}
        uc = UnitCall(dispatch_id, brief.incident_id, brief.unit_id, brief.unit_name, attempt)
        self.registry.unit_calls[dispatch_id] = uc
        self.registry.web_dispatches[dispatch_id] = WebDispatch(
            dispatch_id, slot, variables, asyncio.get_running_loop().time())
        self.emit("unit_call_started", call_id=dispatch_id, simulated=False, transport="web", slot=slot, **base)
        self.emit("transcript", leg="unit", call_id=dispatch_id, incident_id=brief.incident_id, role="assistant",
                  text=self.render(UNIT_FIRST_MESSAGE, variables))
        uc.timeout_task = self.spawn(self._unit_timeout(dispatch_id), name=f"timeout:{dispatch_id}")

    def web_pending(self, slot: int) -> dict | None:
        """Oldest undecided, unclaimed dispatch for this slot (polled by /unit?u=<slot>)."""
        for wd in sorted(self.registry.web_dispatches.values(), key=lambda w: w.created):
            uc = self.registry.unit_calls.get(wd.dispatch_id)
            if wd.slot == slot and wd.call_id is None and uc is not None and not uc.decided:
                return {"dispatch_id": wd.dispatch_id, "assistantId": self.unit_assistant_id,
                        "unit_name": uc.unit_name, "variableValues": wd.variables}
        return None

    def web_bind(self, dispatch_id: str, call_id: str) -> str:
        """Map the browser's Vapi web call to its dispatch. Returns ok | expired | unknown | conflict."""
        wd = self.registry.web_dispatches.get(dispatch_id)
        uc = self.registry.unit_calls.get(dispatch_id)
        if wd is None or uc is None:
            return "unknown"
        if wd.call_id == call_id:
            return "ok"
        if wd.call_id is not None:
            return "conflict"
        if uc.decided:
            return "expired"
        wd.call_id = uc.call_id = call_id
        self.registry.unit_calls[call_id] = uc
        uc.control_url = uc.control_url or self.registry.early_unit_control.pop(call_id, None)
        self.emit("unit_call_connected", incident_id=uc.incident_id, unit_id=uc.unit_id, unit_name=uc.unit_name,
                  call_id=call_id, dispatch_id=dispatch_id, transport="web", slot=wd.slot)
        return "ok"

    async def _unit_timeout(self, call_id: str) -> None:
        await asyncio.sleep(self.settings.unit_call_timeout_s)
        uc = self.registry.unit_calls.get(call_id)
        if uc is None or uc.decided:
            return
        self.emit("unit_call_timeout", incident_id=uc.incident_id, unit_id=uc.unit_id, call_id=call_id,
                  timeout_s=self.settings.unit_call_timeout_s)
        self.unit_decision(call_id, False, "no_answer")
        if uc.control_url and not uc.ended:
            await self.vapi.end_call(uc.control_url)

    def unit_decision(self, call_id: str, accepted: bool, reason: str | None) -> bool:
        """Record a unit's accept/decline exactly once and report it to Backend."""
        uc = self.registry.unit_calls.get(call_id)
        if uc is None or uc.decided:
            return False
        uc.decided, uc.accepted, uc.reason = True, accepted, reason
        if uc.timeout_task and uc.timeout_task is not asyncio.current_task():
            uc.timeout_task.cancel()
        self.emit("unit_accepted" if accepted else "unit_declined", incident_id=uc.incident_id,
                  unit_id=uc.unit_id, unit_name=uc.unit_name, call_id=call_id, reason=reason)
        self.spawn(self._report_unit(uc.incident_id, uc.unit_id, accepted, reason))
        return True

    async def _report_unit(self, incident_id: str, unit_id: str, accepted: bool, reason: str | None) -> None:
        try:
            await self.dispatch.on_unit_response(incident_id, unit_id, accepted, reason)
        except Exception as e:
            log.error("dispatch.on_unit_response failed: %s", e)
            self.emit("dispatch_error", incident_id=incident_id, op="on_unit_response", error=str(e)[:300])

    async def hangup_unit_later(self, call_id: str, delay_s: float = 8.0) -> None:
        """Backstop in case the unit assistant doesn't end the call itself."""
        await asyncio.sleep(delay_s)
        uc = self.registry.unit_calls.get(call_id)
        if uc and uc.control_url and not uc.ended:
            await self.vapi.end_call(uc.control_url)

    # --- VoicePort: pilot -----------------------------------------------------
    async def brief_pilot(self, incident_id: str, text: str) -> None:
        pc = await self._wait_pilot(incident_id)
        if pc is None:
            self.emit("pilot_control_failed", incident_id=incident_id, reason="no_pilot_call", text=text)
            return
        pc.plan = text
        pc.plan_delivered = False
        pc.confirmed = False
        ok = await self._speak(
            pc.call_id,
            incident_id,
            spoken=f"{text} Read back the plan, or say wilco.",
            note=(f"Dispatch update (already spoken to the pilot): {text} "
                  "Wait for the pilot to affirm, then call confirm_plan with their readback."),
        )
        pc.plan_delivered = ok and not pc.ended
        self.emit("pilot_briefed", incident_id=incident_id, call_id=pc.call_id, text=text,
                  delivered=pc.plan_delivered)

    async def update_pilot(self, incident_id: str, text: str) -> None:
        pc = await self._wait_pilot(incident_id)
        if pc is None:
            self.emit("pilot_control_failed", incident_id=incident_id, reason="no_pilot_call", text=text)
            return
        ok = await self._speak(
            pc.call_id,
            incident_id,
            spoken=text,
            note=f"Dispatch update (interim, already spoken to the pilot, no readback needed): {text}",
        )
        self.emit("pilot_updated", incident_id=incident_id, call_id=pc.call_id, text=text, delivered=ok)

    async def _wait_pilot(self, incident_id: str, timeout_s: float = 5.0):
        # Backend may call us before create_incident() has returned to us.
        loop = asyncio.get_running_loop()
        deadline = loop.time() + timeout_s
        while True:
            pc = self.registry.pilot_for_incident(incident_id)
            if pc is not None or loop.time() >= deadline:
                return pc
            await asyncio.sleep(0.1)

    SECONDS_PER_WORD = 0.35  # rough TTS pace; `say` returns when queued, not when spoken

    async def _speak(self, call_id: str, incident_id: str | None, spoken: str, note: str) -> bool:
        lock = self._say_locks.setdefault(call_id, asyncio.Lock())
        async with lock:
            loop = asyncio.get_running_loop()
            wait = self._say_free_at.get(call_id, 0.0) - loop.time()
            if wait > 0:
                await asyncio.sleep(wait)
            ok = await self._speak_now(call_id, incident_id, spoken, note)
            if ok and self.settings.pilot_say_gap_s > 0:
                speech = min(10.0, len(spoken.split()) * self.SECONDS_PER_WORD)
                self._say_free_at[call_id] = loop.time() + speech + self.settings.pilot_say_gap_s
            return ok

    async def _speak_now(self, call_id: str, incident_id: str | None, spoken: str, note: str) -> bool:
        pc = self.registry.pilot(call_id)
        if pc.ended:
            self.emit("pilot_control_failed", incident_id=incident_id, call_id=call_id, reason="call_ended")
            return False
        url = pc.control_url or await self._fetch_control_url(call_id)
        if not url:
            log.error("!!! no controlUrl for pilot call %s; cannot speak to pilot", call_id)
            self.emit("pilot_control_failed", incident_id=incident_id, call_id=call_id, reason="no_control_url")
            return False
        pc.control_url = url
        if not await self.vapi.add_message(url, note):
            log.warning("add-message failed for %s (continuing with say)", call_id)
        if await self.vapi.say(url, spoken):
            return True
        log.error("!!! LIVE CALL CONTROL FAILED TWICE for pilot call %s; backend should fall back", call_id)
        self.emit("pilot_control_failed", incident_id=incident_id, call_id=call_id, reason="say_failed", text=spoken)
        return False

    async def _fetch_control_url(self, call_id: str) -> str | None:
        try:
            call = await self.vapi.get_call(call_id)
        except Exception as e:
            log.warning("GET /call/%s failed: %s", call_id, e)
            return None
        return (call.get("monitor") or {}).get("controlUrl")


class MockDispatch:
    """Stands in for Backend: picks SAN, assigns fake units in order, reassigns on decline."""

    FAKE_UNITS = [
        ("unit-1", "ARFF Crash Four"),
        ("unit-2", "ARFF Rescue Twelve"),
        ("unit-3", "Medic Three"),
        ("unit-4", "Engine Seven"),
        ("unit-5", "Engine Nine"),
    ]

    def __init__(self, voice: VoicePort, settings: Settings):
        self.voice = voice
        self.settings = settings
        self.incidents: dict[str, dict] = {}
        self.confirmed: dict[str, str] = {}
        self.events: list[dict] = []
        self._tasks: set[asyncio.Task] = set()

    async def create_incident(self, report: EmergencyReport) -> str:
        iid = f"INC-{uuid.uuid4().hex[:6].upper()}"
        self.incidents[iid] = {"report": report, "next": 0, "unit": None}
        log.info("[mock] incident %s for %s (%s)", iid, report.callsign, report.emergency_type.value)
        t = asyncio.create_task(self._start(iid))
        self._tasks.add(t)
        t.add_done_callback(self._tasks.discard)
        return iid

    async def _start(self, iid: str) -> None:
        await asyncio.sleep(self.settings.mock_dispatch_delay_s)
        self.events.append({"ts": now_iso(), "kind": "airport_selected", "incident_id": iid, "airport": "SAN"})
        log.info("[mock] %s airport SAN", iid)
        await self._dispatch_next(iid)

    async def _dispatch_next(self, iid: str) -> None:
        inc = self.incidents[iid]
        i = inc["next"]
        if i >= len(self.FAKE_UNITS):
            await self.voice.update_pilot(iid, "Stand by. No units available, escalating to a human coordinator.")
            return
        inc["next"] = i + 1
        unit_id, unit_name = self.FAKE_UNITS[i]
        r: EmergencyReport = inc["report"]
        brief = UnitBrief(
            incident_id=iid, unit_id=unit_id, unit_name=unit_name, phone=self.settings.unit_phone(i + 1),
            callsign=r.callsign or r.callsign_raw, emergency_type=r.emergency_type, severity=r.severity,
            airport="SAN", eta_min=12, souls_on_board=r.souls_on_board,
        )
        inc["unit"] = brief
        log.info("[mock] %s assigning %s", iid, unit_name)
        await self.voice.call_unit(brief)

    async def on_unit_response(self, incident_id: str, unit_id: str, accepted: bool, reason: str | None) -> None:
        log.info("[mock] %s %s %s (%s)", incident_id, unit_id, "ACCEPTED" if accepted else "declined", reason)
        inc = self.incidents[incident_id]
        if accepted:
            b: UnitBrief = inc["unit"]
            cs = spoken_callsign(b.callsign) if is_valid_icao(b.callsign) else b.callsign
            plan = (f"{cs}, dispatch plan: land San Diego International. {b.unit_name} is responding "
                    f"and will meet you on landing. Emergency vehicles standing by.")
            await self.voice.brief_pilot(incident_id, plan)
        else:
            await self.voice.update_pilot(incident_id, "Stand by, assigning another unit.")
            await self._dispatch_next(incident_id)

    async def on_pilot_confirm(self, incident_id: str, readback: str) -> None:
        self.confirmed[incident_id] = readback
        log.info("[mock] %s CLOSED, pilot confirmed: %r", incident_id, readback)

    async def on_event(self, event: dict) -> None:
        self.events.append(event)
        del self.events[:-500]
