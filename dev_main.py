"""Standalone app for developing Voice without the Backend.

    MOCK_DISPATCH=1 SIM_UNITS=1 uvicorn dev_main:app --port 8000 --reload

Also exposes /dev/control/{call_id}, a stand-in for a Vapi controlUrl, so scripts/fake_webhook.py can
run the whole loop with zero Vapi credits, and /dev/state to inspect what happened.
"""
from __future__ import annotations

import logging
import os
from contextlib import asynccontextmanager
from pathlib import Path

import httpx
from fastapi import FastAPI, Request
from fastapi.responses import FileResponse

from aircraft_data import AircraftDataService, build_aircraft_router
from aircraft_data.integration import AircraftEnrichmentDispatch
from voice.config import settings
from voice.ports import MockDispatch, VoiceService, now_iso
from voice.router import build_router
from voice.vapi_client import VapiClient

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
log = logging.getLogger("dev_main")

if not settings.mock_dispatch:
    raise SystemExit("dev_main.py runs with MockDispatch only: set MOCK_DISPATCH=1 (the real Backend mounts voice itself).")

vapi = VapiClient(settings.vapi_api_key)
service = VoiceService(settings, vapi)
dispatch = MockDispatch(service, settings)
http_client = httpx.AsyncClient()
aircraft_service = AircraftDataService(
    http_client,
    download_airports=os.getenv("WARM_AIRPORT_CACHE") == "1",
)
enriched_dispatch = AircraftEnrichmentDispatch(
    dispatch,
    aircraft_service,
    allow_sample_fallback=os.getenv("AIRCRAFT_DEMO_FALLBACK") == "1",
)
service.bind_dispatch(enriched_dispatch)
dev_control_log: list[dict] = []


@asynccontextmanager
async def lifespan(app: FastAPI):
    log.info("voice dev app up: SIM_UNITS=%s SCRIPTED_DECLINE_FIRST=%s webhook=%s unit_assistant=%s",
             settings.sim_units, settings.scripted_decline_first, settings.webhook_url,
             service.unit_assistant_id or "(none: run scripts/setup_vapi.py)")
    try:
        if os.getenv("WARM_AIRPORT_CACHE") == "1":
            source = await aircraft_service.warm_airports()
            log.info("airport data warmed from %s", source)
        yield
    finally:
        await http_client.aclose()
        await vapi.aclose()


app = FastAPI(title="SkyWatch Voice (dev)", lifespan=lifespan)
app.include_router(build_router(service))
app.include_router(build_aircraft_router(aircraft_service))


@app.get("/health")
async def health():
    return {"ok": True, "sim_units": settings.sim_units, "mock_dispatch": settings.mock_dispatch}


@app.post("/dev/control/{call_id}")
async def dev_control(call_id: str, request: Request):
    """Fake live-call-control endpoint: logs what would be spoken to the caller."""
    payload = await request.json()
    dev_control_log.append({"ts": now_iso(), "call_id": call_id, **payload})
    if payload.get("type") == "say":
        log.info("🔊 [%s] SAY: %s", call_id, payload.get("content") or payload.get("message"))
    return {"ok": True}


@app.get("/dev/state")
async def dev_state():
    return {
        **service.registry.snapshot(),
        "control_log": dev_control_log,
        "mock_confirmed": dispatch.confirmed,
        "aircraft": enriched_dispatch.snapshot(),
    }


@app.get("/observer", include_in_schema=False)
async def observer():
    return FileResponse(Path(__file__).parent / "web" / "observer.html")


@app.get("/observer/state")
async def observer_state():
    return enriched_dispatch.snapshot()
