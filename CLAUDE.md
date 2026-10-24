# CLAUDE.md — Voice Layer (SkyWatch Flight Emergency Dispatch)

Hackathon: c0mpiled-16 @ UCSD, "Invisible Interfaces" (tracks: Dispatch, No App, Unsupervised).
**Hard deadline: submissions due 6:30 PM today. Feature freeze 3h15m after start.** Prefer working over clever.

## Project in one paragraph
A pilot declares an emergency by voice. We classify it, look up the live flight (adsb.lol), route it to the
nearest dispatcher, which assigns a capable unit. Units are reached by **outbound voice call** and accept or
decline; a decline goes back to the dispatcher, who picks the next unit. When a unit accepts, the pilot hears
the plan **inside the same call** and confirms ("wilco"), which closes the incident.

The pilot and units never look at a screen (No App tenet). The map is an observer view for judges only.

## Your scope (Voice — owner: me)
Build everything that touches Vapi:
1. **Pilot intake assistant** (inbound phone call + browser web-call fallback): collect the report, call `report_emergency`, later speak dispatch updates, capture confirmation via `confirm_plan`.
2. **Unit dispatch assistant** (outbound phone call): brief the unit, capture `accept_assignment` / `decline_assignment`.
3. **Vapi webhook router** (FastAPI `APIRouter`), tool handlers, and the Vapi REST client (assistants, calls, live call control).
4. **Callsign normalization** (spoken → ICAO, e.g. "United twelve thirty-four" → `UAL1234`).
5. **Simulation and mock modes** so the demo works with no outbound calls and/or no backend.

**NOT in scope:** flight lookup, nearest-dispatcher geo, unit selection logic, map UI. These belong to the Backend
and Frontend teammates. Integrate through the contract below; mock it until they're ready.

## Stack
- Python 3.11+, FastAPI, uvicorn, httpx (async), pydantic v2, python-dotenv
- Vapi (https://api.vapi.ai) for STT → LLM → TTS, phone numbers, tool calls, live call control
- Transcriber: Deepgram `nova-3` **through Vapi**, with keyterms (no separate Deepgram key needed)
- LLM: a fast model through Vapi's own providers (no separate OpenAI/Anthropic key needed). Low latency matters more than raw intelligence.
- ngrok for the public webhook URL (use the free **static domain** so the URL doesn't change)

## First steps for Claude Code
1. Add the Vapi docs MCP server, so you can verify field names instead of guessing:
   `claude mcp add vapi-docs -- npx -y mcp-remote https://docs.vapi.ai/_mcp/server`
2. **Verify against the docs before writing config:** assistant schema (`model`, `voice`, `transcriber`, `server`,
   `serverMessages`, `monitorPlan`), the transcriber keyterm field name, inline function-tool schema, the
   `tool-calls` webhook payload and its response shape, `POST /call` for outbound calls, and live-call-control payloads.
3. On the first real webhook, log the raw JSON to `logs/vapi_raw.jsonl` and confirm where `controlUrl` lives
   (expected at `message.call.monitor.controlUrl`). If it's missing, fetch it with `GET /call/{id}`.

## Repo layout (voice-owned files)
```
voice/
  __init__.py
  config.py          # loads .env, typed settings
  models.py          # pydantic: EmergencyReport, UnitBrief, enums (shared contract)
  callsign.py        # spoken callsign -> ICAO normalization (+ unit tests)
  vapi_client.py     # async httpx: upsert assistant, create outbound call, say(), add_message(), end_call(), get_call()
  assistants.py      # builds the two assistant JSON configs (prompts, tools, transcriber, voice)
  prompts/pilot_system.md
  prompts/unit_system.md
  registry.py        # in-memory maps: call_id <-> incident_id <-> controlUrl; unit_call_id -> (incident_id, unit_id)
  router.py          # APIRouter: POST /vapi/webhook  (dispatch on message.type)
  tools.py           # handlers for each tool call
  ports.py           # DispatchPort Protocol + MockDispatch; VoicePort implementation (functions backend calls)
  sim.py             # simulated units (no real calls)
scripts/
  setup_vapi.py      # idempotent create/update of both assistants; writes .vapi_ids.json; attaches phone number to pilot assistant
  fake_webhook.py    # replays fixture payloads to localhost (no Vapi credits)
  test_unit_call.py  # places ONE real outbound unit call to UNIT_PHONE_1
dev_main.py          # standalone FastAPI app: mounts voice.router with MockDispatch (for testing without backend)
web/pilot.html       # fallback: push-to-talk web call using @vapi-ai/web + VAPI_PUBLIC_KEY
tests/fixtures/*.json, tests/test_callsign.py, tests/test_router.py
```

## Shared contract with Backend (agree on this FIRST, then don't change it)

```python
class EmergencyType(str, Enum):
    MEDICAL="MEDICAL"; FIRE_SMOKE="FIRE_SMOKE"; ENGINE="ENGINE"; FUEL="FUEL"
    DEPRESSURIZATION="DEPRESSURIZATION"; SECURITY="SECURITY"; GEAR_STRUCTURAL="GEAR_STRUCTURAL"; OTHER="OTHER"

class Severity(str, Enum):
    MAYDAY="MAYDAY"; PAN="PAN"

class EmergencyReport(BaseModel):
    call_id: str
    callsign_raw: str              # as spoken
    callsign: str | None           # normalized ICAO, e.g. "UAL1234"
    emergency_type: EmergencyType
    severity: Severity
    souls_on_board: int | None = None
    fuel_minutes: int | None = None
    intentions: str | None = None
    summary: str                   # one sentence

class UnitBrief(BaseModel):
    incident_id: str
    unit_id: str; unit_name: str; phone: str     # E.164
    callsign: str; emergency_type: EmergencyType; severity: Severity
    airport: str; eta_min: int | None; souls_on_board: int | None
```

**Voice calls Backend (`DispatchPort`, async):**
- `create_incident(report: EmergencyReport) -> str` returns `incident_id`
- `on_unit_response(incident_id, unit_id, accepted: bool, reason: str | None) -> None`
- `on_pilot_confirm(incident_id, readback: str) -> None`
- `on_event(event: dict) -> None` sends live transcript lines and status for the map timeline (fire-and-forget)

**Backend calls Voice (`VoicePort`, async, implemented in `voice/ports.py`):**
- `call_unit(brief: UnitBrief) -> None` places an outbound unit call (or simulates it in SIM mode)
- `brief_pilot(incident_id, text: str) -> None` speaks to the pilot via live call control, e.g. the final plan
- `update_pilot(incident_id, text: str) -> None` sends short interim updates ("Stand by, reassigning a unit")

`MockDispatch` (used by `dev_main.py` when `MOCK_DISPATCH=1`) mimics the backend:
1. On `create_incident`: wait 2s, emit an "airport SAN" event, then `call_unit` with fake unit 1.
2. On decline: `update_pilot("Stand by, assigning another unit")`, then `call_unit` with fake unit 2.
3. On accept: `brief_pilot(...)` with the plan.

## Pilot assistant spec
- Name: `skywatch-pilot`.
- `firstMessage`: "SkyWatch emergency line, go ahead."
- Persona: calm aviation emergency coordinator. Short sentences, standard phraseology, never chatty.
- Goal: get **callsign + nature of emergency** first, then call `report_emergency` immediately. Don't wait for every
  field. If they come up naturally, also ask for souls on board, fuel remaining in minutes, and intentions.
- Read back the callsign and emergency briefly, then say: "Emergency services are being coordinated, stand by."
- Messages that arrive via live call control are dispatch updates. Speak them verbatim, then ask for a readback/confirmation.
  When the pilot affirms ("wilco", "roger", "affirm", "confirmed") call `confirm_plan`.
- Never invent units, ETAs, runways or airports. Only relay what dispatch provides.
- Map MAYDAY to severity MAYDAY; PAN-PAN or an urgent non-distress call to PAN.
- Raise `silenceTimeoutSeconds` (~90) and `maxDurationSeconds` (~600) so the call survives while dispatch works.
- Enable live call control (`monitorPlan.controlEnabled: true`; verify the name).
- Transcriber keyterms: Mayday, Pan-pan, squawk, seven seven hundred, souls on board, fuel remaining, wilco, roger,
  affirm, divert, ARFF, depressurization, smoke, United, American, Delta, Southwest, Alaska, JetBlue, Spirit,
  Frontier, SkyWest, Hawaiian, FedEx, Speedbird, November.

**Tools (function tools with server URL = `${PUBLIC_BASE_URL}/vapi/webhook`):**
- `report_emergency(callsign_raw, callsign?, emergency_type, severity, souls_on_board?, fuel_minutes?, intentions?, summary)`.
  `emergency_type` and `severity` are enums matching the contract.
- `confirm_plan(readback)`

## Unit assistant spec
- Name: `skywatch-unit`. Outbound only.
- Per-call variables (`assistantOverrides.variableValues`): `unit_name, callsign, emergency_type, severity, airport, eta_min, souls`.
- `firstMessage`: "Dispatch to {{unit_name}}. {{severity}} priority: {{emergency_type}} aboard {{callsign}}, arriving {{airport}} in about {{eta_min}} minutes, {{souls}} souls on board. Can you respond? Say accept, or unable."
- On yes/accept/responding → `accept_assignment()`. On no/unable/busy → `decline_assignment(reason)`. Then say a short
  sign-off and end the call (endCall tool or end-call via control).
- Tools: `accept_assignment()`, `decline_assignment(reason)`.

## Webhook router behavior (`POST /vapi/webhook`)
- Handle `message.type`:
  - `tool-calls`: route each entry in the tool call list by function name. Respond `{"results":[{"toolCallId": id, "result": "<short string the assistant can say>"}]}`. Arguments may arrive as an object **or** a JSON string; handle both.
  - `status-update`: if a unit call ends without an accept or decline, report it as a decline with reason `no_answer`.
  - `transcript` / `conversation-update`: forward final transcript lines to `DispatchPort.on_event`.
  - `end-of-call-report`: log it.
- **Respond within ~2s.** Do slow work (backend calls, outbound dials) in `asyncio.create_task`. `report_emergency` returns immediately with "Copy, coordinating now."
- Idempotency: ignore duplicate `toolCallId`s. Ignore a second `report_emergency` on the same call if an incident already exists.
- If `VAPI_WEBHOOK_SECRET` is set, verify the secret header Vapi sends (check the docs for the header name).

## Live call control (`vapi_client.say` / `add_message`)
- `say`: POST to `controlUrl` with `{"type":"say","content": text}`. On non-2xx, retry once with `{"type":"say","message": text}`.
  The docs and SDK disagree on the key.
- Before `say`, send `add-message` with a system message ("Dispatch update: ...") so the LLM knows the plan for follow-up questions.
- If control fails twice, log it loudly and emit an event so the backend can fall back.

## Unsupervised guards (call these out in the pitch)
- Unit call timeout: 45s (`UNIT_CALL_TIMEOUT_S`) with no decision → treat as `decline("no_answer")`.
- `MAX_OUTBOUND_CALLS` per incident (default 4). After that, stop dialing and emit an `escalate_human` event.
- Never dial a number that isn't in `.env` (`UNIT_PHONE_*`).
- Log every state change with a timestamp (the judges' map timeline consumes these).

## Modes (env flags)
- `SIM_UNITS=1`: `call_unit` doesn't dial. Unit 1 declines after 4s ("committed to another call"); unit 2 accepts after 4s.
  **Default for rehearsals and the demo backup.**
- `MOCK_DISPATCH=1`: use `MockDispatch` instead of the real backend.
- `SCRIPTED_DECLINE_FIRST=1`: in real-call mode, the first unit's prompt is instructed to decline, so the reassignment loop always shows on stage.

## Callsign normalization (`voice/callsign.py`)
- Telephony → ICAO: united→UAL, american→AAL, delta→DAL, southwest→SWA, alaska→ASA, jetblue→JBU, spirit→NKS,
  frontier (flight)→FFT, skywest→SKW, hawaiian→HAL, fedex→FDX, speedbird→BAW, allegiant→AAY.
- Spoken digits: "twelve thirty-four" → 1234, "one two three four" → 1234, "niner" → 9, "zero/oh" → 0.
- N-number: "November one two three alpha bravo" → N123AB (NATO alphabet).
- Already-normalized input ("UAL1234") passes through. Return `None` if unsure; never guess wrong silently.
- Write pytest cases for every rule above.

## Milestones (check off in order; demo-able at each step)
1. `setup_vapi.py` creates both assistants and attaches the phone number. Calling the number reaches the pilot assistant.
2. Pilot says a mayday → `report_emergency` hits the webhook → `EmergencyReport` logged with a correct callsign.
3. `dev_main.py` + `MockDispatch` + `SIM_UNITS=1` run the full loop: decline → reassign → accept → pilot hears the plan via `say` → "wilco" → `confirm_plan`.
4. Real outbound unit call to `UNIT_PHONE_1`. Accept and decline both work, and the timeout works.
5. Swap `MockDispatch` for the real backend (same contract).
6. `web/pilot.html` fallback works in the browser.

## Rules for Claude Code
- Secrets come only from `.env`. Never print, commit, or hardcode keys. Add `.env`, `.vapi_ids.json` and `logs/` to `.gitignore`.
- Ask me before placing any real outbound call (each one rings a teammate and spends credits). Prefer `fake_webhook.py` and `SIM_UNITS=1` while developing.
- Keep prompts in `voice/prompts/*.md` so I can tweak them without code changes. `setup_vapi.py` re-pushes them (update, not duplicate).
- Small, working increments. After each milestone, tell me exactly how to test it in one or two commands.
- If a Vapi field name is uncertain, check the vapi-docs MCP rather than guessing.

## Run
```
pip install fastapi uvicorn httpx pydantic python-dotenv pytest
ngrok http --url=$NGROK_DOMAIN 8000
python scripts/setup_vapi.py
MOCK_DISPATCH=1 SIM_UNITS=1 uvicorn dev_main:app --port 8000 --reload
```
