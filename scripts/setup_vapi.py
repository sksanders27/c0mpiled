"""Idempotently create/update both assistants and attach the phone number to the pilot assistant.

    .venv/bin/python scripts/setup_vapi.py            # push config
    .venv/bin/python scripts/setup_vapi.py --dry-run  # print the JSON, touch nothing

Re-run after editing voice/prompts/*.md: it updates the existing assistants (by id or by name).
"""
from __future__ import annotations

import argparse
import asyncio
import json
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from voice.assistants import PILOT_NAME, UNIT_NAME, pilot_assistant, unit_assistant  # noqa: E402
from voice.config import VAPI_IDS_PATH, load_vapi_ids, settings  # noqa: E402
from voice.vapi_client import VapiClient, VapiError  # noqa: E402

UNKNOWN_PROP = re.compile(r"property (\w+) should not exist")


async def push(client: VapiClient, cfg: dict, existing_id: str | None) -> dict:
    """Create or update. If Vapi rejects a top-level field we used, drop it and retry (with a warning)."""
    cfg = dict(cfg)
    for _ in range(5):
        try:
            if existing_id:
                return await client.update_assistant(existing_id, cfg)
            return await client.create_assistant(cfg)
        except VapiError as e:
            bad = [p for p in UNKNOWN_PROP.findall(e.body) if p in cfg]
            if e.status == 400 and bad:
                for p in bad:
                    print(f"  ! Vapi rejected field {p!r}; dropping it")
                    cfg.pop(p)
                continue
            if e.status == 404 and existing_id:
                print(f"  ! assistant {existing_id} not found; creating a new one")
                existing_id = None
                continue
            raise
    raise RuntimeError("could not push assistant config")


async def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()

    if not settings.public_base_url.startswith("https://"):
        sys.exit("PUBLIC_BASE_URL must be your https ngrok URL (e.g. https://<name>.ngrok-free.app)")
    configs = {"pilot": pilot_assistant(settings), "unit": unit_assistant(settings)}
    if args.dry_run:
        print(json.dumps(configs, indent=2).replace(settings.vapi_webhook_secret or "\0", "<secret>"))
        return
    if not settings.vapi_api_key:
        sys.exit("VAPI_API_KEY missing from .env")

    client = VapiClient(settings.vapi_api_key, timeout=30)
    try:
        ids = load_vapi_ids()
        by_name = {a.get("name"): a["id"] for a in await client.list_assistants()}
        out = dict(ids)
        for key, name in (("pilot", PILOT_NAME), ("unit", UNIT_NAME)):
            existing = ids.get(f"{key}_assistant_id") or by_name.get(name)
            print(f"{'Updating' if existing else 'Creating'} {name} ...")
            a = await push(client, configs[key], existing)
            out[f"{key}_assistant_id"] = a["id"]
            print(f"  ok: {a['id']}")

        if settings.vapi_phone_number_id:
            await client.attach_assistant_to_number(settings.vapi_phone_number_id, out["pilot_assistant_id"])
            num = await client.get_phone_number(settings.vapi_phone_number_id)
            out["phone_number"] = num.get("number")
            out["phone_provider"] = num.get("provider")
            print(f"Attached {PILOT_NAME} to {num.get('number')} (provider: {num.get('provider')})")
            if num.get("provider") == "vapi":
                print("  ! Free Vapi numbers can't place OUTBOUND calls. Real unit calls (milestone 4) need a "
                      "Twilio/Telnyx/Vonage number imported into Vapi. Inbound + SIM_UNITS=1 work fine.")
        else:
            print("VAPI_PHONE_NUMBER_ID not set; skipped phone attach")

        VAPI_IDS_PATH.write_text(json.dumps(out, indent=2) + "\n")
        print(f"Wrote {VAPI_IDS_PATH.name}")
        print(f"Webhook URL: {settings.webhook_url}")
    finally:
        await client.aclose()


if __name__ == "__main__":
    asyncio.run(main())
