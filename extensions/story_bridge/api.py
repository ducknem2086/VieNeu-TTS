"""FastAPI surface and managed Gradio-file access guard."""

from __future__ import annotations

import asyncio
import math
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from urllib.parse import unquote

from fastapi import FastAPI
from fastapi.exceptions import RequestValidationError
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, ConfigDict, Field, field_validator

from .gradio_bridge import BridgeError, SpeechRequest
from .temp_audio import AudioExpired, AudioStore


class SpeechPayload(BaseModel):
    model_config = ConfigDict(extra="forbid")

    text: str
    voice_id: str | None = None
    temperature: float = 0.8
    max_chars_per_chunk: int = Field(256, ge=128, le=512)
    batch_size: int = Field(16, ge=1, le=64)
    use_batch: bool = True

    @field_validator("text")
    @classmethod
    def valid_text(cls, value: str) -> str:
        if not value.strip() or len(value) > 20_000:
            raise ValueError("text must be nonblank and at most 20000 characters")
        return value

    @field_validator("temperature")
    @classmethod
    def valid_temperature(cls, value: float) -> float:
        if not math.isfinite(value) or not 0.1 <= value <= 1.5:
            raise ValueError("temperature must be finite and between 0.1 and 1.5")
        return value


def _setting(settings: Any, name: str, default: Any) -> Any:
    if isinstance(settings, dict):
        return settings.get(name, default)
    return getattr(settings, name, default)


def _iso(seconds: float) -> str:
    return datetime.fromtimestamp(seconds, timezone.utc).isoformat().replace("+00:00", "Z")


class ManagedFileGuard:
    """ASGI middleware that leases managed Gradio files for the full response."""

    def __init__(self, app, store: AudioStore):
        self.app = app
        self.store = store

    @staticmethod
    def _candidate(scope) -> Path | None:
        path = scope.get("path", "")
        for marker in ("/gradio_api/file=", "/gradio_api/file/", "/file="):
            if marker in path:
                return Path(unquote(path.split(marker, 1)[1]))
        return None

    def _owned_namespace(self, candidate: Path) -> bool:
        candidate = candidate.absolute()
        for base in (self.store.generated_dir, self.store.cache_dir):
            try:
                candidate.relative_to(base)
                return True
            except ValueError:
                continue
        return False

    async def __call__(self, scope, receive, send):
        candidate = self._candidate(scope)
        if candidate is None:
            await self.app(scope, receive, send)
            return
        asset = self.store.find(candidate)
        if asset is None:
            if self._owned_namespace(candidate):
                await JSONResponse({"detail": "managed audio is unavailable"}, status_code=404,
                                   headers={"Cache-Control": "no-store"})(scope, receive, send)
                return
            await self.app(scope, receive, send)
            return
        try:
            leased = self.store.acquire(candidate)
        except AudioExpired:
            await JSONResponse({"detail": "managed audio has expired"}, status_code=410,
                               headers={"Cache-Control": "no-store"})(scope, receive, send)
            return
        released = False

        async def guarded_send(message):
            nonlocal released
            if message.get("type") == "http.response.start":
                headers = list(message.get("headers", []))
                headers.append((b"cache-control", b"no-store"))
                message["headers"] = headers
            await send(message)
            if message.get("type") == "http.response.body" and not message.get("more_body", False) and not released:
                released = True
                self.store.release(leased.id)

        try:
            await self.app(scope, receive, guarded_send)
        finally:
            if not released:
                self.store.release(leased.id)


class LeasedFileResponse(FileResponse):
    def __init__(self, *args, store: AudioStore, asset_id: str, **kwargs):
        super().__init__(*args, **kwargs)
        self._store = store
        self._asset_id = asset_id

    async def __call__(self, scope, receive, send):
        try:
            await super().__call__(scope, receive, send)
        finally:
            self._store.release(self._asset_id)


def create_app(settings: Any, bridge, store: AudioStore, *, mount_gradio: bool = True) -> FastAPI:
    @asynccontextmanager
    async def lifespan(_app):
        store.sweep()

        async def sweep_loop():
            while True:
                await asyncio.sleep(5)
                await asyncio.to_thread(store.sweep)

        sweep_task = asyncio.create_task(sweep_loop())
        try:
            yield
        finally:
            sweep_task.cancel()
            await asyncio.gather(sweep_task, return_exceptions=True)

    app = FastAPI(title="VieNeu Story Bridge", lifespan=lifespan)
    extension_dir = Path(__file__).resolve().parent
    ui_dir = Path(_setting(settings, "ui_dir", extension_dir / "ui"))
    client_dir = Path(_setting(settings, "client_dir", extension_dir / "client"))
    origins = list(_setting(settings, "cors_origins", ["http://localhost:5173", "http://localhost:3000"]))
    app.add_middleware(
        CORSMiddleware,
        allow_origins=origins,
        allow_credentials=False,
        allow_methods=["GET", "POST", "OPTIONS"],
        allow_headers=["*"],
        expose_headers=["X-Audio-Id", "X-Audio-Expires-At", "Content-Disposition"],
    )
    app.add_middleware(ManagedFileGuard, store=store)

    @app.exception_handler(RequestValidationError)
    async def validation_error(_request, exc: RequestValidationError):
        # Pydantic includes the original input in error details. JSON exponent
        # overflow becomes infinity, which Starlette refuses to serialize.
        return JSONResponse(
            {"detail": [
                {"loc": error["loc"], "msg": error["msg"], "type": error["type"]}
                for error in exc.errors()
            ]},
            status_code=422,
        )
    pending = int(_setting(settings, "max_pending_speech", 4))
    semaphore = asyncio.Semaphore(max(1, pending))

    @app.get("/health")
    async def health():
        return {"model_loaded": bool(getattr(bridge, "upstream", None) and getattr(bridge.upstream, "model_loaded", False))}

    @app.get("/", include_in_schema=False)
    async def index():
        page = ui_dir / "index.html"
        if not page.is_file():
            return JSONResponse({"detail": "UI is not installed"}, status_code=404)
        return FileResponse(page, media_type="text/html")

    @app.get("/api/v1/voices")
    async def voices():
        try:
            return {"voices": await asyncio.to_thread(bridge.voices)}
        except BridgeError as exc:
            return JSONResponse({"detail": str(exc)}, status_code=503)
        except Exception:
            return JSONResponse({"detail": "voice service unavailable"}, status_code=503)

    @app.post("/api/v1/model/load")
    async def load_model():
        try:
            return await asyncio.to_thread(bridge.load_model)
        except BridgeError as exc:
            return JSONResponse({"detail": str(exc)}, status_code=503)
        except Exception:
            return JSONResponse({"detail": "model service unavailable"}, status_code=503)

    @app.post("/api/v1/speech")
    async def speech(payload: SpeechPayload):
        if semaphore.locked() and semaphore._value <= 0:
            return JSONResponse({"detail": "speech queue is busy"}, status_code=429)
        await semaphore.acquire()
        try:
            request = SpeechRequest(
                text=payload.text,
                voice_id=payload.voice_id,
                temperature=payload.temperature,
                max_chars_per_chunk=payload.max_chars_per_chunk,
                batch_size=payload.batch_size,
                use_batch=payload.use_batch,
            )
            try:
                asset = await asyncio.to_thread(bridge.synthesize, request)
                leased = await asyncio.to_thread(store.acquire, asset.path)
            except AudioExpired:
                return JSONResponse({"detail": "generated audio expired"}, status_code=503)
            except (BridgeError, FileNotFoundError, ValueError) as exc:
                return JSONResponse({"detail": str(exc)}, status_code=503)
            except Exception:
                return JSONResponse({"detail": "speech service unavailable"}, status_code=503)
            headers = {
                "Cache-Control": "no-store",
                "Content-Disposition": 'inline; filename="speech.wav"',
                "X-Audio-Id": leased.id,
                "X-Audio-Expires-At": _iso(leased.expires_at),
            }
            return LeasedFileResponse(
                leased.path,
                media_type="audio/wav",
                headers=headers,
                store=store,
                asset_id=leased.id,
            )
        finally:
            semaphore.release()

    if mount_gradio:
        import gradio as gr

        bridge.install()
        bridge.upstream.demo.queue(api_open=False, default_concurrency_limit=1)
        gr.mount_gradio_app(
            app, bridge.upstream.demo, path="/_gradio", allowed_paths=[str(store.cache_dir)]
        )
    app.mount("/client", StaticFiles(directory=str(client_dir), check_dir=False), name="client")
    app.mount("/ui", StaticFiles(directory=str(ui_dir), check_dir=False), name="ui")
    return app
