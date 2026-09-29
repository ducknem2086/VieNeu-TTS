# Story Bridge Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add an isolated in-repository story bridge that returns Gradio-generated WAV bytes for JavaScript new-tab playback, with five-minute server retention.

**Architecture:** A single-process FastAPI launcher mounts the original Gradio app and submits work to its existing story event. A small persistent audio registry owns temporary audio and cache cleanup. A browser-independent ES module powers a Vietnamese UI and arbitrary external JavaScript callers.

**Tech Stack:** Python 3.12, existing Gradio 5.49.1, gradio-client 1.13.3, FastAPI, SQLite/standard-library files, native JS/CSS/HTML, pytest, node:test.

**Spec:** `extensions/story_bridge/docs/design.md`

## Global Constraints

- All shipped changes live in `extensions/story_bridge/`.
- Reuse the actual Gradio story-button callback; do not reproduce inference or edit upstream source.
- Audio expires 300 seconds after generation completes, never 300 seconds after request submission.
- Model/reference assets outside the extension-owned runtime directory must never be deleted.
- Server expiry cannot revoke bytes a client has already received; document this limitation.
- One Uvicorn worker; shared Gradio model load/synthesis concurrency limit 1.
- API returns WAV binary, not JSON audio, base64, a presigned URL, or a Blob URL.
- Work in the current user-selected checkout; no upstream tracked files may change.

## Task 1: Temporary audio lifecycle

Create `temp_audio.py`, `__init__.py`, `.gitignore`, and `tests/test_temp_audio.py` inside the extension. Use standard library only for lifecycle code. The runtime folder defaults later to `extensions/story_bridge/.runtime/`; ignore it locally.

Interfaces produced:

```python
@dataclass(frozen=True)
class AudioAsset:
    id: str
    path: Path
    created_at: float
    expires_at: float

class AudioExpired(Exception): pass

class AudioStore:
    def __init__(self, root: Path, ttl_seconds: float = 300, clock: Callable[[], float] = time.time): ...
    # Properties generated_dir and cache_dir are owned subdirectories of root.
    def register(self, path: Path, created_at: float | None = None) -> AudioAsset: ...
    def find(self, path: Path) -> AudioAsset | None: ...
    def link_cache_copies(self, asset: AudioAsset) -> None: ...
    def acquire(self, path: Path) -> AudioAsset: ...  # raise AudioExpired for expired; lease pins active reads
    def release(self, asset_id: str) -> None: ...
    def sweep(self) -> int: ...  # remove only expired managed files, including their cache aliases
    def close(self) -> None: ...
```

The interface blocks use ellipses as signature notation, not implementation placeholders. Use a lightweight persistent registry (SQLite recommended), short thread-safe transactions, and absolute resolved path containment. Register accepts only real generated output under generated_dir; use file mtime when created_at omitted. Repeated registration preserves original expiry. Cache copies under cache_dir retain the original expiry and must match original output (unique basename plus content verification). Discovery during sweep catches copies created after callback registration. find can discover a managed cache alias by its source association; no arbitrary filesystem deletion. Leases apply to an asset and its aliases. PermissionError during sweep leaves work retryable. Crash/restart must retain or recover expired artifacts; never remove unregistered external files.

- [ ] Write behavior tests before implementation and observe expected RED.

```python
clock = [1000.0]
store = AudioStore(tmp_path / 'owned', clock=lambda: clock[0])
path = store.generated_dir / 'speech.wav'
path.write_bytes(b'wave-test')
asset = store.register(path, created_at=1000)
clock[0] = 1299
assert store.acquire(path).id == asset.id
store.release(asset.id)
clock[0] = 1300
with pytest.raises(AudioExpired): store.acquire(path)
store.sweep()
assert not path.exists()
```

Also cover restart, re-registration, copied cache output, late-created aliases, active read leases, outside paths/symlinks, and retryable deletion failures. Derive expected deadlines independently.
- [ ] Implement the interface and run `.venv/Scripts/python.exe -m pytest extensions/story_bridge/tests/test_temp_audio.py -q`.
- [ ] Commit only task files, self-review, and record RED/GREEN evidence in the report.

## Task 2: Gradio bridge, FastAPI, and launcher

Create `gradio_bridge.py`, `api.py`, `launcher.py`, `__main__.py`, `requirements.txt`, `requirements-dev.txt`, and Python integration tests under the extension. Consume Task 1's AudioStore. Keep configuration and errors explicit; split a small `config.py` if useful.

Interfaces produced:

```python
class GradioBridge:
    def __init__(self, upstream, store: AudioStore, upstream_url: str): ...
    def install(self) -> None: ...  # validate/install once, record generated paths without altering event outputs
    def load_model(self) -> dict: ...
    def voices(self) -> list[dict]: ...  # id/name, no private voice vectors
    def synthesize(self, request) -> AudioAsset: ...

def create_app(settings, bridge, store, *, mount_gradio=True): ...
```

Use the public Gradio HTTP client/event queue for synthesis; discover api_name from upstream.gen_event's binding, not a fixed fn_index/name. State components are omitted from client public inputs. The current story callback's 12 inputs are text, voice, custom_audio, custom_text, preset_mode state, Standard mode, use_batch, batch_size, temperature, max_chars, denoise, session state. Explicitly validate this shape. Model loading uses upstream.load_event defaults; validate success by upstream.model_loaded and tts, not HTTP status alone. API speech maps request fields into this existing event and validates that the returned artifact is a registered nonempty WAV. Client(download_files=False) avoids extra disk copies. Install a generator wrapper on synthesis binding(s) that registers completed WAVs before yielding their unchanged tuple and discovers cache copies in finally. Preserve original generator behavior. Configure load/synthesis bindings with one shared Gradio concurrency group before mounting/queue startup.

The launcher configures dedicated tempfile and GRADIO_TEMP_DIR paths before importing upstream, mounts it at `/_gradio`, and listens by default on 127.0.0.1:8002 with workers=1. Local loopback client uses that same port; the extension's launcher is required. Configurable host/port and CORS origin list (default http://localhost:5173 and http://localhost:3000). The root environment already supplies SDK and Gradio. Requirements list fastapi, uvicorn, gradio-client compatible with Gradio 5; document later that the root SDK setup is a prerequisite. No model download on import/startup; load through explicit POST or first speech call. Limits: text nonblank <=20000, temperature .1..1.5 finite, chunks 128..512, batch 1..64, reject unknown fields. Bound pending speech requests (default 4), map excess to 429, upstream failure to JSON error, no raw tracebacks.

Routes: GET /health returns model_loaded; GET /api/v1/voices returns {voices:[{id,name}]}; POST /api/v1/model/load; POST /api/v1/speech returns FileResponse/audio/wav with inline speech.wav, Cache-Control:no-store, X-Audio-Id, X-Audio-Expires-At ISO UTC. Use a read lease released in finally even on disconnect. Background sweep every 5 seconds plus startup sweep; persisted stale records are cleaned after restart. Guard Gradio managed-file reads with the same expiry, including Range requests, while preserving built-in/model/reference assets. CORS exposes audio headers, allows configured origins only. Serve later Task 3 UI at / and browser client assets beneath /client/; endpoints must work before frontend files are present.

- [ ] Write failing tests for binary WAV response, rejected input, CORS/preflight/headers, failure and queue handling, expired Gradio file access, and read lease lifecycle.
- [ ] Create a real lightweight gr.Blocks fixture with a generator writing a tiny WAV, State inputs, and story/load events. Run an ephemeral localhost Uvicorn instance in tests; exercise the real Gradio client transport and cache creation. Mock only model inference, not the bridge callback/HTTP/cache path.
- [ ] Implement and run `.venv/Scripts/python.exe -m pytest extensions/story_bridge/tests -q`.
- [ ] Commit only task files, self-review, and report tests plus compatibility concerns.

## Task 3: Browser client, UI, and user documentation

Create `client/speech-client.js`, `client/package.json` (type module), `ui/index.html`, `ui/styles.css`, `ui/app.js`, `tests/speech-client.test.mjs`, and `README.md` in the extension. Consume Task 2 public HTTP contracts. Native ES modules only, no bundler.

```javascript
export function createSpeechClient({baseUrl = '', fetchImpl = globalThis.fetch} = {}) {
  // synthesize(payload, {signal} = {}) -> Promise<{blob, audioId, expiresAt}>
  // openInNewTab(payload, {signal} = {}) -> Promise<{url, window, dispose, expiresAt}>
}
```

Implement real fetch payload and Content-Type validation; expose readable errors for JSON/non-JSON HTTP failures. openInNewTab must open about:blank during the synchronous click stack before its first await. Popup-blocked means no network request. Navigate the resulting tab to a MIME-correct Blob URL, close the placeholder on failure, and revoke the URL/clear its timer when the child closes or dispose is called. Never revoke immediately on load. No forced expiry of already-downloaded audio. synthesize also works in modern Node via fetch/Blob; opening tabs is browser-only.

UI: responsive Vietnamese reading workspace with a large text area, voice selector, collapsible temperature/chunk/batch controls, load-model action, readiness/status/error feedback, and a primary 'Bắt đầu' action opening the native audio tab. Make the result/tab reopening usable without recomputing if the JS already holds the Blob (retain latest result only and clean it when replaced, if needed). Keyboard/accessibility labels and disabled busy states; no hardcoded model performance claims, credentials, or giant frontend framework. Keep generated audio outside the page's DOM; output is the native browser tab.

- [ ] Write Node behavior tests for fetch payload/binary result, errors, popup-before-fetch ordering, blocked popup, and URL cleanup using small browser boundary doubles. Run `node --test extensions/story_bridge/tests/speech-client.test.mjs` for RED before client implementation.
- [ ] Implement client/UI and run Node tests to GREEN.
- [ ] Document root SDK installation prerequisite; `python -m pip install -r extensions/story_bridge/requirements.txt`; `.venv/Scripts/python.exe -m extensions.story_bridge`; http://127.0.0.1:8002; configured CORS example; curl binary example; JS button example. Explain TTL, restart cleanup, Blob URL scope, one-worker restriction, shared global Gradio stop/model semantics, and upgrade compatibility checks.
- [ ] Verify UI with a real browser if available, using a test WAV response without claiming real-model inference. Commit task files and report focused checks.
