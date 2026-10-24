# SkyWatch — Project Status & Handoff (Voice layer)

_Snapshot: 2026-10-24, 3:10 PM PDT. Hackathon c0mpiled-16 @ UCSD, theme "Invisible Interfaces" (tracks: Dispatch, No App, Unsupervised). **Submissions due 6:30 PM today.**_
Repo: https://github.com/sksanders27/c0mpiled (branch `main`)

I'm the **Voice** owner on a three-person team (Voice, Backend, Frontend). Use this file to help me decide **what to build next in the ~3 hours left**, and in what order. Keep it practical: it has to work for a live demo, and clever matters less than working.

---

## 1. What the product does

A pilot declares an emergency **by voice**: they phone a number, or use a browser fallback. The system:
1. classifies the emergency,
2. looks up the live flight (adsb.lol),
3. routes it to the nearest dispatcher, which assigns a capable response unit,
4. reaches the unit by **outbound voice call**. The unit accepts or declines; a decline goes back to the dispatcher, who picks the next unit,
5. once a unit accepts, speaks the plan to the pilot **inside the same call**. The pilot says "wilco", which closes the incident.

The pilot and units never look at a screen (the "No App" tenet). A map UI exists only as an observer view for judges.

**Ownership**
- **Voice (me, done so far):** everything that touches Vapi: assistants, webhook, tool handlers, live call control, callsign parsing, simulation/mock modes.
- **Backend (teammate):** flight lookup, nearest-dispatcher geo, unit selection, incident state.
- **Frontend (teammate):** map/timeline observer UI.

## 2. Stack
Python 3.14 (venv), FastAPI, httpx (async), pydantic v2. **Vapi** handles STT → LLM → TTS, phone numbers, tool calls and live call control. Transcriber: Deepgram `nova-3` through Vapi, with keyterms. LLM: `openai/gpt-4o-mini` through Vapi. Voice: Vapi "Elliot" v2. An ngrok static domain serves the public webhook URL.

## 3. Milestone status

| # | Milestone | Status |
|---|---|---|
| 1 | `setup_vapi.py` creates both assistants and attaches the phone number | ✅ Done and verified (idempotent: re-running updates, never duplicates) |
| 2 | Pilot says mayday → `report_emergency` hits the webhook with the correct callsign | ✅ Done and verified on a **real phone call** |
| 3 | Full loop with MockDispatch + simulated units: decline → reassign → accept → plan spoken via `say` → "wilco" → `confirm_plan` | ✅ Done: verified offline (fake webhooks) **and on a real phone call** |
| 4 | Real outbound unit call to a teammate's phone; accept, decline and timeout all work | ⛔ **Blocked**: our Vapi number is a free Vapi number, which **can't place outbound calls**. Needs a Twilio/Telnyx/Vonage number imported into Vapi. Code path is written and unit-tested with a fake client. `scripts/test_unit_call.py` not written yet. |
| 5 | Swap MockDispatch for the real Backend (same contract) | ❌ Not started; depends on Backend teammate |
| 6 | `web/pilot.html` browser fallback (push-to-talk via `@vapi-ai/web` + public key) | ❌ Not started |

### Live call result (milestone 2/3, real phone)
Timeline: report received 13s into the call → simulated unit 1 declined → pilot heard "Stand by, assigning another unit" → unit 2 accepted → pilot heard the plan (~24s) → pilot said wilco → `confirm_plan` fired → incident closed. Both `add-message` and `say` returned 200 on the first try. Confirmed that `controlUrl` is at `message.call.monitor.controlUrl`.

Issues found on that call, **fixed and pushed but not yet re-tested on a live call**:
- Pilot said "one forty souls"; STT wrote "1 40"; LLM reported 40. Prompt now says to join aviation number groups.
- "wilco" was transcribed as "Uh, worker". The prompt now accepts affirmation-sounding replies once a plan has been read.
- The assistant's own speech was transcribed as "SkyWest". Added a "SkyWatch" keyterm.
- No live transcript webhooks arrived with the `transcript[transcriptType="final"]` subscription. Switched to plain `transcript`; the router drops partials.

Known minor quirks (not fixed):
- Vapi's default tool filler plays ("1 moment", "Give me a moment").
- Two back-to-back `say` messages can run together into one utterance.

## 4. Architecture (Voice layer)

```
voice/
  config.py        settings from .env (or `env`): flags, phones, timeouts
  models.py        shared contract (EmergencyReport, UnitBrief, enums) + spoken-airport names
  callsign.py      spoken → ICAO ("United twelve thirty-four" → UAL1234, "November one two three alpha bravo" → N123AB); returns None if unsure
  vapi_client.py   async REST (assistants, phone numbers, POST /call, GET /call) + live control (say, add-message, end-call)
  assistants.py    builds both assistant JSON configs; prompts live in voice/prompts/*.md
  registry.py      in-memory maps: pilot call ↔ incident ↔ controlUrl; unit call → (incident, unit); idempotency cache
  router.py        POST /vapi/webhook: tool-calls, status-update, transcript, end-of-call-report
  tools.py         report_emergency, confirm_plan, accept_assignment, decline_assignment
  ports.py         DispatchPort protocol, VoiceService (implements VoicePort), MockDispatch
  sim.py           simulated units (attempt 1 declines after 4s, later attempts accept)
scripts/setup_vapi.py    push/update assistants, attach number, write .vapi_ids.json
scripts/fake_webhook.py  replay a full pilot call against localhost (no credits)
dev_main.py              standalone app: Voice + MockDispatch + /dev/control (fake controlUrl) + /dev/state
tests/                   69 passing tests (55 callsign, 14 router/loop/guards)
```

**Key behaviors**
- Webhook replies within milliseconds. All slow work (backend calls, dialing) runs in background tasks.
- `report_emergency` returns "Copy, coordinating now." at once. A second report on the same call is ignored, and so is a duplicate `toolCallId`.
- Dispatch updates reach the pilot through live call control. First an `add-message` system note goes in, so the LLM knows the plan. Then `say` speaks the text **verbatim**, which is deterministic and not paraphrased by the LLM.
- `confirm_plan` is rejected if no plan has been issued yet. This stops a premature "roger" from closing the incident.
- **Unsupervised guards:**
  - A unit call with no decision after 45s counts as a decline (`no_answer`).
  - A hang-up with no decision also counts as `no_answer`.
  - After 4 outbound calls per incident (`MAX_OUTBOUND_CALLS`), dialing stops and an `escalate_human` event is emitted.
  - Only numbers listed in `.env` are ever dialed.
  - Webhook secret check (optional).
- Every state change is logged with a timestamp to `logs/events.jsonl` and forwarded to `DispatchPort.on_event`. This is the feed for the judges' map timeline.

**Modes (env flags):** `SIM_UNITS=1` (no real unit calls), `MOCK_DISPATCH=1` (fake backend), `SCRIPTED_DECLINE_FIRST=1` (in real-call mode, the first unit's AI always declines, so the reassignment loop shows on stage).

## 5. Contract with Backend (agreed; Voice side is implemented)

```python
EmergencyType: MEDICAL | FIRE_SMOKE | ENGINE | FUEL | DEPRESSURIZATION | SECURITY | GEAR_STRUCTURAL | OTHER
Severity: MAYDAY | PAN

EmergencyReport(call_id, callsign_raw, callsign|None, emergency_type, severity,
                souls_on_board|None, fuel_minutes|None, intentions|None, summary)
UnitBrief(incident_id, unit_id, unit_name, phone(E.164), callsign, emergency_type, severity,
          airport, eta_min|None, souls_on_board|None)
```
- **Voice → Backend (`DispatchPort`, async):** `create_incident(report) -> incident_id`, `on_unit_response(incident_id, unit_id, accepted, reason)`, `on_pilot_confirm(incident_id, readback)`, `on_event(event: dict)`.
- **Backend → Voice (`VoicePort`, async):** `call_unit(brief)`, `brief_pilot(incident_id, text)`, `update_pilot(incident_id, text)`.

Integration as currently designed: Backend builds `VoiceService(settings, VapiClient(key))`, calls `service.bind_dispatch(their_impl)`, mounts `build_router(service)` on their FastAPI app, and calls the VoicePort methods on `service`. Same process, no HTTP between us. (`BACKEND_BASE_URL` exists in `.env`, but no HTTP adapter has been written. Open question: same process or separate services?)

Event kinds emitted so far: `report_received, incident_created, unit_call_started, unit_accepted, unit_declined, unit_call_timeout, unit_call_blocked, unit_call_failed, escalate_human, pilot_updated, pilot_briefed, pilot_confirmed, pilot_control_failed, dispatch_error, call_status, transcript, call_report, duplicate_report_ignored`. Each event carries `ts`, `kind`, and usually `incident_id`, `call_id`, and `leg` (`pilot` or `unit`).

## 6. What still needs doing (my current view; please prioritize)

1. **Re-test the fixes on one live call** (souls = 140, pilot transcript lines in `events.jsonl`, wilco robustness).
2. **Backend integration (milestone 5).** Agree with the Backend teammate on same-process (mount the router) vs. HTTP. If HTTP: write an `HttpDispatch` adapter, plus endpoints Backend can call for `call_unit`, `brief_pilot` and `update_pilot`. Agree on the event schema with Frontend for the map timeline.
3. **Outbound unit calls (milestone 4).** Decision: import a Twilio number into Vapi (costs money and setup time, but shows real calls to units on stage), or demo with `SIM_UNITS=1` and narrate the simulated units. Then write `scripts/test_unit_call.py` and test accept, decline and timeout with a teammate's phone.
4. **Browser fallback (milestone 6).** `web/pilot.html` using `@vapi-ai/web` + `VAPI_PUBLIC_KEY` and the pilot assistant ID. This is insurance if the phone line fails on stage.
5. **Demo polish:**
   - Suppress Vapi's default tool filler phrases.
   - Optionally space out back-to-back `say` calls.
   - Set `VAPI_WEBHOOK_SECRET`.
   - Rehearse the scripted demo flow (`SIM_UNITS=1` is the backup).
6. **Pitch material:** call out the unsupervised guards (timeouts, call caps, allow-list, idempotency, escalate-to-human) and the "No App" angle.

## 7. Questions I'd like help with
- Given ~3h20m to the deadline (and a feature freeze before that), what's the best order of the items above? What should I cut?
- Is a real outbound unit call worth the Twilio setup for the demo, versus simulated units plus narration?
- Same-process vs. HTTP integration with Backend: which is faster and lower-risk to wire up today?
- What should the 2–3 minute live demo script be, and what's the fallback plan if a live call fails?
