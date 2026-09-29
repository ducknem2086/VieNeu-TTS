import socket
import tempfile
import threading
import time
import wave
from pathlib import Path
from types import SimpleNamespace

import gradio as gr
import httpx
import pytest
import uvicorn

from extensions.story_bridge.api import create_app
from extensions.story_bridge.gradio_bridge import GradioBridge
from extensions.story_bridge.temp_audio import AudioStore


@pytest.fixture
def live_bridge(tmp_path, monkeypatch):
    store = AudioStore(tmp_path / "owned")
    public_cache = store.root / "gradio"
    public_cache.mkdir()
    monkeypatch.setenv("GRADIO_TEMP_DIR", str(public_cache))
    monkeypatch.setattr(tempfile, "tempdir", str(store.generated_dir))
    upstream = SimpleNamespace(model_loaded=False, tts=None)

    def load(*args):
        upstream.model_loaded = True
        upstream.tts = SimpleNamespace(list_preset_voices=lambda: [("Ly", "ly")], _default_voice="ly")
        return "ready"

    def story(*args):
        assert len(args) == 12
        assert args[0] == "hello"
        assert args[1] == "ly"
        assert args[4] == "preset_mode"
        with tempfile.NamedTemporaryFile(delete=False, suffix=".wav") as file:
            path = Path(file.name)
        with wave.open(str(path), "wb") as wav:
            wav.setnchannels(1)
            wav.setsampwidth(2)
            wav.setframerate(8000)
            wav.writeframes(b"\0\0" * 80)
        yield str(path), "ok", ""

    with gr.Blocks() as demo:
        upstream.reference_audio = gr.Audio(type="filepath")
        upstream.download_btn = gr.DownloadButton()
        inputs = [
            gr.Textbox(), gr.Dropdown(choices=[("Ly", "ly")], value=None), gr.Audio(type="filepath"),
            gr.Textbox(), gr.State("preset_mode"), gr.Radio(choices=["Standard (Một lần)"], value="Standard (Một lần)"),
            gr.Checkbox(value=True), gr.Slider(1, 64, value=16), gr.Slider(.1, 1.5, value=.8),
            gr.Slider(128, 512, value=256), gr.Checkbox(value=False), gr.State(""),
        ]
        outputs = [gr.Audio(type="filepath"), gr.Textbox(), gr.Textbox()]
        upstream.gen_event = gr.Button("story").click(story, inputs=inputs, outputs=outputs)
        load_inputs = [gr.Textbox(value="model"), gr.Textbox(value="codec"), gr.Textbox(value="cpu"),
                       gr.Checkbox(value=False), gr.Textbox(), gr.Textbox(), gr.Textbox()]
        upstream.load_event = gr.Button("load").click(load, inputs=load_inputs, outputs=gr.Textbox())
    upstream.demo = demo
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        port = sock.getsockname()[1]
    bridge = GradioBridge(upstream, store, f"http://127.0.0.1:{port}/_gradio")
    app = create_app({}, bridge, store)
    server = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=port, log_level="warning"))
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()
    for _ in range(100):
        try:
            if httpx.get(f"http://127.0.0.1:{port}/health", timeout=.2).status_code == 200:
                break
        except httpx.TransportError:
            pass
        time.sleep(.05)
    else:
        raise AssertionError("test server did not start")
    yield f"http://127.0.0.1:{port}", bridge, store
    server.should_exit = True
    thread.join(timeout=10)
    store.close()


def test_real_gradio_client_queue_produces_binary_wav_and_cache_alias(live_bridge):
    url, bridge, store = live_bridge
    with httpx.Client(timeout=30) as client:
        load = client.post(url + "/api/v1/model/load")
        assert load.status_code == 200, load.text
        response = client.post(url + "/api/v1/speech", json={"text": "hello", "voice_id": "ly"})
        assert response.status_code == 200, response.text
        assert response.content[:4] == b"RIFF"
    assert len(list(store.generated_dir.glob("*.wav"))) == 1
    assert len(list(store.cache_dir.rglob("*.wav"))) >= 1


def test_managed_gradio_range_read_expires_and_unknown_copy_is_denied(live_bridge):
    url, bridge, store = live_bridge
    with httpx.Client(timeout=30) as client:
        response = client.post(url + "/api/v1/speech", json={"text": "hello", "voice_id": "ly"})
        assert response.status_code == 200, response.text
        alias = next(store.cache_dir.rglob("*.wav"))
        file_urls = [url + "/_gradio/gradio_api/file" + separator + alias.as_posix()
                     for separator in ("=", "/")]
        for file_url in file_urls:
            for method, headers, expected in (("GET", {}, 200), ("GET", {"Range": "bytes=0-3"}, 206), ("HEAD", {}, 200)):
                active = client.request(method, file_url, headers=headers)
                # Gradio 5.49 registers HEAD only for the file= spelling.
                if method == "HEAD" and "/gradio_api/file/" in file_url:
                    expected = 405
                assert active.status_code == expected, active.text
                assert active.headers["cache-control"] == "no-store"
                assert active.content[:4] == (b"" if method == "HEAD" else b"RIFF")
                deadline = time.monotonic() + 1
                while store._leases and time.monotonic() < deadline:
                    time.sleep(.01)
                assert store._leases == {}
        asset = store.find(alias)
        store._clock = lambda: asset.expires_at
        for file_url in file_urls:
            for method, headers in (("GET", {}), ("GET", {"Range": "bytes=0-3"}), ("HEAD", {})):
                expired = client.request(method, file_url, headers=headers)
                assert expired.status_code == 410
                assert expired.headers["cache-control"] == "no-store"
        orphan = store.cache_dir / "unregistered.wav"
        orphan.write_bytes(b"RIFF")
        for separator in ("=", "/"):
            for method in ("GET", "HEAD"):
                unknown = client.request(method, url + "/_gradio/gradio_api/file" + separator + orphan.as_posix())
                assert unknown.status_code == 404
                assert unknown.headers["cache-control"] == "no-store"
        assert store.sweep() >= 2
        assert not alias.exists()


def test_gradio_native_story_call_is_captured_without_changing_outputs(live_bridge):
    _, bridge, store = live_bridge
    from gradio_client import Client

    client = Client(bridge.upstream_url, download_files=False, verbose=False)
    result = client.predict("hello", "ly", None, "", "Standard (Một lần)", True, 16, .8, 256, False,
                            api_name="/" + bridge._story_api_name)
    assert len(result) == 3
    assert result[1:] == ("ok", "") or result[1:] == ["ok", ""]
    assert store.find(Path(result[0]["path"])) is not None


def test_empty_or_non_wav_artifact_is_rejected(live_bridge):
    _, bridge, store = live_bridge
    path = store.generated_dir / "bad.wav"
    path.write_bytes(b"not a wav")
    asset = store.register(path)

    class BadClient:
        def predict(self, *args, **kwargs):
            return [{"path": str(path)}, "ok", ""]

    bridge.upstream.model_loaded = True
    bridge.upstream.tts = object()
    bridge._client = BadClient()
    from extensions.story_bridge.gradio_bridge import BridgeError, SpeechRequest

    with pytest.raises(BridgeError, match="WAV"):
        bridge.synthesize(SpeechRequest(text="hello", voice_id="ly"))


def test_missing_voice_uses_loaded_model_default(live_bridge):
    url, bridge, store = live_bridge
    with httpx.Client(timeout=30) as client:
        response = client.post(url + "/api/v1/speech", json={"text": "hello"})
        assert response.status_code == 200, response.text
        assert response.content[:4] == b"RIFF"


def test_direct_gradio_call_cannot_bypass_shared_queue(live_bridge):
    url, bridge, _ = live_bridge
    with httpx.Client(timeout=10) as client:
        response = client.post(
            url + "/_gradio/gradio_api/run/" + bridge._story_api_name,
            json={"data": ["hello", "ly", None, "", "Standard (Một lần)", True, 16, .8, 256, False]},
        )
        assert response.status_code == 404, response.text


def test_external_reference_preview_uses_public_cache_while_outputs_use_owned_cache(live_bridge, tmp_path):
    url, bridge, store = live_bridge
    from gradio.processing_utils import move_files_to_cache

    reference = tmp_path / "reference.wav"
    with wave.open(str(reference), "wb") as wav:
        wav.setnchannels(1)
        wav.setsampwidth(2)
        wav.setframerate(8000)
        wav.writeframes(b"\0\0" * 40)
    preview = bridge.upstream.reference_audio.postprocess(str(reference))
    cached = move_files_to_cache(preview, bridge.upstream.reference_audio)
    preview_path = Path(cached.path if hasattr(cached, "path") else cached["path"])
    assert preview_path.is_relative_to(store.root / "gradio")
    with httpx.Client(timeout=30) as client:
        for separator in ("=", "/"):
            response = client.get(url + "/_gradio/gradio_api/file" + separator + preview_path.as_posix())
            assert response.status_code == 200, response.text
            assert response.content[:4] == b"RIFF"
    assert bridge.upstream.demo.fns[bridge.upstream.gen_event["id"]].outputs[0].GRADIO_CACHE == str(store.cache_dir)
    assert bridge.upstream.download_btn.GRADIO_CACHE == str(store.cache_dir)


def test_download_button_copy_stays_guarded_and_expires(live_bridge):
    url, bridge, store = live_bridge
    with httpx.Client(timeout=30) as client:
        speech = client.post(url + "/api/v1/speech", json={"text": "hello"})
        assert speech.status_code == 200, speech.text
        source = next(store.generated_dir.glob("*.wav"))
        copied = Path(bridge.upstream.download_btn.move_resource_to_block_cache(source))
        assert copied.is_relative_to(store.cache_dir)
        asset = store.find(copied)
        assert asset is not None
        address = url + "/_gradio/gradio_api/file=" + copied.as_posix()
        assert client.get(address).status_code == 200
        store._clock = lambda: asset.expires_at
        assert client.get(address).status_code == 410
