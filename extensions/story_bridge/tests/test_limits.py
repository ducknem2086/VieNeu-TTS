from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from extensions.story_bridge.api import create_app
from extensions.story_bridge.config import Settings
from extensions.story_bridge.temp_audio import AudioStore
from extensions.story_bridge.tests.test_task2_contract import FakeBridge


def _client(tmp_path: Path, settings=None):
    store = AudioStore(tmp_path / "audio", clock=lambda: 1000.0)  # FakeBridge registers at created_at=1000
    bridge = FakeBridge(store)
    return TestClient(create_app(settings or {}, bridge, store, mount_gradio=False)), bridge


def test_text_limit_counts_utf8_bytes(tmp_path):
    client, bridge = _client(tmp_path)
    with client:
        exact = "ạ" * 10240  # 3 bytes each = 30720 bytes
        assert client.post("/api/v1/speech", json={"text": exact}).status_code == 200
        over = exact + "a"
        response = client.post("/api/v1/speech", json={"text": over})
        assert response.status_code == 422
        assert "30720" in response.text
    assert len(bridge.calls) == 1


def test_text_limit_reads_settings(tmp_path):
    client, _ = _client(tmp_path, {"max_text_bytes": 8})
    with client:
        assert client.post("/api/v1/speech", json={"text": "12345678"}).status_code == 200
        assert client.post("/api/v1/speech", json={"text": "123456789"}).status_code == 422


def test_settings_from_env(monkeypatch):
    monkeypatch.setenv("STORY_BRIDGE_MAX_TEXT_BYTES", "1024")
    monkeypatch.setenv("STORY_BRIDGE_AUDIO_TTL", "60")
    monkeypatch.setenv("STORY_BRIDGE_UPSTREAM_TIMEOUT", "120")
    settings = Settings.from_env()
    assert settings.max_text_bytes == 1024
    assert settings.audio_ttl_seconds == 60
    assert settings.upstream_timeout_seconds == 120


@pytest.mark.parametrize("name", ["STORY_BRIDGE_MAX_TEXT_BYTES", "STORY_BRIDGE_AUDIO_TTL", "STORY_BRIDGE_UPSTREAM_TIMEOUT"])
def test_settings_reject_non_positive(monkeypatch, name):
    monkeypatch.setenv(name, "0")
    with pytest.raises(ValueError):
        Settings.from_env()


def test_settings_defaults(monkeypatch):
    for name in ("STORY_BRIDGE_MAX_TEXT_BYTES", "STORY_BRIDGE_AUDIO_TTL", "STORY_BRIDGE_UPSTREAM_TIMEOUT"):
        monkeypatch.delenv(name, raising=False)
    settings = Settings.from_env()
    assert (settings.max_text_bytes, settings.audio_ttl_seconds, settings.upstream_timeout_seconds) == (30720, 300.0, 900.0)
