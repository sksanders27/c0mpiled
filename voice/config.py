"""Typed settings loaded from .env (falls back to a file named `env`)."""
from __future__ import annotations

import json
import os
from dataclasses import dataclass, field
from pathlib import Path

from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parent.parent
VAPI_IDS_PATH = ROOT / ".vapi_ids.json"

for _name in (".env", "env"):
    if (ROOT / _name).exists():
        load_dotenv(ROOT / _name, override=False)


def _flag(name: str, default: bool = False) -> bool:
    v = os.getenv(name)
    if v is None or v.strip() == "":
        return default
    return v.strip().lower() in ("1", "true", "yes", "on")


def _int(name: str, default: int) -> int:
    try:
        return int(os.getenv(name, "") or default)
    except ValueError:
        return default


def _float(name: str, default: float) -> float:
    try:
        return float(os.getenv(name, "") or default)
    except ValueError:
        return default


@dataclass
class Settings:
    vapi_api_key: str = ""
    vapi_public_key: str = ""
    vapi_phone_number_id: str = ""
    vapi_webhook_secret: str = ""
    public_base_url: str = ""
    voice_model_provider: str = "openai"
    voice_model: str = "gpt-4o-mini"
    unit_phones: list[str] = field(default_factory=list)
    backend_base_url: str = ""
    mock_dispatch: bool = False
    sim_units: bool = True
    scripted_decline_first: bool = False
    unit_call_timeout_s: float = 45.0
    max_outbound_calls: int = 4
    sim_unit_delay_s: float = 4.0
    mock_dispatch_delay_s: float = 2.0
    log_dir: Path = ROOT / "logs"

    @classmethod
    def from_env(cls) -> "Settings":
        phones = [os.getenv(f"UNIT_PHONE_{i}", "").strip() for i in range(1, 10)]
        return cls(
            vapi_api_key=os.getenv("VAPI_API_KEY", ""),
            vapi_public_key=os.getenv("VAPI_PUBLIC_KEY", ""),
            vapi_phone_number_id=os.getenv("VAPI_PHONE_NUMBER_ID", ""),
            vapi_webhook_secret=os.getenv("VAPI_WEBHOOK_SECRET", ""),
            public_base_url=os.getenv("PUBLIC_BASE_URL", "").rstrip("/"),
            voice_model_provider=os.getenv("VOICE_MODEL_PROVIDER", "openai"),
            voice_model=os.getenv("VOICE_MODEL", "gpt-4o-mini"),
            unit_phones=[p for p in phones if p],
            backend_base_url=os.getenv("BACKEND_BASE_URL", ""),
            mock_dispatch=_flag("MOCK_DISPATCH"),
            sim_units=_flag("SIM_UNITS", default=True),
            scripted_decline_first=_flag("SCRIPTED_DECLINE_FIRST"),
            unit_call_timeout_s=_float("UNIT_CALL_TIMEOUT_S", 45.0),
            max_outbound_calls=_int("MAX_OUTBOUND_CALLS", 4),
            sim_unit_delay_s=_float("SIM_UNIT_DELAY_S", 4.0),
            mock_dispatch_delay_s=_float("MOCK_DISPATCH_DELAY_S", 2.0),
        )

    @property
    def webhook_url(self) -> str:
        return f"{self.public_base_url}/vapi/webhook"

    def unit_phone(self, n: int) -> str:
        """Phone for fake unit n (1-based), cycling through the configured ones."""
        if not self.unit_phones:
            return f"+1555000000{n}"
        return self.unit_phones[(n - 1) % len(self.unit_phones)]


def load_vapi_ids() -> dict:
    try:
        return json.loads(VAPI_IDS_PATH.read_text())
    except (FileNotFoundError, json.JSONDecodeError):
        return {}


settings = Settings.from_env()
