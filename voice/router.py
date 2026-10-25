"""POST /vapi/webhook: dispatches Vapi server messages by message.type."""
from __future__ import annotations

import hmac
import json
import logging

from fastapi import APIRouter, HTTPException, Request

from .ports import VoiceService, now_iso
from .tools import handle_tool

log = logging.getLogger("voice.router")


def parse_args(raw) -> dict:
    """Tool arguments arrive as an object or as a JSON string."""
    if isinstance(raw, dict):
        return raw
    if isinstance(raw, str) and raw.strip():
        try:
            parsed = json.loads(raw)
            return parsed if isinstance(parsed, dict) else {}
        except json.JSONDecodeError:
            log.warning("unparseable tool arguments: %r", raw[:200])
    return {}


def build_router(service: VoiceService) -> APIRouter:
    router = APIRouter()
    reg = service.registry
    warned_no_control: set[str] = set()

    def raw_log(body: dict) -> None:
        try:
            with open(service.settings.log_dir / "vapi_raw.jsonl", "a") as f:
                f.write(json.dumps({"ts": now_iso(), "body": body}, default=str) + "\n")
        except OSError as e:
            log.warning("could not write vapi_raw.jsonl: %s", e)

    def leg_info(call_id: str | None, is_unit: bool) -> tuple[str, str | None]:
        if is_unit:
            uc = reg.unit_calls.get(call_id)
            return "unit", uc.incident_id if uc else None
        pc = reg.pilot_calls.get(call_id) if call_id else None
        return "pilot", pc.incident_id if pc else None

    async def tool_calls(msg: dict, call: dict) -> list[dict]:
        items = msg.get("toolCallList")
        if not items:
            items = [t["toolCall"] for t in msg.get("toolWithToolCallList") or [] if t.get("toolCall")]
        results = []
        for tc in items:
            tcid = tc.get("id")
            fn = tc.get("function") or {}
            name = fn.get("name") or tc.get("name")
            args = parse_args(fn.get("arguments", tc.get("arguments")))
            if tcid and tcid in reg.tool_results:
                log.info("duplicate toolCallId %s (%s) ignored", tcid, name)
                results.append({"toolCallId": tcid, "result": reg.tool_results[tcid]})
                continue
            try:
                result = await handle_tool(service, name, args, call)
            except Exception:
                log.exception("tool %s failed", name)
                results.append({"toolCallId": tcid, "result": "Stand by."})
                continue
            if tcid:
                reg.tool_results[tcid] = result
            results.append({"toolCallId": tcid, "result": result})
        return results

    def call_ended(call_id: str | None, is_unit: bool, ended_reason: str | None) -> None:
        if not call_id:
            return
        if is_unit:
            uc = reg.unit_calls.get(call_id)
            if uc is None:
                return
            uc.ended = True
            if not uc.decided:
                # Hung up / no answer / voicemail without a decision counts as a decline.
                log.info("unit call %s ended (%s) without a decision", call_id, ended_reason)
                service.unit_decision(call_id, False, "no_answer")
        elif call_id in reg.pilot_calls:
            reg.pilot_calls[call_id].ended = True

    @router.post("/vapi/webhook")
    async def vapi_webhook(request: Request):
        secret = service.settings.vapi_webhook_secret
        if secret and not hmac.compare_digest(request.headers.get("x-vapi-secret", ""), secret):
            log.warning("webhook rejected: bad or missing X-Vapi-Secret")
            raise HTTPException(status_code=401, detail="bad secret")
        try:
            body = await request.json()
        except (json.JSONDecodeError, UnicodeDecodeError):
            raise HTTPException(status_code=400, detail="invalid json")
        raw_log(body)

        msg = body.get("message") or {}
        mtype = msg.get("type") or ""
        call = msg.get("call") or {}
        call_id = call.get("id")
        is_unit = service.is_unit_call(call)

        if call_id and is_unit and call_id not in reg.unit_calls:
            # Web-transport unit call that the browser hasn't bound yet: bind from the echoed variables.
            dispatch_id = ((call.get("assistantOverrides") or {}).get("variableValues") or {}).get("dispatch_id")
            if dispatch_id:
                service.web_bind(dispatch_id, call_id)

        if call_id:
            control_url = (call.get("monitor") or {}).get("controlUrl")
            if control_url and is_unit and call_id not in reg.unit_calls:
                reg.early_unit_control[call_id] = control_url  # picked up by web_bind; never a pilot entry
            elif control_url:
                reg.note_control_url(call_id, control_url)
            elif not is_unit and call_id not in warned_no_control:
                warned_no_control.add(call_id)
                log.warning("no message.call.monitor.controlUrl for call %s; will GET /call/{id} when needed", call_id)

        leg, incident_id = leg_info(call_id, is_unit)

        if mtype == "tool-calls":
            return {"results": await tool_calls(msg, call)}

        if mtype == "status-update":
            status = msg.get("status")
            ended_reason = msg.get("endedReason")
            service.emit("call_status", leg=leg, call_id=call_id, incident_id=incident_id, status=status,
                         ended_reason=ended_reason)
            if status == "ended":
                call_ended(call_id, is_unit, ended_reason)

        elif mtype.startswith("transcript"):
            if msg.get("transcriptType", "final") == "final" and msg.get("transcript"):
                service.emit("transcript", leg=leg, call_id=call_id, incident_id=incident_id,
                             role=msg.get("role"), text=msg.get("transcript"))

        elif mtype == "end-of-call-report":
            ended_reason = msg.get("endedReason")
            artifact = msg.get("artifact") or {}
            log.info("end-of-call-report %s leg=%s reason=%s", call_id, leg, ended_reason)
            try:
                with open(service.settings.log_dir / "end_of_call.jsonl", "a") as f:
                    f.write(json.dumps({"ts": now_iso(), "call_id": call_id, "leg": leg, "incident_id": incident_id,
                                        "ended_reason": ended_reason, "transcript": artifact.get("transcript")}) + "\n")
            except OSError:
                pass
            service.emit("call_report", leg=leg, call_id=call_id, incident_id=incident_id, ended_reason=ended_reason)
            call_ended(call_id, is_unit, ended_reason)

        return {}

    return router
