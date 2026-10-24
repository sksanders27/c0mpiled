"""Exercise the integrated backend through HTTP with offline voice and units.

Run a separately started main:app, then execute this script. No Vapi client is
imported or constructed. Health flags are checked before submitting any report.
"""

from __future__ import annotations

import argparse
import math
import os
from pathlib import Path
import sys
import time
from uuid import uuid4

import httpx
from dotenv import load_dotenv


FAILURE_STATES = {"ESCALATED", "NEEDS_CLARIFICATION", "FAILED", "CANCELLED", "RESOLVED"}


class DemoError(RuntimeError):
    """A rehearsal could not complete through the expected offline path."""


def load_environment() -> None:
    root = Path(__file__).resolve().parents[1]
    for name in (".env", "env"):
        if (root / name).is_file():
            load_dotenv(root / name, override=False)


def request_json(client: httpx.Client, method: str, path: str, *, headers: dict,
                 params: dict | None = None, payload: dict | None = None):
    try:
        response = client.request(method, path, headers=headers, params=params, json=payload)
    except httpx.HTTPError as exc:
        # Avoid printing request headers, credentials, or arbitrary server bodies.
        raise DemoError(f"HTTP request to {path} failed ({type(exc).__name__}). Is main:app running?") from exc
    if response.status_code >= 400:
        if response.status_code in {401, 403}:
            setting = "VAPI_WEBHOOK_SECRET" if path == "/vapi/webhook" else "API_TOKEN"
            raise DemoError(f"{path} returned HTTP {response.status_code}; check {setting} in the server and script environments.")
        raise DemoError(f"{path} returned HTTP {response.status_code}; inspect the server logs.")
    if 300 <= response.status_code < 400:
        raise DemoError(f"Unexpected redirect from {path}; check --base-url.")
    try:
        return response.json()
    except ValueError as exc:
        raise DemoError(f"{path} did not return JSON.") from exc


def tool_payload(call_id: str, name: str, arguments: dict) -> dict:
    return {
        "message": {
            "type": "tool-calls",
            "call": {"id": call_id},
            "toolCallList": [{
                "id": str(uuid4()),
                "type": "function",
                "function": {"name": name, "arguments": arguments},
            }],
        }
    }


def submit_tool(client: httpx.Client, headers: dict, call_id: str, name: str,
                arguments: dict) -> None:
    result = request_json(client, "POST", "/vapi/webhook", headers=headers,
                          payload=tool_payload(call_id, name, arguments))
    if not isinstance(result, dict) or not result.get("results"):
        raise DemoError(f"The webhook did not acknowledge {name}.")
    if any(item.get("error") for item in result["results"] if isinstance(item, dict)):
        raise DemoError(f"The webhook returned a tool error for {name}; inspect server logs.")


def find_incident(client: httpx.Client, headers: dict, call_id: str,
                  deadline: float) -> dict:
    while time.monotonic() < deadline:
        incidents = request_json(client, "GET", "/api/incidents", headers=headers)
        if not isinstance(incidents, list):
            raise DemoError("/api/incidents did not return the integrated backend's incident array.")
        for incident in incidents:
            if isinstance(incident, dict) and incident.get("report", {}).get("call_id") == call_id:
                return incident
        time.sleep(0.2)
    raise DemoError("Timed out waiting for the incident to be created; inspect server dispatch errors.")


def print_events(client: httpx.Client, headers: dict, incident_id: str,
                 after: int) -> int:
    events = request_json(client, "GET", "/api/events", headers=headers,
                          params={"incident_id": incident_id, "after": after})
    if not isinstance(events, list):
        raise DemoError("/api/events did not return an event array.")
    for event in events:
        sequence = event.get("sequence", 0)
        if isinstance(sequence, int):
            after = max(after, sequence)
        print(f"  {sequence:>4}  {event.get('type', 'event')}: {event.get('message', '')}", flush=True)
    return after


def wait_for_status(client: httpx.Client, headers: dict, incident_id: str,
                    target: str, deadline: float, sequence: int) -> tuple[dict, int]:
    while time.monotonic() < deadline:
        sequence = print_events(client, headers, incident_id, sequence)
        incident = request_json(client, "GET", f"/api/incidents/{incident_id}", headers=headers)
        status = incident.get("status")
        if status == target:
            return incident, sequence
        if status in FAILURE_STATES:
            raise DemoError(f"Incident stopped in {status}; inspect its warnings and server logs.")
        time.sleep(0.2)
    raise DemoError(f"Timed out waiting for {target}. The incident remains available in /api/incidents.")


def run_demo(base_url: str, timeout: float) -> None:
    load_environment()
    api_token = os.getenv("API_TOKEN", "")
    webhook_secret = os.getenv("VAPI_WEBHOOK_SECRET", "")
    api_headers = {"Authorization": f"Bearer {api_token}"} if api_token else {}
    webhook_headers = {"X-Vapi-Secret": webhook_secret} if webhook_secret else {}
    deadline = time.monotonic() + timeout

    with httpx.Client(base_url=base_url.rstrip("/"), timeout=min(timeout, 5.0), follow_redirects=False) as client:
        health = request_json(client, "GET", "/health", headers={})
        if not isinstance(health, dict) or health.get("sim_units") is not True or health.get("offline_voice") is not True:
            raise DemoError("Refusing to submit a report: /health must show sim_units=true and offline_voice=true. Start main:app with OFFLINE_VOICE=1 and SIM_UNITS=1.")
        if health.get("mock_dispatch") is not False:
            raise DemoError("Start the integrated main:app with MOCK_DISPATCH=0.")
        if health.get("authentication_enabled") and not api_token:
            raise DemoError("The observer API requires API_TOKEN; set it in the script environment or .env.")

        run_id = uuid4().hex
        call_id = f"offline-demo-pilot-{run_id}"
        flight_number = 1000 + int(run_id[:8], 16) % 9000
        callsign = f"UAL{flight_number}"
        print(f"Offline rehearsal: {callsign}; simulated FIRE_SMOKE emergency and responders.", flush=True)
        submit_tool(client, webhook_headers, call_id, "report_emergency", {
            "callsign_raw": f"United {flight_number}",
            "callsign": callsign,
            "emergency_type": "FIRE_SMOKE",
            "severity": "MAYDAY",
            "souls_on_board": 140,
            "fuel_minutes": 25,
            "intentions": "Exercise coordination only; continuing with existing ATC instructions.",
            "summary": "Simulated cabin-smoke report for an offline demonstration.",
        })

        incident = find_incident(client, api_headers, call_id, deadline)
        incident_id = incident["id"]
        print(f"Incident: {incident_id}", flush=True)
        incident, sequence = wait_for_status(client, api_headers, incident_id,
                                            "PILOT_BRIEFED", deadline, 0)
        if incident.get("plan_delivered") is not True:
            raise DemoError("The incident is PILOT_BRIEFED without a delivered plan.")
        assignments = incident.get("assignments", [])
        if not any(assignment.get("status") == "DECLINED" for assignment in assignments):
            raise DemoError("The expected simulated decline was not recorded.")
        accepted = {assignment.get("unit_type") for assignment in assignments
                    if assignment.get("status") == "ACCEPTED"}
        if not {"FIRE", "AMBULANCE"}.issubset(accepted):
            raise DemoError("The response plan was issued before both required capabilities accepted.")

        print("Pilot received the exercise plan. Submitting readback: wilco.", flush=True)
        submit_tool(client, webhook_headers, call_id, "confirm_plan", {"readback": "Wilco, exercise response plan copied."})
        incident, sequence = wait_for_status(client, api_headers, incident_id,
                                            "CONFIRMED", deadline, sequence)
        print_events(client, api_headers, incident_id, sequence)
        units = request_json(client, "GET", "/api/units", headers=api_headers)
        if any(unit.get("incident_id") == incident_id for unit in units):
            raise DemoError("The confirmed incident still holds simulated responders.")
        if incident.get("pilot_confirmed") is not True or not incident.get("closed_at"):
            raise DemoError("The incident did not record its pilot confirmation and closure.")
        print(f"CONFIRMED: {incident_id}. Simulated incident closed; responders released.", flush=True)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--base-url", default="http://127.0.0.1:8000", help="Integrated FastAPI server URL")
    parser.add_argument("--timeout", type=float, default=40.0, help="Workflow deadline in seconds (default: 40)")
    args = parser.parse_args()
    if not math.isfinite(args.timeout) or args.timeout <= 0:
        parser.error("--timeout must be a finite positive number")
    if not args.base_url.startswith(("http://", "https://")):
        parser.error("--base-url must use http:// or https://")
    try:
        run_demo(args.base_url, args.timeout)
    except (DemoError, httpx.InvalidURL) as exc:
        print(f"Demo did not complete: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
