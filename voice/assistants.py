"""Builds the two Vapi assistant configs. Prompts live in voice/prompts/*.md."""
from __future__ import annotations

from pathlib import Path

from .config import Settings
from .models import EmergencyType, Severity

PROMPTS = Path(__file__).resolve().parent / "prompts"

PILOT_NAME = "skywatch-pilot"
UNIT_NAME = "skywatch-unit"

PILOT_KEYTERMS = [
    "SkyWatch", "Mayday", "Pan-pan", "squawk", "seven seven hundred", "souls on board", "fuel remaining",
    "wilco", "roger", "affirm", "divert", "ARFF", "depressurization", "smoke",
    "United", "American", "Delta", "Southwest", "Alaska", "JetBlue", "Spirit", "Frontier",
    "SkyWest", "Hawaiian", "FedEx", "Speedbird", "November",
]
UNIT_KEYTERMS = ["SkyWatch", "accept", "unable", "responding", "en route", "committed", "affirm", "negative", "ARFF"]

UNIT_FIRST_MESSAGE = (
    "Dispatch to {{unit_name}}. {{severity}} priority: {{emergency_type}} aboard {{callsign}}, "
    "arriving {{airport}} in about {{eta_min}} minutes, {{souls}} souls on board. "
    "Can you respond? Say accept, or unable."
)

# Plain "transcript" (not 'transcript[transcriptType="final"]', which never arrived on a live call);
# the router drops partials.
SERVER_MESSAGES = ["tool-calls", "status-update", "transcript", "end-of-call-report"]

VOICE = {"provider": "vapi", "version": "2", "voiceId": "Elliot"}


def _prompt(name: str) -> str:
    return (PROMPTS / name).read_text().strip()


def _server(s: Settings) -> dict:
    server: dict = {"url": s.webhook_url, "timeoutSeconds": 10}
    if s.vapi_webhook_secret:
        # Inline secret header; works for saved assistants and inline tool servers alike.
        server["headers"] = {"X-Vapi-Secret": s.vapi_webhook_secret}
    return server


def _fn_tool(s: Settings, name: str, description: str, properties: dict, required: list[str]) -> dict:
    return {
        "type": "function",
        "function": {
            "name": name,
            "description": description,
            "parameters": {"type": "object", "properties": properties, "required": required},
        },
        "server": _server(s),
    }


def pilot_tools(s: Settings) -> list[dict]:
    return [
        _fn_tool(
            s,
            "report_emergency",
            "Report the pilot's emergency to dispatch. Call as soon as you know the callsign and the nature "
            "of the emergency. Call only once per call.",
            {
                "callsign_raw": {"type": "string", "description": "The callsign exactly as the pilot said it."},
                "callsign": {"type": "string", "description": "ICAO callsign if certain, e.g. UAL1234 or N123AB."},
                "emergency_type": {"type": "string", "enum": [e.value for e in EmergencyType]},
                "severity": {
                    "type": "string",
                    "enum": [e.value for e in Severity],
                    "description": "MAYDAY for distress, PAN for urgency (pan-pan).",
                },
                "souls_on_board": {"type": "integer", "description": "People on board, if known."},
                "fuel_minutes": {"type": "integer", "description": "Fuel remaining in minutes, if known."},
                "intentions": {"type": "string", "description": "Pilot intentions, e.g. 'divert to nearest'."},
                "summary": {"type": "string", "description": "One-sentence summary of the emergency."},
            },
            ["callsign_raw", "emergency_type", "severity", "summary"],
        ),
        _fn_tool(
            s,
            "confirm_plan",
            "Call when the pilot affirms the dispatch plan (wilco, roger, affirm, confirmed, or a readback).",
            {"readback": {"type": "string", "description": "What the pilot said to confirm."}},
            ["readback"],
        ),
    ]


def unit_tools(s: Settings) -> list[dict]:
    return [
        _fn_tool(s, "accept_assignment", "The unit accepts the assignment and is responding.", {}, []),
        _fn_tool(
            s,
            "decline_assignment",
            "The unit cannot respond.",
            {"reason": {"type": "string", "description": "Short reason in the unit's words."}},
            ["reason"],
        ),
        {"type": "endCall"},
    ]


def _model(s: Settings, system_prompt: str, tools: list[dict]) -> dict:
    return {
        "provider": s.voice_model_provider,
        "model": s.voice_model,
        "temperature": 0.2,
        "messages": [{"role": "system", "content": system_prompt}],
        "tools": tools,
    }


def _transcriber(keyterms: list[str]) -> dict:
    return {"provider": "deepgram", "model": "nova-3", "language": "en", "keyterm": keyterms}


def pilot_assistant(s: Settings) -> dict:
    return {
        "name": PILOT_NAME,
        "firstMessage": "SkyWatch emergency line, go ahead.",
        "model": _model(s, _prompt("pilot_system.md"), pilot_tools(s)),
        "voice": VOICE,
        "transcriber": _transcriber(PILOT_KEYTERMS),
        "server": _server(s),
        "serverMessages": SERVER_MESSAGES,
        "monitorPlan": {"controlEnabled": True},
        "silenceTimeoutSeconds": 90,
        "maxDurationSeconds": 600,
        "backgroundSound": "off",
    }


def unit_assistant(s: Settings) -> dict:
    return {
        "name": UNIT_NAME,
        "firstMessage": UNIT_FIRST_MESSAGE,
        "model": _model(s, _prompt("unit_system.md"), unit_tools(s)),
        "voice": VOICE,
        "transcriber": _transcriber(UNIT_KEYTERMS),
        "server": _server(s),
        "serverMessages": SERVER_MESSAGES,
        "monitorPlan": {"controlEnabled": True},
        "endCallPhrases": ["dispatch out"],
        "silenceTimeoutSeconds": 30,
        "maxDurationSeconds": 120,
        "backgroundSound": "off",
    }
