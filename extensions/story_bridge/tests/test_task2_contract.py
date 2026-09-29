from pathlib import Path
import asyncio
import threading
import wave
from concurrent.futures import ThreadPoolExecutor

from fastapi.testclient import TestClient

from extensions.story_bridge.api import LeasedFileResponse, create_app
from extensions.story_bridge.config import Settings
from extensions.story_bridge.gradio_bridge import BridgeError
from extensions.story_bridge.temp_audio import AudioStore


class FakeBridge:
    def __init__(self, store):
        self.store = store
        self.loaded = False
        self.calls = []

    def load_model(self):
        self.loaded = True
        return {"model_loaded": True}

    def voices(self):
        return [{"id": "ly", "name": "Ly"}]

    def synthesize(self, request):
        self.calls.append(request)
        path = self.store.generated_dir / "speech.wav"
        with wave.open(str(path), "wb") as output:
            output.setnchannels(1)
            output.setsampwidth(2)
            output.setframerate(8000)
            output.writeframes(b"\0\0" * 80)
        return self.store.register(path, created_at=1000)


def test_speech_returns_binary_and_releases_read_lease(tmp_path: Path):
    clock = [1000.0]
    store = AudioStore(tmp_path / "audio", clock=lambda: clock[0])
    bridge = FakeBridge(store)
    app = create_app({"cors_origins": ["http://localhost:5173"]}, bridge, store, mount_gradio=False)
    with TestClient(app) as client:
        response = client.post("/api/v1/speech", json={"text": "xin chao"})
        assert response.status_code == 200
        assert response.headers["content-type"] == "audio/wav"
        assert response.content.startswith(b"RIFF")
        assert response.headers["x-audio-id"]
        assert response.headers["cache-control"] == "no-store"
    assert store._leases == {}


def test_speech_rejects_unknown_and_invalid_fields(tmp_path: Path):
    store = AudioStore(tmp_path / "audio")
    app = create_app({}, FakeBridge(store), store, mount_gradio=False)
    with TestClient(app) as client:
        assert client.post("/api/v1/speech", json={"text": "   "}).status_code == 422
        assert client.post("/api/v1/speech", json={"text": "ok", "unknown": 1}).status_code == 422
        assert client.post("/api/v1/speech", json={"text": "ok", "temperature": 2}).status_code == 422


def test_cors_exposes_audio_headers(tmp_path: Path):
    store = AudioStore(tmp_path / "audio")
    app = create_app({"cors_origins": ["http://localhost:5173"]}, FakeBridge(store), store, mount_gradio=False)
    with TestClient(app) as client:
        response = client.options(
            "/api/v1/speech",
            headers={
                "Origin": "http://localhost:5173",
                "Access-Control-Request-Method": "POST",
                "Access-Control-Request-Headers": "content-type",
            },
        )
        assert response.status_code == 200
        assert response.headers["access-control-allow-origin"] == "http://localhost:5173"
        speech = client.post(
            "/api/v1/speech", json={"text": "ok"}, headers={"Origin": "http://localhost:5173"}
        )
        assert "X-Audio-Id" in speech.headers["access-control-expose-headers"]


def test_upstream_failure_is_json_503(tmp_path: Path):
    store = AudioStore(tmp_path / "audio")

    class BrokenBridge(FakeBridge):
        def synthesize(self, request):
            raise BridgeError("synthesis failed")

    app = create_app({}, BrokenBridge(store), store, mount_gradio=False)
    with TestClient(app) as client:
        response = client.post("/api/v1/speech", json={"text": "hello"})
        assert response.status_code == 503
        assert response.headers["content-type"].startswith("application/json")
        assert response.json() == {"detail": "synthesis failed"}


def test_unexpected_upstream_exception_has_no_raw_traceback(tmp_path: Path):
    store = AudioStore(tmp_path / "audio")

    class BrokenBridge(FakeBridge):
        def synthesize(self, request):
            raise RuntimeError("private internal detail")

    app = create_app({}, BrokenBridge(store), store, mount_gradio=False)
    with TestClient(app) as client:
        response = client.post("/api/v1/speech", json={"text": "hello"})
        assert response.status_code == 503
        assert response.headers["content-type"].startswith("application/json")
        assert "private internal detail" not in response.text


def test_full_pending_queue_returns_429(tmp_path: Path):
    store = AudioStore(tmp_path / "audio", clock=lambda: 1000.0)
    entered = threading.Event()
    release = threading.Event()

    class SlowBridge(FakeBridge):
        def synthesize(self, request):
            entered.set()
            release.wait(timeout=5)
            return super().synthesize(request)

    app = create_app({"max_pending_speech": 1}, SlowBridge(store), store, mount_gradio=False)
    with TestClient(app) as client, ThreadPoolExecutor(max_workers=1) as pool:
        first = pool.submit(client.post, "/api/v1/speech", json={"text": "one"})
        assert entered.wait(timeout=2)
        busy = client.post("/api/v1/speech", json={"text": "two"})
        assert busy.status_code == 429
        release.set()
        assert first.result(timeout=5).status_code == 200


def test_ui_and_client_routes_pick_up_files_created_after_startup(tmp_path: Path):
    store = AudioStore(tmp_path / "audio")
    ui = tmp_path / "ui"
    client_dir = tmp_path / "client"
    app = create_app({"ui_dir": ui, "client_dir": client_dir}, FakeBridge(store), store, mount_gradio=False)
    with TestClient(app) as client:
        assert client.get("/").status_code == 404
        ui.mkdir()
        client_dir.mkdir()
        (ui / "index.html").write_text("<h1>Story</h1>", encoding="utf-8")
        (client_dir / "speech-client.js").write_text("export const speech = true;", encoding="utf-8")
        assert client.get("/").text == "<h1>Story</h1>"
        assert "export const speech" in client.get("/client/speech-client.js").text


def test_wildcard_bind_uses_loopback_for_gradio_client(monkeypatch):
    monkeypatch.setenv("STORY_BRIDGE_HOST", "0.0.0.0")
    monkeypatch.setenv("STORY_BRIDGE_PORT", "18123")
    settings = Settings.from_env()
    assert settings.host == "0.0.0.0"
    assert settings.upstream_url == "http://127.0.0.1:18123/_gradio"


def test_file_response_releases_lease_when_client_send_breaks(tmp_path: Path):
    store = AudioStore(tmp_path / "audio", clock=lambda: 1000)
    path = store.generated_dir / "speech.wav"
    path.write_bytes(b"RIFF" + b"\0" * 128)
    asset = store.register(path, created_at=1000)
    leased = store.acquire(path)
    response = LeasedFileResponse(path, store=store, asset_id=leased.id)

    async def interrupted():
        async def receive():
            return {"type": "http.disconnect"}

        async def send(message):
            raise ConnectionError("disconnected")

        await response({"type": "http", "method": "GET", "headers": []}, receive, send)

    try:
        asyncio.run(interrupted())
    except ConnectionError:
        pass
    assert store._leases == {}
