from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path


@dataclass(frozen=True)
class Settings:
    host: str = "127.0.0.1"
    port: int = 8002
    runtime_dir: Path = field(default_factory=lambda: Path(__file__).resolve().parent / ".runtime")
    cors_origins: list[str] = field(default_factory=lambda: ["http://localhost:5173", "http://localhost:3000"])
    max_pending_speech: int = 4
    upstream_url: str = "http://127.0.0.1:8002/_gradio"

    @classmethod
    def from_env(cls) -> "Settings":
        origins = os.getenv("STORY_BRIDGE_CORS_ORIGINS", "http://localhost:5173,http://localhost:3000")
        host = os.getenv("STORY_BRIDGE_HOST", "127.0.0.1")
        port = int(os.getenv("STORY_BRIDGE_PORT", "8002"))
        client_host = "127.0.0.1" if host in ("0.0.0.0", "::", "[::]") else host
        return cls(
            host=host,
            port=port,
            runtime_dir=Path(os.getenv("STORY_BRIDGE_RUNTIME", str(Path(__file__).parent / ".runtime"))),
            cors_origins=[item.strip() for item in origins.split(",") if item.strip()],
            max_pending_speech=int(os.getenv("STORY_BRIDGE_MAX_PENDING", "4")),
            upstream_url=f"http://{client_host}:{port}/_gradio",
        )
