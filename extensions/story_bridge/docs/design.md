# Story Bridge design

Approved scope: an extension inside this fork, a redesigned story UI, and a JSON-to-audio FastAPI endpoint usable by any JavaScript project. The browser opens the returned binary audio in a new tab using a locally created Blob URL. Reuse the actual Gradio story-button callback; do not reproduce inference or edit upstream source.

## Boundaries

All shipped changes live in `extensions/story_bridge/`. The extension has its own launcher, dependencies, documentation, tests, and runtime directory. Upstream `apps/`, `src/`, root packaging, and Docker files remain untouched. Work in the current user-selected checkout (`nemhd/fork-1`), which is clean and is not main/master. The module directory is the isolation boundary the user requested.

The launcher imports `apps.gradio_main` and mounts its Blocks at `/_gradio`. It discovers `gen_event`, validates the expected story inputs and outputs, and invokes that event through Gradio's local HTTP API/client. Dynamic event names must be discovered rather than hardcoded. The model is shared with Gradio. All model load/synthesis event bindings use one concurrency group with limit 1, on one Uvicorn worker. No inference reimplementation or second model instance.

## Public contract

`POST /api/v1/speech` receives JSON `{text, voice_id?, temperature: 0.8, max_chars_per_chunk: 256, batch_size: 16, use_batch: true}`. Text must be nonblank and at most 20,000 characters; chunk size is 128..512, temperature 0.1..1.5, batch size 1..64. The response body is the WAV bytes with `Content-Type: audio/wav`, inline disposition, no-store, `X-Audio-Id`, and `X-Audio-Expires-At` (UTC ISO timestamp). CORS origins are configurable; expose these headers. Reject busy service with 429 and missing/broken upstream integration with 503 rather than returning error prose as audio. Provide health, voices, and explicit model-load endpoints. Speech can load the default model lazily using the existing Gradio load event; no model downloads occur merely on import.

The JavaScript client is framework-independent. `synthesize(payload)` returns `{blob, audioId, expiresAt}`; `openInNewTab(payload)` opens a blank tab synchronously before awaiting the network, then navigates it to a Blob URL. Failures close the blank tab. Closing the audio tab revokes its URL. Server expiry cannot revoke bytes a client has already received; document this limitation. No base64, S3, or persistent audio library.

## Temporary audio

Audio expires 300 seconds after generation completes, never 300 seconds after request submission. Capture the callback's first completed WAV result and register its actual modification time. Re-registering a file must not extend its lifetime. Track original WAVs and Gradio cache copies. Model/reference assets outside the extension-owned runtime directory must never be deleted. Persist lightweight lifecycle metadata so restart cleanup works. A cleanup loop runs at most 5 seconds apart, and startup performs cleanup. Deny new reads after expiry. In-flight file responses may finish; delete their files after their read lease closes. Account for Windows open-file deletion failures and retry on later sweeps.

Gradio file routes must obey the same expiry for managed audio and receive no-store headers. Register/cache-discover outputs even if the HTTP caller disconnects. Keep the normal Gradio result structure for the original UI.

## UI and delivery

Provide a responsive Vietnamese page served at `/`: text editor, voice selection, advanced generation controls, load/readiness state, status feedback, and an action opening the native audio tab. Native HTML/CSS/ES modules keep the client reusable without a build tool. Include a Windows-friendly launch command, dependency instructions, CORS examples, and curl/browser examples. Do not imply audio generation has been validated on a real model unless it has.

## Verification

Test real temporary files with a controllable clock (boundary expiry, restart, aliases, leases, external-file protection), real Gradio event integration with a lightweight waveform-producing callback in tests, API binary response and CORS, invalid input/errors, and the browser client's popup ordering and URL cleanup. Run a focused upstream baseline. Test the actual upstream callback using a small test engine if downloading weights is unnecessary; distinguish this from a real-model smoke test.
