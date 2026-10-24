"""Backend settings supplement the existing voice.config.Settings contract."""

from dataclasses import dataclass
import math
import os


@dataclass(frozen=True)
class BackendSettings:
    aircraft_mode: str = "mock"
    allow_aircraft_fallback: bool = True
    offline_voice: bool = True
    api_token: str | None = None
    max_attempts_per_type: int = 3
    max_total_attempts: int = 4
    assignment_timeout_seconds: float | None = None
    cors_origins: tuple[str, ...] = ("http://localhost:3000", "http://localhost:5173", "http://127.0.0.1:5173")

    def __post_init__(self):
        if self.aircraft_mode not in {"mock", "live"}:
            raise ValueError("AIRCRAFT_MODE must be mock or live")
        if self.max_attempts_per_type < 1 or self.max_total_attempts < 1:
            raise ValueError("Assignment budgets must be positive")
        if self.assignment_timeout_seconds is not None and (not math.isfinite(self.assignment_timeout_seconds) or self.assignment_timeout_seconds <= 0):
            raise ValueError("Assignment timeout must be finite and positive")

    @classmethod
    def from_env(cls):
        def flag(name, default):
            value = os.getenv(name, "").strip().lower()
            return default if not value else value in {"1", "true", "yes", "on"}
        return cls(
            aircraft_mode=os.getenv("AIRCRAFT_MODE", "mock"),
            allow_aircraft_fallback=flag("ALLOW_AIRCRAFT_FALLBACK", True),
            offline_voice=flag("OFFLINE_VOICE", True),
            api_token=os.getenv("API_TOKEN") or None,
            max_attempts_per_type=int(os.getenv("MAX_ATTEMPTS_PER_TYPE") or "3"),
            max_total_attempts=int(os.getenv("MAX_TOTAL_ATTEMPTS") or "4"),
            assignment_timeout_seconds=float(os.environ["ASSIGNMENT_TIMEOUT_SECONDS"]) if os.getenv("ASSIGNMENT_TIMEOUT_SECONDS") else None,
            cors_origins=tuple(x.strip() for x in os.getenv("CORS_ORIGINS", "http://localhost:3000,http://localhost:5173,http://127.0.0.1:5173").split(",") if x.strip()),
        )
