"""Windows-friendly single-worker launcher for the Story Bridge."""

from __future__ import annotations

import os
import tempfile

from .config import Settings


def create_runtime(settings: Settings):
    settings.runtime_dir.mkdir(parents=True, exist_ok=True)
    generated = settings.runtime_dir / "generated"
    generated.mkdir(parents=True, exist_ok=True)
    cache = settings.runtime_dir / "cache"
    cache.mkdir(parents=True, exist_ok=True)
    # Upstream uses tempfile.NamedTemporaryFile for WAV output. Set both knobs
    # before importing it so generated files are owned by AudioStore.
    os.environ.setdefault("TMPDIR", str(generated))
    os.environ.setdefault("TEMP", str(generated))
    os.environ.setdefault("TMP", str(generated))
    os.environ["GRADIO_TEMP_DIR"] = str(cache)
    tempfile.tempdir = str(generated)


def build_app(settings: Settings | None = None):
    settings = settings or Settings.from_env()
    create_runtime(settings)
    from apps import gradio_main as upstream

    from .api import create_app
    from .gradio_bridge import GradioBridge
    from .temp_audio import AudioStore

    store = AudioStore(settings.runtime_dir)
    bridge = GradioBridge(upstream, store, settings.upstream_url)
    bridge.install()
    return create_app(settings, bridge, store, mount_gradio=True)


def main() -> None:
    import uvicorn

    settings = Settings.from_env()
    app = build_app(settings)
    uvicorn.run(app, host=settings.host, port=settings.port, workers=1)
