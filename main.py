"""Integrated SkyWatch app. Run: uvicorn main:app --workers 1 --port 8000."""

from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager
import hmac
import logging
from pathlib import Path
from typing import Annotated

import httpx
from fastapi import Depends, FastAPI, Header, HTTPException, Query, Request, WebSocket, WebSocketDisconnect
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, JSONResponse, RedirectResponse
from pydantic import BaseModel, Field, ValidationError

from backend.aircraft import AircraftLookupError, AircraftService
from backend.config import BackendSettings
from backend.dispatch import DispatchEngine, DispatchError
from backend.models import AircraftContext, Incident
from backend.offline import OfflineVapiClient
from voice.config import Settings, load_vapi_ids
from voice.models import EmergencyReport
from voice.ports import VoiceService
from voice.router import build_router
from voice.vapi_client import VapiClient


logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
log = logging.getLogger("skywatch")
# adsb.lol answers 403 to httpx's default "python-httpx/x" User-Agent; any descriptive UA works.
HTTP_HEADERS = {"User-Agent": "SkyWatch/0.2 (c0mpiled-16 hackathon; github.com/sksanders27/c0mpiled)"}


def mask_phone(phone: str) -> str:
    return f"***{phone[-4:]}" if phone and len(phone) > 4 else "***"


def startup_banner(settings: Settings, config: BackendSettings, voice: VoiceService, dispatch: DispatchEngine) -> None:
    """Make the active mode impossible to miss in the terminal before a live call."""
    log.info("=" * 72)
    log.info("SkyWatch up: OFFLINE_VOICE=%s SIM_UNITS=%s UNIT_TRANSPORT=%s AIRCRAFT_MODE=%s SCRIPTED_DECLINE_FIRST=%s",
             int(config.offline_voice), int(settings.sim_units), settings.unit_transport, config.aircraft_mode,
             int(settings.scripted_decline_first))
    if not settings.sim_units and settings.unit_transport == "web":
        log.info("unit browsers: %s", ", ".join(f"{settings.public_base_url}/unit?u={n}"
                                               for n in range(1, len(settings.unit_phones) + 1)))
    log.info("auto-reset after pilot hangup: %s", f"{settings.auto_reset_after_hangup_s:g}s"
             if settings.auto_reset_after_hangup_s is not None else "off")
    log.info("webhook=%s unit_assistant=%s", settings.webhook_url,
             voice.unit_assistant_id or "(none: run scripts/setup_vapi.py)")
    log.info("unit -> phone: %s", ", ".join(f"{uid}={mask_phone(p)}" for uid, p in dispatch.unit_phones.items()))
    if config.offline_voice and settings.vapi_api_key:
        log.warning("!" * 72)
        log.warning("!!! OFFLINE_VOICE=1 but VAPI_API_KEY is set: a REAL pilot call will hear NO dispatch updates.")
        log.warning("!!! For a live call set OFFLINE_VOICE=0 in .env and restart.")
        log.warning("!" * 72)
    log.info("=" * 72)


async def reset_state(voice: VoiceService, dispatch: DispatchEngine, message: str | None = None) -> None:
    """Clear incidents, units, events and voice call state (POST /dev/reset and auto-reset)."""
    await voice.aclose()  # cancel sim units, timeouts and pending voice work; the service stays usable
    voice.registry.reset()
    await (dispatch.reset(message) if message else dispatch.reset())


def install_auto_reset(voice: VoiceService, dispatch: DispatchEngine, delay_s: float | None) -> None:
    """Reset rehearsal state delay_s after a pilot hangs up, unless another pilot call is live by then.

    The delay keeps the finished incident on the observer for a moment.
    """
    if delay_s is None:
        return

    async def later(call_id: str) -> None:
        await asyncio.sleep(delay_s)
        live = [pc.call_id for pc in voice.registry.pilot_calls.values() if not pc.ended]
        if live:
            log.info("auto-reset skipped: pilot call %s is live", live[0])
            return
        await reset_state(voice, dispatch, f"Automatic reset {delay_s:g}s after the pilot call ended.")
        log.info("auto-reset: state cleared %gs after pilot call %s ended", delay_s, call_id)

    voice.pilot_ended_hooks.append(lambda call_id: voice.spawn(later(call_id), name=f"auto-reset:{call_id}"))


class UnitBind(BaseModel):
    dispatch_id: str = Field(min_length=1, max_length=64)
    call_id: str = Field(min_length=1, max_length=128)


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
        async with httpx.AsyncClient(timeout=5, transport=aircraft_transport, follow_redirects=False,
                                     headers=HTTP_HEADERS) as client:
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
            install_auto_reset(voice, dispatch, settings.auto_reset_after_hangup_s)
            startup_banner(settings, config, voice, dispatch)
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
                       allow_methods=["GET", "POST", "OPTIONS"], allow_headers=["Authorization", "Content-Type", "X-Vapi-Secret"])

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
    @app.get("/observer/state", dependencies=protected, include_in_schema=False)
    async def snapshot(request: Request):
        return await request.app.state.dispatch.snapshot()

    @app.get("/observer", include_in_schema=False)
    async def observer():
        return FileResponse(Path(__file__).parent / "web" / "observer.html")

    @app.get("/pilot", include_in_schema=False)
    async def pilot_page():
        """Browser fallback for the pilot phone line (web call to the same pilot assistant)."""
        return FileResponse(Path(__file__).parent / "web" / "pilot.html")

    @app.get("/unit", include_in_schema=False)
    async def unit_page():
        """Unit 'phone' for UNIT_TRANSPORT=web: open /unit?u=1 (UNIT_PHONE_1's slot), /unit?u=2, ..."""
        return FileResponse(Path(__file__).parent / "web" / "unit.html")

    @app.get("/unit/pending", include_in_schema=False)
    async def unit_pending(request: Request, u: int = Query(ge=1, le=9)):
        voice = request.app.state.voice
        return {"dispatch": voice.web_pending(u), "transport": settings.unit_transport, "sim_units": settings.sim_units}

    @app.post("/unit/bind", include_in_schema=False)
    async def unit_bind(bind: UnitBind, request: Request):
        status = request.app.state.voice.web_bind(bind.dispatch_id, bind.call_id)
        if status != "ok":
            # The page hangs up when told the dispatch is no longer wanted (timed out / reassigned).
            raise HTTPException(404 if status == "unknown" else 409, status)
        return {"ok": True}

    @app.get("/pilot/config", include_in_schema=False)
    async def pilot_config():
        # Only the PUBLIC key (designed for browsers) and the assistant id. Never the private key.
        assistant_id = load_vapi_ids().get("pilot_assistant_id")
        if not settings.vapi_public_key or not assistant_id:
            raise HTTPException(503, "Set VAPI_PUBLIC_KEY and run scripts/setup_vapi.py")
        return {"publicKey": settings.vapi_public_key, "assistantId": assistant_id}

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

    @app.post("/dev/reset", dependencies=protected, tags=["system"])
    async def dev_reset(request: Request):
        """Clear incidents, units, events and voice call state between rehearsals (no restart needed)."""
        voice, dispatch = request.app.state.voice, request.app.state.dispatch
        await reset_state(voice, dispatch)
        log.info("rehearsal state reset via POST /dev/reset")
        return {"ok": True, "incidents": 0, "sequence": dispatch.sequence}

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
