"""HTTP and WebSocket API for the integrated dispatch backend."""
from __future__ import annotations

import asyncio
import hmac
import json

from fastapi import APIRouter, Depends, HTTPException, Query, Request, WebSocket, WebSocketDisconnect
from pydantic import BaseModel

from voice.models import EmergencyReport

from .aircraft import AircraftLookupError, AircraftService
from .config import BackendSettings
from .dispatch import DispatchEngine, DispatchError


class ConfirmBody(BaseModel):
    readback: str = "wilco"


def build_router(engine: DispatchEngine, aircraft: AircraftService,
                 config: BackendSettings) -> APIRouter:
    router = APIRouter()

    async def authorize(request: Request) -> None:
        token = config.api_token
        if token is None:
            return
        value = request.headers.get("authorization", "")
        supplied = value[7:] if value.lower().startswith("bearer ") else ""
        if not supplied or not hmac.compare_digest(supplied, token):
            raise HTTPException(status_code=401, detail="Authentication required")

    def dispatch_error(error: DispatchError) -> HTTPException:
        return HTTPException(status_code=error.status_code, detail=error.detail)

    @router.get("/health")
    async def health():
        return {
            "ok": True,
            "sim_units": engine.settings.sim_units,
            "offline_voice": config.offline_voice,
            "mock_dispatch": False,
            "aircraft_mode": config.aircraft_mode,
            "authentication_enabled": bool(config.api_token),
            "persistence": "in_memory",
        }

    @router.post("/api/incidents", status_code=202, dependencies=[Depends(authorize)])
    @router.post("/api/emergencies", status_code=202, dependencies=[Depends(authorize)])
    async def create_incident(report: EmergencyReport):
        try:
            incident_id = await engine.create_incident(report)
            return await engine.incident(incident_id)
        except DispatchError as error:
            raise dispatch_error(error)

    @router.get("/api/snapshot", dependencies=[Depends(authorize)])
    @router.get("/observer/state", dependencies=[Depends(authorize)], include_in_schema=False)
    async def snapshot():
        return await engine.snapshot()

    @router.get("/api/incidents", dependencies=[Depends(authorize)])
    async def incidents():
        return (await engine.snapshot())["incidents"]

    @router.get("/api/incidents/{incident_id}", dependencies=[Depends(authorize)])
    async def get_incident(incident_id: str):
        try:
            return await engine.incident(incident_id)
        except DispatchError as error:
            raise dispatch_error(error)

    @router.get("/api/incidents/{incident_id}/voice-update", dependencies=[Depends(authorize)])
    async def voice_update(incident_id: str):
        try:
            return await engine.voice_update(incident_id)
        except DispatchError as error:
            raise dispatch_error(error)

    @router.post("/api/incidents/{incident_id}/confirm", dependencies=[Depends(authorize)])
    async def confirm(incident_id: str, body: ConfirmBody):
        if engine.voice.registry.pilot_for_incident(incident_id) is None:
            raise HTTPException(status_code=409, detail="No active pilot call is bound to this incident")
        try:
            await engine.on_pilot_confirm(incident_id, body.readback)
            return await engine.incident(incident_id)
        except DispatchError as error:
            raise dispatch_error(error)

    @router.post("/api/incidents/{incident_id}/cancel", dependencies=[Depends(authorize)])
    async def cancel(incident_id: str):
        try:
            return await engine.end(incident_id)
        except DispatchError as error:
            raise dispatch_error(error)

    @router.post("/api/incidents/{incident_id}/resolve", dependencies=[Depends(authorize)])
    async def resolve(incident_id: str):
        try:
            return await engine.end(incident_id, resolved=True)
        except DispatchError as error:
            raise dispatch_error(error)

    @router.get("/api/units", dependencies=[Depends(authorize)])
    async def units():
        return (await engine.snapshot())["units"]

    @router.get("/api/dispatchers", dependencies=[Depends(authorize)])
    async def dispatchers():
        return (await engine.snapshot())["dispatchers"]

    @router.get("/api/airports", dependencies=[Depends(authorize)])
    async def airports():
        return (await engine.snapshot())["airports"]

    @router.get("/api/aircraft/{callsign}", dependencies=[Depends(authorize)])
    async def aircraft_lookup(callsign: str):
        try:
            return await aircraft.lookup(callsign)
        except AircraftLookupError as error:
            raise HTTPException(status_code=503, detail=str(error)) from error

    @router.get("/api/events", dependencies=[Depends(authorize)])
    async def events(after: int = Query(0, ge=0), incident_id: str | None = None):
        result = (await engine.snapshot())["events"]
        return [event for event in result if event["sequence"] > after and
                (incident_id is None or event["incident_id"] == incident_id)]

    @router.websocket("/ws/events")
    async def websocket_events(websocket: WebSocket):
        origin = websocket.headers.get("origin")
        if origin and origin not in config.cors_origins:
            await websocket.close(code=1008, reason="Origin not allowed")
            return
        await websocket.accept()
        if config.api_token:
            auth = websocket.headers.get("authorization", "")
            bearer = auth[7:] if auth.lower().startswith("bearer ") else ""
            if not bearer or not hmac.compare_digest(bearer, config.api_token):
                try:
                    message = await asyncio.wait_for(websocket.receive_text(), timeout=5)
                    supplied = json.loads(message).get("token", "")
                except (asyncio.TimeoutError, json.JSONDecodeError, WebSocketDisconnect, AttributeError):
                    await websocket.close(code=1008, reason="Authentication required")
                    return
                if not isinstance(supplied, str) or not hmac.compare_digest(supplied, config.api_token):
                    await websocket.close(code=1008, reason="Authentication required")
                    return
        queue, initial = await engine.subscribe()
        try:
            await websocket.send_json({"type": "snapshot", "data": initial})
            receive_task = asyncio.create_task(websocket.receive_text())
            while True:
                event_task = asyncio.create_task(queue.get())
                done, _ = await asyncio.wait(
                    {receive_task, event_task}, timeout=20,
                    return_when=asyncio.FIRST_COMPLETED,
                )
                if not done:
                    event_task.cancel()
                    await asyncio.gather(event_task, return_exceptions=True)
                    await websocket.send_json({"type": "heartbeat", "sequence": engine.sequence})
                    continue
                if receive_task in done:
                    message = receive_task.result()
                    if message == "ping":
                        await websocket.send_json({"type": "pong"})
                    receive_task = asyncio.create_task(websocket.receive_text())
                if event_task in done:
                    item = event_task.result()
                else:
                    event_task.cancel()
                    await asyncio.gather(event_task, return_exceptions=True)
                    continue
                if item.get("type") == "resync_required":
                    await websocket.send_json({"type": "snapshot", "data": await engine.snapshot()})
                else:
                    await websocket.send_json(item)
        except WebSocketDisconnect:
            pass
        finally:
            if "receive_task" in locals() and not receive_task.done():
                receive_task.cancel()
                await asyncio.gather(receive_task, return_exceptions=True)
            await engine.unsubscribe(queue)

    return router
