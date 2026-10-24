"""Integrated SkyWatch app. Run: uvicorn main:app --workers 1 --port 8000."""

from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager
import hmac
from typing import Annotated

import httpx
from fastapi import Depends, FastAPI, Header, HTTPException, Query, Request, WebSocket, WebSocketDisconnect
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse, RedirectResponse
from pydantic import BaseModel, Field, ValidationError

from backend.aircraft import AircraftLookupError, AircraftService
from backend.config import BackendSettings
from backend.dispatch import DispatchEngine, DispatchError
from backend.models import AircraftContext, Incident
from backend.offline import OfflineVapiClient
from voice.config import Settings
from voice.models import EmergencyReport
from voice.ports import VoiceService
from voice.router import build_router
from voice.vapi_client import VapiClient


class Confirmation(BaseModel):
    readback: str = Field(min_length=1, max_length=2000)


def create_app(settings: Settings | None = None, backend_settings: BackendSettings | None = None,
               aircraft_provider=None, vapi_client=None, aircraft_transport=None):
    settings = settings or Settings.from_env()
    config = backend_settings or BackendSettings.from_env()
    if settings.mock_dispatch:
        raise ValueError("main.py uses the real backend. Set MOCK_DISPATCH=0; dev_main.py retains the standalone voice mock.")
    if config.offline_voice and not settings.sim_units:
        raise ValueError("OFFLINE_VOICE=1 requires SIM_UNITS=1")
    if vapi_client is None and not config.offline_voice and not settings.vapi_api_key:
        raise ValueError("Live voice requires VAPI_API_KEY; use OFFLINE_VOICE=1 for an offline rehearsal")

    @asynccontextmanager
    async def lifespan(application):
        async with httpx.AsyncClient(timeout=5, transport=aircraft_transport, follow_redirects=False) as client:
            vapi = vapi_client or (OfflineVapiClient() if config.offline_voice else VapiClient(settings.vapi_api_key))
            voice = VoiceService(settings, vapi)
            aircraft = AircraftService(client, mode=config.aircraft_mode,
                                       allow_fallback=config.allow_aircraft_fallback, provider=aircraft_provider)
            dispatch = DispatchEngine(voice, settings, config, aircraft)
            voice.bind_dispatch(dispatch)
            application.state.voice, application.state.dispatch = voice, dispatch
            application.state.engine, application.state.vapi = dispatch, vapi
            voice_router = build_router(voice)
            application.state.voice_webhook = next(route.endpoint for route in voice_router.routes if route.path == "/vapi/webhook")
            try:
                yield
            finally:
                await dispatch.aclose()
                await voice.aclose()
                if hasattr(vapi, "aclose"):
                    await vapi.aclose()

    app = FastAPI(title="SkyWatch — Integrated Emergency Dispatch", version="0.2.0", lifespan=lifespan,
                  description="Existing Vapi voice service connected to aircraft intelligence, dispatch, and observer APIs. Emergencies and responders are simulated. State is in-memory; use one worker.")
    app.add_middleware(CORSMiddleware, allow_origins=list(config.cors_origins), allow_credentials=False,
                       allow_methods=["GET", "POST", "OPTIONS"], allow_headers=["Authorization", "Content-Type"])

    @app.exception_handler(DispatchError)
    async def dispatch_error(request, error):
        return JSONResponse(status_code=error.status_code, content={"detail": error.detail})

    async def authorize(authorization: Annotated[str | None, Header()] = None):
        if config.api_token and not hmac.compare_digest(authorization or "", f"Bearer {config.api_token}"):
            raise HTTPException(401, "Valid bearer token required", headers={"WWW-Authenticate": "Bearer"})

    protected = [Depends(authorize)]

    @app.get("/", include_in_schema=False)
    async def root():
        return RedirectResponse("/docs")

    @app.get("/health", tags=["system"])
    async def health():
        return {"ok": True, "mock_dispatch": False, "sim_units": settings.sim_units,
                "offline_voice": config.offline_voice, "aircraft_mode": config.aircraft_mode,
                "authentication_enabled": bool(config.api_token), "persistence": "in-memory"}

    @app.post("/api/emergencies", status_code=202, response_model=Incident, dependencies=protected, tags=["incidents"])
    @app.post("/api/incidents", status_code=202, response_model=Incident, dependencies=protected, tags=["incidents"])
    async def report(report: EmergencyReport, request: Request):
        """JSON intake using the shared Voice report. Production Vapi intake is /vapi/webhook."""
        dispatch, voice = request.app.state.dispatch, request.app.state.voice
        iid = await dispatch.create_incident(report)
        voice.registry.bind_incident(report.call_id, iid)
        return await dispatch.incident(iid)

    @app.get("/api/snapshot", dependencies=protected, tags=["dashboard"])
    async def snapshot(request: Request):
        return await request.app.state.dispatch.snapshot()

    @app.get("/api/incidents", response_model=list[Incident], dependencies=protected, tags=["incidents"])
    async def incidents(request: Request):
        return (await request.app.state.dispatch.snapshot())["incidents"]

    @app.get("/api/incidents/{incident_id}", response_model=Incident, dependencies=protected, tags=["incidents"])
    async def incident(incident_id: str, request: Request):
        return await request.app.state.dispatch.incident(incident_id)

    @app.get("/api/incidents/{incident_id}/voice-update", dependencies=protected, tags=["voice"])
    async def voice_update(incident_id: str, request: Request):
        return await request.app.state.dispatch.voice_update(incident_id)

    @app.post("/api/incidents/{incident_id}/confirm", response_model=Incident, dependencies=protected, tags=["voice"])
    async def confirm(incident_id: str, confirmation: Confirmation, request: Request):
        voice, dispatch = request.app.state.voice, request.app.state.dispatch
        pc = voice.registry.pilot_for_incident(incident_id)
        if pc is None or pc.ended or not pc.plan_delivered:
            raise DispatchError(409, "The active pilot has not received a response plan")
        await dispatch.on_pilot_confirm(incident_id, confirmation.readback)
        pc.confirmed = True
        return await dispatch.incident(incident_id)

    @app.post("/api/incidents/{incident_id}/cancel", response_model=Incident, dependencies=protected, tags=["incidents"])
    async def cancel(incident_id: str, request: Request):
        return await request.app.state.dispatch.end(incident_id)

    @app.post("/api/incidents/{incident_id}/resolve", response_model=Incident, dependencies=protected, tags=["incidents"])
    async def resolve(incident_id: str, request: Request):
        return await request.app.state.dispatch.end(incident_id, resolved=True)

    @app.get("/api/units", dependencies=protected, tags=["dashboard"])
    async def units(request: Request):
        return (await request.app.state.dispatch.snapshot())["units"]

    @app.get("/api/dispatchers", dependencies=protected, tags=["dashboard"])
    async def dispatchers(request: Request):
        return (await request.app.state.dispatch.snapshot())["dispatchers"]

    @app.get("/api/airports", dependencies=protected, tags=["aircraft"])
    async def airports(request: Request):
        return request.app.state.dispatch.aircraft_service.airports

    @app.get("/api/aircraft/{callsign}", response_model=AircraftContext, dependencies=protected, tags=["aircraft"])
    async def aircraft(callsign: str, request: Request):
        try:
            return AircraftContext.model_validate(await request.app.state.dispatch.aircraft_service.lookup(callsign))
        except (AircraftLookupError, ValidationError) as error:
            raise HTTPException(503, "Aircraft lookup did not return usable data") from error

    @app.get("/api/events", dependencies=protected, tags=["dashboard"])
    async def events(request: Request, incident_id: str | None = None, after: int = Query(default=0, ge=0)):
        return [e for e in (await request.app.state.dispatch.snapshot())["events"]
                if e["sequence"] > after and (not incident_id or e["incident_id"] == incident_id)]

    @app.websocket("/ws/events")
    async def websocket_events(websocket: WebSocket):
        origin = websocket.headers.get("origin")
        if origin and origin not in config.cors_origins:
            await websocket.close(code=1008)
            return
        await websocket.accept()
        if config.api_token:
            header = websocket.headers.get("authorization", "")
            if not hmac.compare_digest(header, f"Bearer {config.api_token}"):
                try:
                    credentials = await asyncio.wait_for(websocket.receive_json(), timeout=5)
                    token = credentials.get("token") if isinstance(credentials, dict) else None
                    if not isinstance(token, str) or not hmac.compare_digest(token, config.api_token):
                        await websocket.close(code=1008)
                        return
                except (TimeoutError, ValueError, WebSocketDisconnect):
                    await websocket.close(code=1008)
                    return
        dispatch = websocket.app.state.dispatch
        queue, initial = await dispatch.subscribe()
        incoming = event_task = None
        try:
            await websocket.send_json({"type": "snapshot", "data": initial})
            last_sequence = initial["sequence"]
            incoming, event_task = asyncio.create_task(websocket.receive_text()), asyncio.create_task(queue.get())
            while True:
                done, _ = await asyncio.wait({incoming, event_task}, timeout=15, return_when=asyncio.FIRST_COMPLETED)
                if not done:
                    await websocket.send_json({"type": "heartbeat", "sequence": dispatch.sequence})
                if incoming in done:
                    if incoming.result() == "ping":
                        await websocket.send_json({"type": "pong"})
                    incoming = asyncio.create_task(websocket.receive_text())
                if event_task in done:
                    message = event_task.result()
                    if message["type"] == "resync_required":
                        message = {"type": "snapshot", "data": await dispatch.snapshot()}
                        last_sequence = message["data"]["sequence"]
                        await websocket.send_json(message)
                    elif message["data"]["sequence"] > last_sequence:
                        last_sequence = message["data"]["sequence"]
                        await websocket.send_json(message)
                    event_task = asyncio.create_task(queue.get())
        except (WebSocketDisconnect, RuntimeError):
            pass
        finally:
            tasks = [t for t in (incoming, event_task) if t is not None]
            for task in tasks:
                task.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)
            await dispatch.unsubscribe(queue)

    # Preserve the team's existing router, tools, call control, and wire schemas.
    # Vapi authenticates via VAPI_WEBHOOK_SECRET, independently of observer API_TOKEN.
    app.include_router(build_router_for_lifespan(app))
    return app


def build_router_for_lifespan(app):
    """Resolve VoiceService at request time because it belongs to app lifespan."""
    from fastapi import APIRouter
    router = APIRouter()

    @router.post("/vapi/webhook", tags=["voice"])
    async def webhook(request: Request):
        voice = request.app.state.voice
        if not hasattr(request.app.state, "voice_webhook"):
            actual_router = build_router(voice)
            request.app.state.voice_webhook = next(route.endpoint for route in actual_router.routes if route.path == "/vapi/webhook")
        return await request.app.state.voice_webhook(request)
    return router


app = create_app()
