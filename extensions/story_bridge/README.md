# Story Bridge

Story Bridge adds a Vietnamese reading workspace at `/` and a JSON-to-WAV API for JavaScript applications. It is an isolated extension of this VieNeu-TTS checkout. Audio uses the existing Gradio story-generation event and the same model instance; it does not implement a second inference path.

## Install and run

From the repository root, first install this repository's Python SDK and its Gradio dependencies following the root [README](../../README.md). For a local editable install, `python -m pip install -e .` is one option. Then install the bridge's extra dependencies:

```powershell
python -m pip install -r extensions/story_bridge/requirements.txt
```

If your existing `.venv` was created by `uv` and has no `pip` module, use `uv pip install --python .venv/Scripts/python.exe -e .` for the root SDK install and then:

```powershell
uv pip install --python .venv/Scripts/python.exe -r extensions/story_bridge/requirements.txt
```

Start from the repository root with one process:

```powershell
.venv/Scripts/python.exe -m extensions.story_bridge
```

Open <http://127.0.0.1:8002>. The model is not downloaded or loaded merely by opening the page. Select **Tải mô hình** or press **Bắt đầu** to load the default model on demand. Voice choices appear after loading; leaving the voice selector on **Tự động** uses the model's default preset voice. The first load can take time and may download model weights according to the upstream SDK configuration.

The launcher defaults to `127.0.0.1:8002`. `STORY_BRIDGE_HOST`, `STORY_BRIDGE_PORT`, `STORY_BRIDGE_RUNTIME`, and `STORY_BRIDGE_MAX_PENDING` configure the host, port, runtime directory, and pending speech limit. Keep Uvicorn at **one worker**: the Gradio callback, model state, and generation queue are shared in this process. Do not launch the app through a multiworker Uvicorn command.

## HTTP API

| Route | Purpose |
| --- | --- |
| `GET /health` | Returns `{"model_loaded": false}` or `true`. |
| `GET /api/v1/voices` | Returns `{"voices": [{"id": "…", "name": "…"}]}`; the list can be empty before model load. |
| `POST /api/v1/model/load` | Explicitly loads the model; returns `{"model_loaded": true}` on success. |
| `POST /api/v1/speech` | Accepts JSON and returns WAV bytes (`Content-Type: audio/wav`). |

Speech JSON accepts `text` (required, nonblank, at most 20,000 characters), optional `voice_id`, `temperature` (default `0.8`, range `0.1`–`1.5`), `max_chars_per_chunk` (default `256`, range `128`–`512`), `batch_size` (default `16`, range `1`–`64`), and `use_batch` (default `true`). Unknown fields are rejected. Responses include `Cache-Control: no-store`, `Content-Disposition: inline`, `X-Audio-Id`, and `X-Audio-Expires-At` (UTC ISO time). Errors are JSON where available; a full speech queue returns `429`, and unavailable model or upstream integration returns `503`.

For a binary download with curl (Bash syntax):

```bash
curl --fail-with-body -X POST http://127.0.0.1:8002/api/v1/speech \
  -H 'Content-Type: application/json' \
  --data-binary '{"text":"Xin chào, đây là bài đọc thử."}' \
  --output speech.wav
```

The standalone browser client is an ES module. Call `openInNewTab` directly in a click handler so the blank tab is reserved before the request begins:

```html
<textarea id="story"></textarea>
<button id="read">Bắt đầu</button>
<script type="module">
  import { createSpeechClient } from 'http://127.0.0.1:8002/client/speech-client.js';
  const client = createSpeechClient({ baseUrl: 'http://127.0.0.1:8002' });
  document.querySelector('#read').addEventListener('click', () => {
    client.openInNewTab({ text: document.querySelector('#story').value })
      .catch((error) => alert(error.message));
  });
</script>
```

`synthesize(payload, {signal})` instead returns `{blob, audioId, expiresAt}` for applications with their own workflow. `openInNewTab(payload, {signal})` returns `{url, window, dispose, expiresAt}` plus the downloaded `blob` and `audioId`; `openBlobInNewTab(blob)` reopens an already downloaded result without another request. The client validates the WAV content type and reports readable HTTP errors. A blocked popup triggers no speech request. Blob URLs are scoped to the browser origin and page lifetime, are revoked when their audio tab closes or `dispose()` is called, and cannot be opened as durable links from another origin. The built-in page retains only its latest downloaded Blob for reopening; reloading the page loses that in-memory result.

## Calling from another local site

Set the exact browser origin before starting the bridge. For example, in PowerShell:

```powershell
$env:STORY_BRIDGE_CORS_ORIGINS = "http://localhost:5173,http://localhost:3000"
.venv/Scripts/python.exe -m extensions.story_bridge
```

The origin is scheme, host, and port; `localhost` and `127.0.0.1` are different origins. The API exposes the audio metadata headers to allowed browser origins. CORS is a browser restriction, **not access control**. The bridge has no authentication and is intended for a trusted local environment. Binding to a network interface exposes the API to clients that can reach that interface; add an authenticated gateway before any wider deployment.

Browser mixed-content and local-network rules also apply. Allowing an origin through CORS alone does not let a public HTTPS site call a private HTTP or localhost bridge. For a public application, serve an authorized, reachable HTTPS API or reverse proxy; ordinary same-network development origins can use the local bridge when the browser permits them.

## Audio lifetime and shared state

Server-owned output WAVs in `.runtime/generated/` and managed output copies in `.runtime/cache/` expire **300 seconds after generation completes**. The cleanup loop runs at most five seconds apart and startup also sweeps persisted metadata, so expired outputs from a previous process are removed after restart. New reads are denied after expiry, while a read already in progress may finish. Normal Gradio reference and upload cache under `.runtime/gradio/` is outside this generated-output TTL.

The server's expiry does not erase bytes already downloaded to a browser or a saved `speech.wav`. An open Blob audio tab can keep playing until it closes or its owner calls `dispose()`; the page cleans its Blob URLs on unload. Gradio's original UI and this API share one model and one generation queue. Model loading or changing the model in the original UI changes shared process state, and Gradio stop/cancel actions can affect shared work. Avoid simultaneous control from multiple tabs if a stable model choice matters.

When upgrading VieNeu-TTS or Gradio, recheck the story and load event bindings, their input/output component shapes, the Gradio client transport, and WAV output handling. Run the extension's Python contract tests and `node --test extensions/story_bridge/tests/speech-client.test.mjs` after upgrading; the bridge deliberately validates upstream bindings rather than silently assuming they stayed compatible.
