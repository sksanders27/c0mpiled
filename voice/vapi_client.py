"""Thin async client for the Vapi REST API and live call control."""
from __future__ import annotations

import logging

import httpx

log = logging.getLogger("voice.vapi")

VAPI_BASE = "https://api.vapi.ai"


class VapiError(RuntimeError):
    def __init__(self, status: int, body: str):
        super().__init__(f"Vapi HTTP {status}: {body[:500]}")
        self.status = status
        self.body = body


class VapiClient:
    def __init__(self, api_key: str, base_url: str = VAPI_BASE, timeout: float = 10.0):
        self._api = httpx.AsyncClient(
            base_url=base_url,
            headers={"Authorization": f"Bearer {api_key}"},
            timeout=timeout,
        )
        # Control URLs are per-call and unauthenticated (unless controlAuthenticationEnabled).
        self._control = httpx.AsyncClient(timeout=timeout)

    async def aclose(self) -> None:
        await self._api.aclose()
        await self._control.aclose()

    async def _req(self, method: str, path: str, **kw) -> dict | list:
        r = await self._api.request(method, path, **kw)
        if r.status_code >= 400:
            raise VapiError(r.status_code, r.text)
        return r.json() if r.content else {}

    # --- assistants -----------------------------------------------------------
    async def list_assistants(self, limit: int = 100) -> list[dict]:
        return await self._req("GET", "/assistant", params={"limit": limit})

    async def create_assistant(self, cfg: dict) -> dict:
        return await self._req("POST", "/assistant", json=cfg)

    async def update_assistant(self, assistant_id: str, cfg: dict) -> dict:
        return await self._req("PATCH", f"/assistant/{assistant_id}", json=cfg)

    async def get_assistant(self, assistant_id: str) -> dict:
        return await self._req("GET", f"/assistant/{assistant_id}")

    # --- phone numbers --------------------------------------------------------
    async def get_phone_number(self, phone_number_id: str) -> dict:
        return await self._req("GET", f"/phone-number/{phone_number_id}")

    async def attach_assistant_to_number(self, phone_number_id: str, assistant_id: str) -> dict:
        return await self._req("PATCH", f"/phone-number/{phone_number_id}", json={"assistantId": assistant_id})

    # --- calls ----------------------------------------------------------------
    async def create_call(
        self,
        assistant_id: str,
        phone_number_id: str,
        customer_number: str,
        variable_values: dict | None = None,
    ) -> dict:
        body: dict = {
            "assistantId": assistant_id,
            "phoneNumberId": phone_number_id,
            "customer": {"number": customer_number},
        }
        if variable_values:
            body["assistantOverrides"] = {"variableValues": variable_values}
        return await self._req("POST", "/call", json=body)

    async def get_call(self, call_id: str) -> dict:
        return await self._req("GET", f"/call/{call_id}")

    # --- live call control ----------------------------------------------------
    async def _control_post(self, control_url: str, payload: dict) -> bool:
        try:
            r = await self._control.post(control_url, json=payload)
        except httpx.HTTPError as e:
            log.warning("control POST %s failed: %s", payload.get("type"), e)
            return False
        if r.status_code >= 300:
            log.warning("control POST %s -> %s %s", payload.get("type"), r.status_code, r.text[:200])
            return False
        return True

    async def say(self, control_url: str, text: str) -> bool:
        # Docs use "content"; some SDK versions use "message". Try both.
        if await self._control_post(control_url, {"type": "say", "content": text}):
            return True
        return await self._control_post(control_url, {"type": "say", "message": text})

    async def add_message(
        self, control_url: str, content: str, role: str = "system", trigger_response: bool = False
    ) -> bool:
        return await self._control_post(
            control_url,
            {
                "type": "add-message",
                "message": {"role": role, "content": content},
                "triggerResponseEnabled": trigger_response,
            },
        )

    async def end_call(self, control_url: str) -> bool:
        return await self._control_post(control_url, {"type": "end-call"})
