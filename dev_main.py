"""Standalone app for developing Voice without the Backend.

    MOCK_DISPATCH=1 SIM_UNITS=1 uvicorn dev_main:app --port 8000 --reload

Also exposes /dev/control/{call_id}, a stand-in for a Vapi controlUrl, so scripts/fake_webhook.py can
run the whole loop with zero Vapi credits, and /dev/state to inspect what happened.
"""
from __future__ import annotations

import logging
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request

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
service.bind_dispatch(dispatch)
dev_control_log: list[dict] = []


@asynccontextmanager
async def lifespan(app: FastAPI):
    log.info("voice dev app up: SIM_UNITS=%s SCRIPTED_DECLINE_FIRST=%s webhook=%s unit_assistant=%s",
             settings.sim_units, settings.scripted_decline_first, settings.webhook_url,
             service.unit_assistant_id or "(none: run scripts/setup_vapi.py)")
    yield
    await vapi.aclose()


app = FastAPI(title="SkyWatch Voice (dev)", lifespan=lifespan)
app.include_router(build_router(service))


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
    }
