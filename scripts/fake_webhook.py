"""Replay fixture webhooks against a local dev_main.py. No Vapi credits, no phone.

    MOCK_DISPATCH=1 SIM_UNITS=1 .venv/bin/uvicorn dev_main:app --port 8000     # terminal 1
    .venv/bin/python scripts/fake_webhook.py                                    # terminal 2

Plays a pilot call: status -> transcript -> report_emergency (+ a duplicate) -> waits for the
dispatch plan to be "spoken" (via /dev/control) -> confirm_plan. Each run uses a fresh call id.
"""
from __future__ import annotations

import argparse
import json
import sys
import time
import uuid
from pathlib import Path

import httpx

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from voice.config import settings  # noqa: E402

FIXTURES = Path(__file__).resolve().parent.parent / "tests" / "fixtures"


def load(name: str, base: str, call_id: str) -> dict:
    text = (FIXTURES / name).read_text()
    text = text.replace("http://127.0.0.1:8000", base).replace("fake-pilot-call-1", call_id)
    text = text.replace("call_report_001", f"call_report_{call_id}").replace("call_confirm_001", f"call_confirm_{call_id}")
    return json.loads(text)


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--base", default="http://127.0.0.1:8000")
    ap.add_argument("--timeout", type=float, default=40.0, help="seconds to wait for the plan")
    args = ap.parse_args()

    call_id = f"fake-pilot-{uuid.uuid4().hex[:8]}"
    headers = {"X-Vapi-Secret": settings.vapi_webhook_secret} if settings.vapi_webhook_secret else {}
    http = httpx.Client(base_url=args.base, headers=headers, timeout=10)

    def post(name: str) -> dict:
        t0 = time.monotonic()
        r = http.post("/vapi/webhook", json=load(name, args.base, call_id))
        ms = (time.monotonic() - t0) * 1000
        print(f"-> {name:32s} {r.status_code} {ms:5.0f}ms {r.text[:160]}")
        r.raise_for_status()
        return r.json()

    print(f"pilot call id: {call_id}")
    post("pilot_status_in_progress.json")
    post("pilot_transcript_final.json")
    res = post("pilot_report_emergency.json")
    assert res["results"][0]["result"] == "Copy, coordinating now.", res
    post("pilot_report_emergency.json")  # same toolCallId: idempotent

    print("waiting for dispatch (mock: 2s) + sim unit 1 decline (4s) + sim unit 2 accept (4s) ...")
    deadline = time.monotonic() + args.timeout
    pilot = {}
    while time.monotonic() < deadline:
        pilot = http.get("/dev/state").json()["pilot_calls"].get(call_id, {})
        if pilot.get("plan"):
            break
        time.sleep(0.5)
    else:
        sys.exit(f"timed out waiting for plan; pilot state: {pilot}")

    post("pilot_confirm_plan.json")
    time.sleep(0.3)
    state = http.get("/dev/state").json()
    print("\nspoken to pilot via live call control:")
    for c in state["control_log"]:
        if c["call_id"] == call_id and c.get("type") == "say":
            print(f"   🔊 {c.get('content') or c.get('message')}")
    iid = state["pilot_calls"][call_id]["incident_id"]
    print(f"\nincident {iid}: confirmed={state['pilot_calls'][call_id]['confirmed']} "
          f"mock_readback={state['mock_confirmed'].get(iid)!r}")
    for uc in state["unit_calls"].values():
        if uc["incident_id"] == iid:
            print(f"   unit {uc['unit_name']}: {'ACCEPTED' if uc['accepted'] else 'declined'} ({uc['reason']})")
    print("\nOK: full loop complete" if state["pilot_calls"][call_id]["confirmed"] else "\nFAIL: not confirmed")


if __name__ == "__main__":
    main()
