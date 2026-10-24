"""Make the test run independent of whoever's local .env / env file is present.

Runs before test modules import `main` (which builds an app at import time). voice.config loads
dotenv files with override=False, so values set here win.
"""
import os

os.environ.update({
    "MOCK_DISPATCH": "0",
    "OFFLINE_VOICE": "1",
    "SIM_UNITS": "1",
    "AIRCRAFT_MODE": "mock",
    "ALLOW_AIRCRAFT_FALLBACK": "1",
    "SCRIPTED_DECLINE_FIRST": "0",
    "API_TOKEN": "",
    "VAPI_WEBHOOK_SECRET": "",
})
