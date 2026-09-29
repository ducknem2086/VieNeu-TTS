"""Adapter around the upstream Gradio story and model-load events."""

from __future__ import annotations

import threading
import wave
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .temp_audio import AudioAsset, AudioStore


class BridgeError(RuntimeError):
    """The upstream Gradio application could not satisfy a bridge request."""


@dataclass(frozen=True)
class SpeechRequest:
    text: str
    voice_id: str | None = None
    temperature: float = 0.8
    max_chars_per_chunk: int = 256
    batch_size: int = 16
    use_batch: bool = True


class GradioBridge:
    def __init__(self, upstream: Any, store: AudioStore, upstream_url: str):
        self.upstream = upstream
        self.store = store
        self.upstream_url = upstream_url.rstrip("/")
        self._lock = threading.RLock()
        self._installed = False
        self._story_binding = None
        self._load_binding = None
        self._client = None
        self._story_api_name = None
        self._load_api_name = None

    def _binding(self, event: Any):
        event_id = event.get("id") if isinstance(event, dict) else getattr(event, "id", None)
        if event_id is not None:
            try:
                return self.upstream.demo.fns[event_id]
            except (AttributeError, IndexError, KeyError):
                pass
        # Gradio 5.49 omits Dependency.id until configuration serialization.
        # Identity matching keeps discovery runtime based without relying on a
        # fixed fn index or API name.
        target_fn = getattr(event, "fn", None)
        for binding in getattr(self.upstream.demo, "fns", {}).values():
            if getattr(binding, "fn", None) is target_fn:
                return binding
        raise BridgeError("upstream event binding is unavailable")

    @staticmethod
    def _component_kind(component: Any) -> str:
        return type(component).__name__.lower()

    def install(self) -> None:
        with self._lock:
            if self._installed:
                return
            story_event = getattr(self.upstream, "gen_event", None)
            load_event = getattr(self.upstream, "load_event", None)
            if story_event is None or load_event is None:
                raise BridgeError("upstream story/load events are missing")
            story = self._binding(story_event)
            load = self._binding(load_event)
            kinds = [self._component_kind(item) for item in getattr(story, "inputs", ())]
            expected = [
                "textbox", "dropdown", "audio", "textbox", "state", "radio",
                "checkbox", "slider", "slider", "slider", "checkbox", "state",
            ]
            if kinds != expected:
                raise BridgeError(f"unexpected story input shape: {kinds!r}")
            if len(getattr(story, "outputs", ())) != 3:
                raise BridgeError("story event must have three outputs")
            if not getattr(story, "types_generator", False):
                raise BridgeError("story event must remain a generator")
            # Keep all model actions in one queue lane. Gradio reads these mutable
            # attributes when queue startup occurs.
            synthesis_bindings = [story]
            for event_name in ("clone_gen_event", "conv_gen_event", "srt_gen_event"):
                event = getattr(self.upstream, event_name, None)
                if event is not None:
                    synthesis_bindings.append(self._binding(event))
            for binding in (load, *synthesis_bindings):
                binding.concurrency_id = "story-bridge-model"
                binding.concurrency_limit = 1
            # Only generated audio leaves through these components. Ordinary
            # inputs and reference previews keep Gradio's sibling cache.
            for binding in synthesis_bindings:
                binding.outputs[0].GRADIO_CACHE = str(self.store.cache_dir)
            download_button = getattr(self.upstream, "download_btn", None)
            if download_button is not None:
                download_button.GRADIO_CACHE = str(self.store.cache_dir)
            for binding in synthesis_bindings:
                if not getattr(binding, "types_generator", False):
                    raise BridgeError("synthesis event must remain a generator")
                original = binding.fn

                def capture(*args, _original=original, **kwargs):
                    captured: list[AudioAsset] = []
                    try:
                        for value in _original(*args, **kwargs):
                            asset = self._capture_output(value)
                            if asset is not None:
                                captured.append(asset)
                            yield value
                    finally:
                        for asset in captured:
                            self.store.link_cache_copies(asset)

                capture.__name__ = getattr(original, "__name__", "story_callback")
                capture.__wrapped__ = original
                binding.fn = capture
            self._story_binding = story
            self._load_binding = load
            self._story_api_name = getattr(story, "api_name", None) or getattr(story_event, "api_name", None)
            self._load_api_name = getattr(load, "api_name", None) or getattr(load_event, "api_name", None)
            if not self._story_api_name or not self._load_api_name:
                raise BridgeError("upstream event api names are unavailable")
            self._installed = True

    def _capture_output(self, value: Any) -> AudioAsset | None:
        candidate = value[0] if isinstance(value, (tuple, list)) and value else value
        path = self._path_from_value(candidate)
        if path is None:
            return None
        try:
            asset = self.store.find(path)
            if asset is None and path.exists() and path.parent == self.store.generated_dir:
                asset = self.store.register(path)
            if asset is not None:
                self.store.link_cache_copies(asset)
            return asset
        except (OSError, ValueError):
            # A callback's output must never be changed by registry bookkeeping.
            return None

    @staticmethod
    def _path_from_value(value: Any) -> Path | None:
        if isinstance(value, Path):
            return value
        if isinstance(value, str) and value.lower().endswith(".wav"):
            return Path(value)
        if isinstance(value, dict):
            for key in ("path", "name"):
                if value.get(key):
                    return Path(value[key])
        return None

    def _client_or_create(self):
        if self._client is None:
            from gradio_client import Client

            self._client = Client(self.upstream_url, download_files=False, verbose=False)
        return self._client

    def load_model(self) -> dict:
        self.install()
        with self._lock:
            binding = self._load_binding
            defaults = [getattr(component, "value", None) for component in binding.inputs]
            try:
                self._client_or_create().predict(*defaults, api_name="/" + self._load_api_name.lstrip("/"))
            except Exception as exc:
                raise BridgeError(f"model load request failed: {exc}") from exc
            if not bool(getattr(self.upstream, "model_loaded", False)) or getattr(self.upstream, "tts", None) is None:
                raise BridgeError("upstream model did not become ready")
            return {"model_loaded": True}

    def voices(self) -> list[dict]:
        tts = getattr(self.upstream, "tts", None)
        if tts is None or not bool(getattr(self.upstream, "model_loaded", False)):
            return []
        try:
            values = tts.list_preset_voices()
        except Exception as exc:
            raise BridgeError(f"voice discovery failed: {exc}") from exc
        result = []
        for value in values or []:
            if isinstance(value, (tuple, list)) and len(value) >= 2:
                result.append({"id": str(value[1]), "name": str(value[0])})
            else:
                result.append({"id": str(value), "name": str(value)})
        return result

    def synthesize(self, request: SpeechRequest | Any) -> AudioAsset:
        self.install()
        with self._lock:
            if not bool(getattr(self.upstream, "model_loaded", False)) or getattr(self.upstream, "tts", None) is None:
                self.load_model()
            voice_id = request.voice_id
            if not voice_id:
                available = self.voices()
                default = getattr(self.upstream.tts, "_default_voice", None)
                identifiers = {voice["id"] for voice in available}
                voice_id = default if default in identifiers else (available[0]["id"] if available else None)
            if not voice_id:
                raise BridgeError("loaded model has no preset voice")
            args = [
                request.text,
                voice_id,
                None,
                "",
                getattr(self._story_binding.inputs[5], "value", None),
                bool(request.use_batch),
                int(request.batch_size),
                float(request.temperature),
                int(request.max_chars_per_chunk),
                False,
            ]
            try:
                output = self._client_or_create().predict(*args, api_name="/" + self._story_api_name.lstrip("/"))
            except Exception as exc:
                raise BridgeError(f"speech request failed: {exc}") from exc
            candidate = output[0] if isinstance(output, (tuple, list)) and output else output
            path = self._path_from_value(candidate)
            if path is None:
                raise BridgeError("upstream returned no WAV artifact")
            asset = self.store.find(path)
            if asset is None:
                self._capture_output(candidate)
                asset = self.store.find(path)
            if asset is None or not asset.path.exists() or asset.path.stat().st_size <= 0:
                raise BridgeError("upstream returned an unregistered or empty WAV")
            try:
                with wave.open(str(asset.path), "rb") as wav:
                    if wav.getnframes() <= 0:
                        raise BridgeError("upstream returned an empty WAV")
            except (OSError, EOFError, wave.Error) as exc:
                raise BridgeError("upstream returned an invalid WAV") from exc
            self.store.link_cache_copies(asset)
            return asset
