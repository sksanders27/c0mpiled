"""Real DispatchPort implementation for SkyWatch's existing VoiceService.

Voice owns all calls, simulation, and speech. Backend owns location, resource
selection, reservations, reassignment, and the observer event stream.
"""

from __future__ import annotations

import asyncio
from collections import deque
from datetime import timedelta
import logging
import math

from voice.callsign import normalize_callsign, spoken_callsign
from voice.config import Settings
from voice.models import EmergencyReport, EmergencyType, UnitBrief, spoken_airport
from voice.ports import VoicePort

from .aircraft import AircraftLookupError, AircraftService
from .config import BackendSettings
from .models import (AircraftContext, Assignment, DispatchEvent, Dispatcher, Incident,
                     IncidentStatus, Unit, UnitType, utcnow)

log = logging.getLogger("backend.dispatch")
REQUIREMENTS = {
    EmergencyType.MEDICAL: [UnitType.AMBULANCE],
    EmergencyType.FIRE_SMOKE: [UnitType.FIRE, UnitType.AMBULANCE],
    EmergencyType.ENGINE: [UnitType.FIRE, UnitType.AMBULANCE],
    EmergencyType.FUEL: [UnitType.FIRE],
    EmergencyType.DEPRESSURIZATION: [UnitType.AMBULANCE],
    EmergencyType.SECURITY: [UnitType.POLICE, UnitType.AMBULANCE],
    EmergencyType.GEAR_STRUCTURAL: [UnitType.FIRE, UnitType.AMBULANCE],
    EmergencyType.OTHER: [UnitType.FIRE, UnitType.AMBULANCE],
}
CLOSED = {IncidentStatus.CONFIRMED, IncidentStatus.RESOLVED, IncidentStatus.CANCELLED, IncidentStatus.FAILED}
STOPPED = CLOSED | {IncidentStatus.ESCALATED, IncidentStatus.NEEDS_CLARIFICATION}


class DispatchError(RuntimeError):
    def __init__(self, status_code, detail):
        self.status_code, self.detail = status_code, detail
        super().__init__(detail)


def distance_km(lat1, lon1, lat2, lon2):
    a, b = math.radians(lat1), math.radians(lat2)
    h = math.sin((b-a)/2)**2 + math.cos(a)*math.cos(b)*math.sin(math.radians(lon2-lon1)/2)**2
    return 6371.0088 * 2 * math.asin(math.sqrt(min(1, max(0, h))))


def seed_units():
    units = {}
    for n, (latitude, longitude, base) in enumerate([
        (32.7336, -117.1897, "Harbor"), (32.8157, -117.1397, "Mesa"),
        (33.1283, -117.2800, "North County"),
    ], 1):
        for kind, capabilities in [(UnitType.FIRE, ["ARFF", "FIRE_SUPPRESSION"]),
                                   (UnitType.AMBULANCE, ["ALS", "MEDICAL"]),
                                   (UnitType.POLICE, ["SECURITY"])]:
            unit = Unit(id=f"{kind.lower()}-{n}", name=f"{base} {kind.title()} {n}",
                        unit_type=kind, capabilities=capabilities, latitude=latitude, longitude=longitude)
            units[unit.id] = unit
    return units


class DispatchEngine:
    def __init__(self, voice: VoicePort, settings: Settings, backend_settings: BackendSettings,
                 aircraft: AircraftService):
        self.voice, self.settings, self.config, self.aircraft_service = voice, settings, backend_settings, aircraft
        self.lock = asyncio.Lock()
        self.incidents: dict[str, Incident] = {}
        self.units = seed_units()
        self.dispatchers = [Dispatcher(id=f"dispatch-{a['icao']}", name=f"{a['name']} demo dispatch",
                                      airport_icao=a["icao"], latitude=a["latitude"], longitude=a["longitude"])
                            for a in aircraft.airports]
        self.by_call: dict[str, str] = {}
        self.events: deque[DispatchEvent] = deque(maxlen=2000)
        self.sequence = 0
        self.subscribers: set[asyncio.Queue] = set()
        self.tasks: set[asyncio.Task] = set()
        self.watchdogs: dict[str, asyncio.Task] = {}
        self.briefed: dict[str, asyncio.Event] = {}
        self.assignment_timeout = backend_settings.assignment_timeout_seconds or settings.unit_call_timeout_s + 1
        self.unit_phones = {uid: settings.unit_phone(n) for n, uid in enumerate(self.units, 1)}
        self.closing = False

    def _spawn(self, coroutine):
        task = asyncio.create_task(coroutine)
        self.tasks.add(task)
        task.add_done_callback(self._task_done)
        return task

    def _task_done(self, task):
        self.tasks.discard(task)
        if not task.cancelled() and task.exception():
            log.error("Backend background task failed (%s)", type(task.exception()).__name__)

    async def aclose(self):
        self.closing = True
        tasks = list(self.tasks)
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)

    async def reset(self, message="Rehearsal reset: incidents, units and events cleared."):
        """Rehearsal helper behind POST /dev/reset: drop incidents, reservations and events."""
        current = asyncio.current_task()
        tasks = [task for task in self.tasks if task is not current]
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        async with self.lock:
            self.incidents.clear()
            self.units = seed_units()
            self.by_call.clear()
            self.events.clear()
            self.watchdogs.clear()
            self.briefed.clear()
            self._emit(None, "system.reset", message)

    def _get(self, incident_id):
        if incident_id not in self.incidents:
            raise DispatchError(404, "Incident not found")
        return self.incidents[incident_id]

    def _emit(self, incident, kind, message, **data):
        """Emit under the state lock. Subscriber/network work never blocks it."""
        self.sequence += 1
        if incident:
            incident.version += 1
            incident.updated_at = utcnow()
            data = {**data, "incident": incident.model_dump(mode="json")}
        event = DispatchEvent(sequence=self.sequence, type=kind, kind=kind,
                              incident_id=incident.id if incident else None,
                              incident_version=incident.version if incident else None,
                              message=message,
                              data={**data, "units": [u.model_dump(mode="json") for u in self.units.values()]})
        self.events.append(event)
        for queue in tuple(self.subscribers):
            if queue.full():
                while not queue.empty():
                    queue.get_nowait()
                queue.put_nowait({"type": "resync_required"})
            else:
                queue.put_nowait({"type": "event", "data": event.model_dump(mode="json")})

    async def create_incident(self, report: EmergencyReport) -> str:
        async with self.lock:
            if self.closing:
                raise DispatchError(503, "Dispatch service is shutting down")
            if report.call_id in self.by_call:
                return self.by_call[report.call_id]
            callsign = normalize_callsign(report.callsign or report.callsign_raw)
            for previous in self.incidents.values():
                if callsign and previous.callsign == callsign and previous.status not in CLOSED:
                    raise DispatchError(409, "This callsign already has an active incident on another pilot call")
            incident = Incident(callsign=callsign, report=report,
                                required_unit_types=REQUIREMENTS[report.emergency_type])
            self.incidents[incident.id] = incident
            self.by_call[report.call_id] = incident.id
            self.briefed[incident.id] = asyncio.Event()
            self._emit(incident, "incident.reported", f"Emergency report received for {callsign or 'an unconfirmed callsign'}.")
            self._spawn(self._start(incident.id))
            return incident.id

    async def _start(self, incident_id):
        try:
            incident = self.incidents[incident_id]
            if incident.callsign is None:
                async with self.lock:
                    if incident.status in STOPPED:
                        return
                    incident.status = IncidentStatus.NEEDS_CLARIFICATION
                    incident.warnings.append("Spoken callsign could not be normalized; no aircraft identity was guessed.")
                    self._emit(incident, "incident.clarification_required", "Please clarify the aircraft callsign.")
                await self.voice.update_pilot(incident_id, "Please repeat the aircraft callsign. A human coordinator is needed to clarify this report.")
                return
            context = AircraftContext.model_validate(await self.aircraft_service.lookup(incident.callsign))
            async with self.lock:
                if incident.status in STOPPED:
                    return
                incident.aircraft, incident.nearby_airports = context.aircraft, context.nearby_airports
                incident.warnings.extend(context.warnings)
                incident.status = IncidentStatus.LOCATED
                self._emit(incident, "aircraft.located", f"Located {incident.callsign} using {context.aircraft.source} data.")
                airport = context.nearby_airports[0]
                incident.selected_airport, incident.destination_basis = airport, "NEAREST_DEMO_AIRPORT"
                incident.warnings.append("Nearest airport is a demo coordination point; proximity does not establish landing suitability or ATC clearance.")
                dispatcher = min(self.dispatchers, key=lambda d: distance_km(airport.latitude, airport.longitude, d.latitude, d.longitude))
                incident.dispatcher = dispatcher
                coverage = min(distance_km(u.latitude, u.longitude, airport.latitude, airport.longitude) for u in self.units.values())
                if airport.distance_km > 300 or coverage > 150:
                    incident.status = IncidentStatus.NEEDS_CLARIFICATION
                    incident.warnings.append("Aircraft or destination is outside seeded Southern California demo coverage.")
                    self._emit(incident, "incident.clarification_required", "Outside configured responder coverage; human coordination required.")
                    out_of_area = True
                else:
                    incident.status = IncidentStatus.DISPATCHER_ASSIGNED
                    self._emit(incident, "dispatcher.assigned", f"Selected {dispatcher.name}.")
                    out_of_area = False
            if out_of_area:
                await self.voice.update_pilot(incident_id, "Your location is outside this demonstration's responder coverage. Human coordination is required.")
            else:
                await self._advance(incident_id)
        except asyncio.CancelledError:
            raise
        except Exception as error:
            log.error("Incident lookup/dispatch failed (%s)", type(error).__name__)
            async with self.lock:
                incident = self._get(incident_id)
                if incident.status in STOPPED:
                    return
                self._release(incident)
                incident.status = IncidentStatus.FAILED
                incident.warnings.append(str(error) if isinstance(error, AircraftLookupError) else "Aircraft lookup or dispatch failed validation.")
                self._emit(incident, "incident.failed", "Automatic dispatch failed; human coordination required.")
            await self.voice.update_pilot(incident_id, "Automatic dispatch could not complete. Human coordination is required.")

    async def _advance(self, incident_id, reassigning=False):
        brief = plan = escalation = None
        async with self.lock:
            incident = self._get(incident_id)
            if self.closing or incident.status in STOPPED | {IncidentStatus.UNIT_ACCEPTED, IncidentStatus.PILOT_BRIEFED}:
                return
            if any(a.status == "PENDING" for a in incident.assignments):
                return
            accepted = {a.unit_type for a in incident.assignments if a.status == "ACCEPTED"}
            needed = [kind for kind in incident.required_unit_types if kind not in accepted]
            if not needed:
                incident.status = IncidentStatus.UNIT_ACCEPTED
                incident.plan_version += 1
                names = ", ".join(a.unit_name for a in incident.assignments if a.status == "ACCEPTED")
                plan = (f"{spoken_callsign(incident.callsign)}, exercise response coordinated at "
                        f"{spoken_airport(incident.selected_airport.icao)}. {names} have accepted. "
                        "This is response coordination only; follow your existing ATC instructions.")
                incident.plan = plan
                self._emit(incident, "incident.units_accepted", "Every required response capability has accepted.")
            else:
                kind = needed[0]
                tried = {a.unit_id for a in incident.assignments}
                type_attempts = sum(a.unit_type == kind for a in incident.assignments)
                budget = min(self.config.max_total_attempts, self.settings.max_outbound_calls)
                airport = incident.selected_airport
                candidates = [u for u in self.units.values() if u.unit_type == kind and u.status == "AVAILABLE"
                              and u.id not in tried and distance_km(u.latitude, u.longitude, airport.latitude, airport.longitude) <= 150]
                if len(incident.assignments) >= budget or type_attempts >= self.config.max_attempts_per_type:
                    escalation = "Assignment attempt budget reached."
                elif not candidates:
                    escalation = f"No available {kind.value.lower()} units remain in demo coverage."
                if escalation:
                    self._escalate(incident, escalation)
                else:
                    unit = min(candidates, key=lambda u: (distance_km(u.latitude, u.longitude, airport.latitude, airport.longitude), u.id))
                    unit.status, unit.incident_id = "RESERVED", incident.id
                    assignment = Assignment(unit_id=unit.id, unit_name=unit.name, unit_type=kind,
                                            deadline=utcnow() + timedelta(seconds=self.assignment_timeout),
                                            distance_km=round(distance_km(unit.latitude, unit.longitude, airport.latitude, airport.longitude), 2))
                    incident.assignments.append(assignment)
                    incident.status = IncidentStatus.UNIT_PENDING
                    self._emit(incident, "assignment.pending", f"Requesting {unit.name} through the voice service.", assignment_id=assignment.id)
                    brief = UnitBrief(incident_id=incident.id, unit_id=unit.id, unit_name=unit.name,
                                      phone=self.unit_phones[unit.id], callsign=incident.callsign,
                                      emergency_type=incident.report.emergency_type, severity=incident.report.severity,
                                      airport=airport.icao, eta_min=None, souls_on_board=incident.report.souls_on_board)
        if escalation:
            await self.voice.update_pilot(incident_id, "Dispatch is escalating to a human coordinator. The complete response is not yet confirmed.")
        elif brief:
            if reassigning:
                # Keep slow speech outside both the state lock and the reassignment path.
                self._spawn(self.voice.update_pilot(incident_id, "Stand by, assigning another unit."))
            try:
                await self.voice.call_unit(brief)
            except Exception:
                await self.on_unit_response(incident_id, brief.unit_id, False, "voice_call_failed")
                return
            async with self.lock:
                if assignment.status == "PENDING" and incident.status not in STOPPED:
                    # Voice owns its 45-second timeout; this is a delayed backstop
                    # for missing callbacks, including a broken simulation task.
                    assignment.deadline = utcnow() + timedelta(seconds=self.assignment_timeout)
                    self.watchdogs[assignment.id] = self._spawn(self._watch(incident_id, assignment))
        elif plan:
            try:
                await self.voice.brief_pilot(incident_id, plan)
            except Exception:
                async with self.lock:
                    incident.warnings.append("Voice could not deliver the final response plan.")
                    self._emit(incident, "pilot.delivery_failed", "Pilot briefing failed; incident remains unconfirmed.")

    async def _watch(self, incident_id, assignment):
        await asyncio.sleep(self.assignment_timeout)
        await self.on_unit_response(incident_id, assignment.unit_id, False, "backend_timeout")

    def _cancel_watch(self, assignment):
        task = self.watchdogs.pop(assignment.id, None)
        if task and task is not asyncio.current_task():
            task.cancel()

    async def on_unit_response(self, incident_id: str, unit_id: str, accepted: bool, reason: str | None):
        async with self.lock:
            incident = self._get(incident_id)
            if self.closing or incident.status in STOPPED:
                return
            assignment = next((a for a in incident.assignments if a.unit_id == unit_id and a.status == "PENDING"), None)
            if assignment is None:
                return  # Completed, expired, or superseded unit calls cannot change state.
            timed_out = reason in {"no_answer", "backend_timeout"} or utcnow() > assignment.deadline
            assignment.status = "TIMED_OUT" if timed_out else "ACCEPTED" if accepted else "DECLINED"
            assignment.reason, assignment.responded_at = reason, utcnow()
            unit = self.units[assignment.unit_id]
            if assignment.status == "ACCEPTED":
                unit.status = "ASSIGNED"
            elif unit.incident_id == incident.id:
                unit.status, unit.incident_id = "AVAILABLE", None
            self._cancel_watch(assignment)
            self._emit(incident, f"assignment.{assignment.status.lower()}",
                       f"{unit.name}: {assignment.status.lower().replace('_', ' ')}.",
                       assignment_id=assignment.id, reason=reason)
            reassigning = assignment.status != "ACCEPTED"
        # _advance tells the pilot "assigning another unit" only if it really calls another unit;
        # announcing it here raced the escalation message and could promise a unit that never comes.
        await self._advance(incident_id, reassigning=reassigning)

    def _release(self, incident, pending_only=False):
        for assignment in incident.assignments:
            if assignment.status == "PENDING":
                assignment.status, assignment.responded_at = "CANCELLED", utcnow()
            if not pending_only or assignment.status == "CANCELLED":
                self._cancel_watch(assignment)
                unit = self.units[assignment.unit_id]
                if unit.incident_id == incident.id:
                    unit.status, unit.incident_id = "AVAILABLE", None

    def _escalate(self, incident, reason):
        self._release(incident, pending_only=True)
        incident.status = IncidentStatus.ESCALATED
        incident.warnings.append(reason)
        self._emit(incident, "incident.escalated", reason + " Human coordination required.")

    async def on_event(self, event: dict):
        if self.closing:
            return
        # Do not publish raw registry/control URLs, phone numbers, credentials,
        # or arbitrary Vapi error bodies to the observer UI.
        allowed = {"ts", "kind", "incident_id", "call_id", "unit_id", "unit_name", "attempt", "leg", "role",
                   "text", "status", "ended_reason", "reason", "simulated", "delivered", "timeout_s", "attempts", "readback"}
        clean = {key: value for key, value in event.items() if key in allowed}
        kind = str(event.get("kind", "voice.event"))
        async with self.lock:
            iid = event.get("incident_id") or self.by_call.get(event.get("call_id"))
            incident = self.incidents.get(iid)
            if incident and kind == "escalate_human" and incident.status not in STOPPED:
                self._escalate(incident, "Voice outbound-call cap reached.")
            if incident and kind == "pilot_briefed" and event.get("text") == incident.plan:
                if incident.status == IncidentStatus.UNIT_ACCEPTED and event.get("delivered") is True:
                    incident.plan_delivered = True
                    incident.status = IncidentStatus.PILOT_BRIEFED
                elif event.get("delivered") is False:
                    incident.warnings.append("Final voice briefing was not delivered; confirmation remains blocked.")
                self.briefed[incident.id].set()
            self._emit(incident, kind, str(clean.get("text") or kind.replace("_", " ")), voice_event=clean)

    async def on_pilot_confirm(self, incident_id: str, readback: str):
        async with self.lock:
            incident = self._get(incident_id)
            if incident.status == IncidentStatus.CONFIRMED:
                return
            waiting_for_event = incident.status == IncidentStatus.UNIT_ACCEPTED
        if waiting_for_event:
            try:
                await asyncio.wait_for(self.briefed[incident_id].wait(), timeout=0.5)
            except TimeoutError:
                pass
        async with self.lock:
            if incident.status == IncidentStatus.CONFIRMED:
                return
            if incident.status != IncidentStatus.PILOT_BRIEFED or not incident.plan_delivered:
                raise DispatchError(409, "The final response plan has not been delivered to the pilot")
            incident.status = IncidentStatus.CONFIRMED
            incident.pilot_confirmed, incident.pilot_readback, incident.closed_at = True, readback, utcnow()
            self._release(incident)
            self._emit(incident, "incident.confirmed", "Pilot readback confirmed; simulated incident closed and responders released.")

    async def end(self, incident_id, resolved=False):
        async with self.lock:
            incident = self._get(incident_id)
            target = IncidentStatus.RESOLVED if resolved else IncidentStatus.CANCELLED
            if incident.status == target:
                return incident.model_dump(mode="json")
            if incident.status in CLOSED:
                raise DispatchError(409, "Incident is already closed")
            self._release(incident)
            incident.status, incident.closed_at = target, utcnow()
            self._emit(incident, f"incident.{target.lower()}", f"Incident {target.lower()}; simulated responders released.")
            return incident.model_dump(mode="json")

    async def incident(self, incident_id):
        async with self.lock:
            return self._get(incident_id).model_dump(mode="json")

    async def voice_update(self, incident_id):
        async with self.lock:
            incident = self._get(incident_id)
            if incident.status == IncidentStatus.ESCALATED:
                message = "Automatic dispatch stopped. The full response is not confirmed; human coordination is required."
            elif incident.status == IncidentStatus.NEEDS_CLARIFICATION:
                message = "The callsign or destination needs clarification. Human coordination is required."
            elif incident.status == IncidentStatus.FAILED:
                message = "Automatic processing failed. Human coordination is required."
            elif incident.status in {IncidentStatus.CANCELLED, IncidentStatus.RESOLVED}:
                message = f"The simulated incident is {incident.status.value.lower()}."
            else:
                message = incident.plan or "Dispatch is coordinating, stand by."
            return {"incident_id": incident.id, "call_id": incident.report.call_id, "status": incident.status,
                    "plan_version": incident.plan_version, "message": message,
                    "ready_for_confirmation": incident.status == IncidentStatus.PILOT_BRIEFED,
                    "plan_delivered": incident.plan_delivered, "pilot_confirmed": incident.pilot_confirmed,
                    "simulated_emergency": True}

    def _snapshot(self):
        return {"incidents": [i.model_dump(mode="json") for i in self.incidents.values()],
                "units": [u.model_dump(mode="json") for u in self.units.values()],
                "airports": self.aircraft_service.airports,
                "dispatchers": [d.model_dump(mode="json") for d in self.dispatchers],
                "events": [e.model_dump(mode="json") for e in self.events], "sequence": self.sequence,
                "simulation": {"emergencies": True, "responders": True, "aircraft_mode": self.config.aircraft_mode,
                               "sim_units": self.settings.sim_units, "offline_voice": self.config.offline_voice}}

    async def snapshot(self):
        async with self.lock:
            return self._snapshot()

    async def subscribe(self):
        async with self.lock:
            queue = asyncio.Queue(maxsize=128)
            self.subscribers.add(queue)
            return queue, self._snapshot()

    async def unsubscribe(self, queue):
        async with self.lock:
            self.subscribers.discard(queue)
