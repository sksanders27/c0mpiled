# SkyWatch — Integration Status & Remaining Work

_Snapshot: 2026-10-24, ~4:10 PM PDT. Reviewed from GitHub `sksanders27/c0mpiled`: `main` @ `3590e48`, plus two teammate branches._
_Hackathon: c0mpiled-16 @ UCSD ("Invisible Interfaces": Dispatch, No App, Unsupervised tracks). **Submissions due 6:30 PM.** Top-5 live demos at 7:45 PM._

**Audience: Claude Code, running in the Voice owner's checkout.** Read this whole file and `CLAUDE.md`, then work through Section 4 in order. The tests have **not** been run by the reviewer (no package access there). Running them is part of your job.

**Progress (updated by Claude Code in the Voice checkout):**
- [x] 4.1 Merged both branches into `main`.
  - 151 tests pass.
  - `demo_backend.py` reaches CONFIRMED, and `/observer` + `/observer/state` both serve.
  - Two extra test fixes: `tests/conftest.py` pins offline env (tests previously read the local `.env`), and the WebSocket test now tolerates a Starlette 1.10 TestClient exit race.
- [ ] 4.2 MUST fixes
- [ ] 4.3 Live call
- [ ] 4.4 Demo-value items

**Time plan:**
- Integration merged and green by **4:45 PM**
- Live end-to-end call working by **5:15 PM**
- **Feature freeze at 5:30 PM**
- Rehearse and record a backup video until 6:15
- **Submit by 6:15 PM**

---

## 1. Where the code actually is

`main` contains **only the Voice layer**. Everyone else's work sits on two unmerged branches that both fork from `main` @ `3590e48`:

| Branch | Author(s) | Contains |
|---|---|---|
| `origin/codex/integrate-dispatch-backend` | magnuswolff1 (Backend) | `backend/` (aircraft lookup via adsb.lol, `DispatchEngine` implementing `DispatchPort`, models, `OfflineVapiClient`), `data/airports.json` (8 SoCal airports), integrated `main.py` (same-process app with `/vapi/webhook`, `/api/*`, `/ws/events`), `scripts/demo_backend.py`, `docs/BACKEND_INTEGRATION.md`, **voice-layer changes** (`voice/ports.py` `aclose()` + `plan_delivered`; `voice/tools.py` synchronous, bounded `confirm_plan`; `voice/registry.py` new fields), and tests (`test_backend_aircraft`, `test_backend_dispatch`, `test_integrated_app`, `test_voice_integration_guards`). |
| `origin/codex/aircraft-data-integration` | mgreenan (Frontend/aircraft) + a merge of Backend's **first 4 commits only** (up to `2f199ac`) | `web/observer.html` (Leaflet map plus timeline that polls `/observer/state` every 1.2s), `aircraft_data/` (a second, parallel aircraft/airport lookup used only by `dev_main.py`), `backend/router.py` (Backend API as an `APIRouter`, including `/observer/state`), a **different** `main.py`, and `dev_main.py` wired to `aircraft_data`. Tests: `test_aircraft_data` (needs `pytest-asyncio`) and `test_backend_api`. |

This branch does **not** include Backend's last three commits (the voice-lifecycle fixes and integrated `main.py`).

**Trial merge result** (merging integrate first, then aircraft, into `main`): **4 conflicts.**
- `main.py` (add/add, two different apps)
- `README.md` (add/add)
- `requirements.txt`
- `.gitignore`

Everything else auto-merges, including `dev_main.py`, `.env.example`, and all of `voice/`.

Design decisions already settled (don't reopen them):
- **Same process.** Backend's `DispatchEngine` is bound with `voice.bind_dispatch(...)`, and the voice router is mounted on the same FastAPI app. There is no HTTP between Voice and Backend.
- **One public URL:** `https://pastor-earmark-handshake.ngrok-free.dev` → port 8000 → `uvicorn main:app --workers 1`. State is in memory, so run exactly one worker.
- The shared contract in `CLAUDE.md` (`EmergencyReport`, `UnitBrief`, `DispatchPort`, `VoicePort`) is unchanged by both branches.

---

## 2. What works today (per authors and earlier Voice tests)

- **Voice (main):**
  - Real inbound pilot call → `report_emergency` → MockDispatch → simulated units → decline → reassign → accept → plan spoken via live call control → "wilco" → `confirm_plan`.
  - Verified on a real phone call. 69 tests passing.
- **Backend (integrate branch):**
  - Callsign → adsb.lol (`AIRCRAFT_MODE=live`), or a labeled mock (`AIRCRAFT_MODE=mock`).
  - Nearest seeded airport, then nearest dispatcher.
  - Capability requirements per emergency type, e.g. FIRE_SMOKE needs FIRE **and** AMBULANCE; MEDICAL needs AMBULANCE only.
  - Nearest available unit gets reserved, then `voice.call_unit`.
  - Decline or timeout leads to reassignment. Attempt budgets apply: 3 per type, 4 total, also capped by Voice's `MAX_OUTBOUND_CALLS`. Exhausting them escalates to a human.
  - Final plan → `voice.brief_pilot`. Confirmation only counts after the plan was actually delivered.
  - Event bus with sequence numbers, exposed as `/api/snapshot`, `/api/events` and `/ws/events`.
  - The offline rehearsal is `scripts/demo_backend.py`.
- **Frontend (aircraft branch):** `web/observer.html` renders the engine snapshot:
  - incident cards (callsign, status, altitude/speed/track, warnings, nearby airports),
  - plane and airport markers,
  - an event timeline.

  It does **not** yet draw the dispatcher, units or assignments.

---

## 3. Problems found in review (fix the ones marked MUST)

1. **MUST: the current `.env` will break the demo.**
   - The Voice owner's `.env` has `MOCK_DISPATCH=1`, so `main:app` refuses to start.
   - It has no `OFFLINE_VOICE` line, and `BackendSettings` **defaults `OFFLINE_VOICE` to true**. The app then uses `OfflineVapiClient` for live call control, so on a real call **the pilot never hears any dispatch update**, silently.
   - `AIRCRAFT_MODE` also defaults to `mock`.
   - The required values are in Section 5.
2. **MUST: `main.py` conflict.** Two incompatible `create_app` signatures:
   - The integrate branch has `create_app(settings=, backend_settings=, aircraft_provider=, vapi_client=, aircraft_transport=)`, with inline `/api/*` and `/ws/events`, and **no** `/observer` routes.
   - The aircraft branch has `create_app(*, aircraft_provider=, voice_settings=, backend_settings=, vapi_client=)`, uses `backend/router.py`, and serves `/observer`.
   - Resolution is in 4.1.
3. **MUST: silent failure when incident creation fails.**
   - In `voice/tools.py` `_create_incident`: if `dispatch.create_incident` raises, only a `dispatch_error` event is emitted. One example is `DispatchError(409)`, raised when the same callsign already has an active incident, which happens on every rehearsal re-run without a restart.
   - The pilot hears nothing ever again.
   - Fix: on failure, speak to the pilot through the pilot call's control URL, e.g. "Dispatch could not open this incident. A human coordinator will take over." **Also** add a `POST /dev/reset` endpoint (or a documented restart) to clear state between rehearsals.
4. **SHOULD: missing spoken airport names.** `voice/models.py` `SPOKEN_AIRPORT` lacks `SEE` (Gillespie Field), `SDM` (Brown Field) and `RNM` (Ramona), which are in `data/airports.json`. Without them, TTS says "S E E".
5. **SHOULD: demo length with FIRE_SMOKE.** FIRE_SMOKE requires FIRE + AMBULANCE, which means 3 unit legs (fire-1 declines, fire-2 accepts, ambulance-1 accepts). That's fine in simulation but long with real calls. **Use MEDICAL for the live script** (one capability: decline → accept = 2 calls).
6. **SHOULD: unit-to-phone mapping is implicit.**
   - `DispatchEngine.unit_phones` maps units in seed order onto `UNIT_PHONE_1..n` cyclically.
   - With two phones: `fire-1`→phone 1, `ambulance-1`→phone 2, `police-1`→phone 1, `fire-2`→phone 2, `ambulance-2`→phone 1, and so on.
   - For MEDICAL that means ambulance-1 (phone 2, scripted decline) then ambulance-2 (phone 1, accepts).
   - Log the mapping at startup and put it in the README so the right teammate expects the right call.
7. **SHOULD: observer doesn't show the dispatch story.** Judges can't see the dispatcher choice or the unit decline/accept except as timeline text. Add:
   - a dispatcher marker,
   - unit markers colored by status (AVAILABLE/RESERVED/ASSIGNED),
   - the incident's assignments list (unit, status, reason) on the incident card.

   The snapshot already includes `incidents[].dispatcher`, `incidents[].assignments` and `units[]`.
8. **NICE:** `/ws/events` rejects origins not in `CORS_ORIGINS`. The bundled observer polls over HTTP, same-origin, so it isn't affected. If anyone builds a separate WebSocket client, add the ngrok origin.
9. **NICE: cleanup.**
   - `UPSTREAM_SNAPSHOT.json` and the README "Source handoff" section describe a patch-based import and are now stale. Delete them.
   - `aircraft_data/` duplicates `backend/aircraft.py`. **Don't unify today.** Keep `aircraft_data/` only for the `dev_main.py` fallback, and document that.

---

## 4. Do these in order

### 4.1 Merge (target: green by 4:45)
Ask teammates to **pause pushing** to their branches during the merge.

```bash
git fetch --all
git checkout -b integration origin/main
git merge --no-edit origin/codex/integrate-dispatch-backend    # merges cleanly
git merge origin/codex/aircraft-data-integration               # 4 conflicts
```

Resolve as follows:
- **`main.py`:** keep the **integrate-branch** version. It carries the voice-lifecycle integration and its tests.
  - Port these from the aircraft version: `GET /observer` (serves `web/observer.html`) and `GET /observer/state` (returns `await dispatch.snapshot()`, with the same auth dependency as `/api/snapshot`). Add `"X-Vapi-Secret"` to the CORS `allow_headers`.
  - Then update `tests/test_backend_api.py` to call `create_app(settings=..., backend_settings=..., vapi_client=...)`, i.e. rename `voice_settings`→`settings`.
  - `backend/router.py` becomes unused. Delete it, unless you choose instead to switch `main.py` over to it (don't, no time).
- **`requirements.txt`:** keep the integrate branch's pinned lines and **add `pytest-asyncio`**, which `tests/test_aircraft_data.py` needs. Regenerate or adjust `requirements-lock.txt` if it's used.
- **`.gitignore`:** union of both.
- **`README.md`:**
  - Keep the integrate version's run, test and integration sections.
  - Append the aircraft branch's `aircraft_data` / observer sections.
  - Drop the stale "Source handoff" section, add the `/observer` URL, and add the mode table from Section 5.
- `git rm UPSTREAM_SNAPSHOT.json`.

Verify, then merge into `main` and push. Tell the team to rebase onto `main` from then on.

```bash
pip install -r requirements.txt
pytest -q                                   # ALL tests must pass (voice + backend + aircraft)
OFFLINE_VOICE=1 SIM_UNITS=1 MOCK_DISPATCH=0 AIRCRAFT_MODE=mock uvicorn main:app --workers 1 --port 8000
python scripts/demo_backend.py              # in a 2nd terminal: full offline loop must close the incident
# open http://127.0.0.1:8000/observer while demo_backend runs: incident card + timeline must update
```

### 4.2 Fix the MUST items (Section 3: #1 and #3; #2 is done in 4.1)
- **Startup banner:** `main.py` logs at startup:
  - OFFLINE_VOICE, SIM_UNITS, AIRCRAFT_MODE, SCRIPTED_DECLINE_FIRST,
  - the webhook URL and the unit assistant ID,
  - the unit→phone mapping, with phones masked except the last 4 digits.

  Also log a **loud warning** if `OFFLINE_VOICE=1` while `VAPI_API_KEY` is set (likely misconfiguration).
- **Pilot failure message:** when `create_incident` raises, speak the failure message to the pilot (§3 #3), and add a test.
- **`POST /dev/reset`:** resets `DispatchEngine` incidents, units, events and voice registry state. Allow it only when `API_TOKEN` is unset or the bearer token matches. Add a test.
- Add the `SPOKEN_AIRPORT` entries (§3 #4).

### 4.3 Live end-to-end on a real call (target: 5:15)
1. Update the **real `.env`** to the "Live demo (sim units)" column in Section 5.
2. Run `python scripts/setup_vapi.py` (idempotent), so both assistants point at the ngrok webhook.
3. Run `ngrok http --url=pastor-earmark-handshake.ngrok-free.dev 8000` and `uvicorn main:app --workers 1 --port 8000`.
4. Pick a real aircraft currently near San Diego:
   - `GET https://api.adsb.lol/v2/point/32.73/-117.19/60`
   - Choose a UAL/AAL/SWA/ASA/DAL flight. Its callsign must normalize with `voice/callsign.py`.
   - Confirm with `GET /api/aircraft/<CALLSIGN>` that it returns `is_mock: false`.
5. The Voice owner calls the pilot number and declares a MEDICAL mayday with that callsign. Expect:
   - the plane on `/observer`,
   - a sim decline, then "Stand by, assigning another unit",
   - a sim accept, then the plan spoken,
   - "wilco" → incident `CONFIRMED`.
6. Check `logs/events.jsonl` and `/api/snapshot`. **Don't change prompts after this passes**, except to fix a failure.

### 4.4 Demo-value items, in priority order (stop at 5:30 freeze; cut from the bottom)
1. **Observer upgrade** (§3 #7). Frontend teammate owns it, but do it here if they're busy. About 30 min.
2. **`web/pilot.html` browser fallback**, served at `/pilot`:
   - one button, starting a web call to the pilot assistant with `@vapi-ai/web` (pinned version, loaded from a CDN) and `VAPI_PUBLIC_KEY`;
   - the assistant ID is read from `.vapi_ids.json` by a small `GET /pilot/config` endpoint.

   Never expose the private key. About 20 min.
3. **Suppress Vapi's default tool filler** ("one moment"). Check the vapi-docs MCP for the tool `messages` / `request-start` config, then re-run `setup_vapi.py`.
4. **Serialize pilot `say` calls** with a per-call queue and a ~1.5s gap, so back-to-back updates don't merge.
5. **Real outbound unit calls (milestone 4)**, only if a Twilio number has been imported into Vapi by the human:
   - Add `VAPI_OUTBOUND_PHONE_NUMBER_ID` (falls back to `VAPI_PHONE_NUMBER_ID`), used only by `call_unit`.
   - Write `scripts/test_unit_call.py` (one call to `UNIT_PHONE_1` with a fake `UnitBrief`).
   - **Ask the human before running it.**
   - Test accept, decline and the 45s timeout.
   - If this isn't working by 5:15, drop it and demo with `SIM_UNITS=1`.

---

## 5. Environment modes (`.env`)

| Variable | Offline rehearsal | **Live demo (sim units)** | Live demo (real unit calls) | Voice-only fallback (`dev_main:app`) |
|---|---|---|---|---|
| `MOCK_DISPATCH` | 0 | **0** | 0 | 1 |
| `OFFLINE_VOICE` | 1 | **0** ← must be explicit | 0 | n/a |
| `SIM_UNITS` | 1 | **1** | 0 | 1 |
| `AIRCRAFT_MODE` | mock | **live** | live | n/a |
| `ALLOW_AIRCRAFT_FALLBACK` | 1 | **1** (labeled sample if adsb.lol fails) | 1 | n/a |
| `SCRIPTED_DECLINE_FIRST` | 0 | 0 (sim already declines attempt 1) | **1** | 0 |
| `API_TOKEN` | blank | **blank** (otherwise observer prompts for a token on stage) | blank | n/a |
| `VAPI_OUTBOUND_PHONE_NUMBER_ID` | n/a | n/a | Twilio-imported number ID | n/a |

Unchanged values:
- `VAPI_*` keys and `VAPI_PHONE_NUMBER_ID` (inbound free Vapi number)
- `PUBLIC_BASE_URL=https://pastor-earmark-handshake.ngrok-free.dev`
- `UNIT_PHONE_1=+15108338925`, `UNIT_PHONE_2=+16198160348`
- `UNIT_CALL_TIMEOUT_S=45`, `MAX_OUTBOUND_CALLS=4`, `MAX_ATTEMPTS_PER_TYPE=3`, `MAX_TOTAL_ATTEMPTS=4`

Run command for every live mode: `uvicorn main:app --workers 1 --port 8000`. Switching modes means editing `.env` and restarting (a few seconds).

---

## 6. Demo script (≈2.5 min) and fallbacks

1. **Hook:** "In an emergency the pilot is on the radio, not on an app. SkyWatch turns that voice call into a dispatched response, and nobody touches a screen."
2. **Mayday (speakerphone):** "Mayday mayday mayday, [real callsign], passenger in cardiac arrest, 140 souls on board, 45 minutes fuel, request San Diego."
3. **On `/observer`:** the live plane appears, then the nearest airport and dispatcher, then the unit assignment.
4. **Unit 1 declines** (simulated, or the scripted real call), and the pilot hears "Stand by, assigning another unit."
5. **Unit 2 accepts.** The pilot hears the plan and says "wilco." The incident shows `CONFIRMED` and the responders are released.
6. **Unsupervised close:**
   - 45s unit timeouts with a backend watchdog,
   - per-type and total attempt budgets that escalate to a human,
   - a dial allow-list,
   - duplicate-report and stale-callback suppression,
   - confirmation accepted only after delivery is verified.

**Fallback ladder (rehearse each switch once):**
1. Phone line fails → `/pilot` in the browser.
2. adsb.lol fails → automatic labeled sample (`ALLOW_AIRCRAFT_FALLBACK=1`).
3. Real unit calls fail → `SIM_UNITS=1`, restart.
4. Integrated app fails → `MOCK_DISPATCH=1 uvicorn dev_main:app --port 8000` (the voice loop proven earlier).
5. Everything fails → play the recorded video.

---

## 7. Rules (unchanged from CLAUDE.md, plus integration specifics)

- Never commit `.env`, `.vapi_ids.json` or `logs/`. Never print keys. (Checked: the Vapi key is not in git history.)
- **Ask before any real outbound call.** Each one rings a teammate and spends credits.
- Work on the `integration` branch. Only merge to `main` with `pytest -q` fully green and `scripts/demo_backend.py` closing an incident.
- Don't change the shared contract or the event/snapshot shapes the observer reads (`incidents[].aircraft`, `incidents[].nearby_airports`, `events[].kind/message/data.voice_event`) without telling the Frontend teammate.
- After each step in Section 4, report the 1–2 commands that prove it works, then update this file's status lines.
