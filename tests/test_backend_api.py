from fastapi.testclient import TestClient

from backend.config import BackendSettings
from backend.offline import OfflineVapiClient
from main import create_app
from voice.config import Settings


def test_integrated_api_snapshot_and_auth(tmp_path):
    voice = Settings(log_dir=tmp_path, mock_dispatch=False, sim_units=True)
    backend = BackendSettings(api_token="observer-secret")
    app = create_app(voice_settings=voice, backend_settings=backend,
                     vapi_client=OfflineVapiClient())

    with TestClient(app) as client:
        health = client.get("/health")
        assert health.status_code == 200
        assert health.json()["offline_voice"] is True
        assert health.json()["authentication_enabled"] is True

        assert client.get("/api/snapshot").status_code == 401
        headers = {"Authorization": "Bearer observer-secret"}
        response = client.get("/api/snapshot", headers=headers)
        assert response.status_code == 200
        snapshot = response.json()
        assert snapshot["incidents"] == []
        assert snapshot["simulation"]["aircraft_mode"] == "mock"

        units = client.get("/api/units", headers=headers).json()
        assert units
        assert all("phone" not in unit for unit in units)
        assert client.get("/observer/state", headers=headers).status_code == 200
