"""Integrated Voice + dispatch app. Use OFFLINE_VOICE=1 for rehearsals."""
from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

import httpx
from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from backend.aircraft import AircraftProvider, AircraftService
from backend.config import BackendSettings
from backend.dispatch import DispatchEngine
from backend.offline import OfflineVapiClient
from backend.router import build_router
from voice.config import Settings
from voice.ports import VoiceService
from voice.router import build_router as build_voice_router
from voice.vapi_client import VapiClient


def create_app(*, aircraft_provider: AircraftProvider | None = None,
               voice_settings: Settings | None = None,
               backend_settings: BackendSettings | None = None,
               vapi_client: Any | None = None) -> FastAPI:
    voice_config = voice_settings or Settings.from_env()
    backend_config = backend_settings or BackendSettings.from_env()
    if voice_config.mock_dispatch:
        raise ValueError("main:app requires MOCK_DISPATCH=0; use dev_main:app for MockDispatch")
    if backend_config.offline_voice and not voice_config.sim_units:
        raise ValueError("OFFLINE_VOICE=1 requires SIM_UNITS=1")

    shared_http = httpx.AsyncClient()
    vapi = vapi_client or (OfflineVapiClient() if backend_config.offline_voice else VapiClient(voice_config.vapi_api_key))
    aircraft_service = AircraftService(
        shared_http,
        mode=backend_config.aircraft_mode,
        allow_fallback=backend_config.allow_aircraft_fallback,
        provider=aircraft_provider,
    )
    voice_service = VoiceService(voice_config, vapi)
    dispatch = DispatchEngine(voice_service, voice_config, backend_config, aircraft_service)
    voice_service.bind_dispatch(dispatch)

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        try:
            yield
        finally:
            await dispatch.aclose()
            voice_tasks = tuple(voice_service._tasks)
            for task in voice_tasks:
                task.cancel()
            await asyncio.gather(*voice_tasks, return_exceptions=True)
            await shared_http.aclose()
            close = getattr(vapi, "aclose", None)
            if close:
                await close()

    app = FastAPI(title="SkyWatch Dispatch", lifespan=lifespan)
    app.state.voice = voice_service
    app.state.dispatch = dispatch
    app.state.aircraft = aircraft_service
    app.add_middleware(
        CORSMiddleware,
        allow_origins=list(backend_config.cors_origins),
        allow_credentials=False,
        allow_methods=["GET", "POST", "OPTIONS"],
        allow_headers=["Authorization", "Content-Type", "X-Vapi-Secret"],
    )
    app.include_router(build_voice_router(voice_service))
    app.include_router(build_router(dispatch, aircraft_service, backend_config))

    @app.get("/observer", include_in_schema=False)
    async def observer():
        from fastapi.responses import FileResponse
        return FileResponse(Path(__file__).parent / "web" / "observer.html")

    return app


app = create_app()
