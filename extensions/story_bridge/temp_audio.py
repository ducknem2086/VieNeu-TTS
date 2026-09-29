"""Persistent ownership and expiry tracking for generated WAV files."""

from __future__ import annotations

import hashlib
import sqlite3
import stat
import threading
import time
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Callable


@dataclass(frozen=True)
class AudioAsset:
    id: str
    path: Path
    created_at: float
    expires_at: float


class AudioExpired(Exception):
    """A managed audio file can no longer be read."""


class AudioStore:
    def __init__(
        self, root: Path, ttl_seconds: float = 300, clock: Callable[[], float] = time.time
    ):
        if ttl_seconds <= 0:
            raise ValueError("ttl_seconds must be positive")
        self.root = Path(root).resolve()
        self.root.mkdir(parents=True, exist_ok=True)
        self.generated_dir = self.root / "generated"
        self.cache_dir = self.root / "cache"
        self.generated_dir.mkdir(exist_ok=True)
        self.cache_dir.mkdir(exist_ok=True)
        self._clock = clock
        self._ttl = ttl_seconds
        self._lock = threading.RLock()
        self._leases: dict[str, int] = {}
        self._db = sqlite3.connect(self.root / "audio.sqlite3", check_same_thread=False)
        with self._db:
            self._db.execute(
                "CREATE TABLE IF NOT EXISTS assets ("
                "id TEXT PRIMARY KEY, path TEXT UNIQUE NOT NULL, name TEXT NOT NULL, "
                "digest TEXT NOT NULL, size INTEGER NOT NULL, "
                "created_at REAL NOT NULL, expires_at REAL NOT NULL)"
            )
            self._db.execute(
                "CREATE TABLE IF NOT EXISTS aliases ("
                "path TEXT PRIMARY KEY, asset_id TEXT NOT NULL REFERENCES assets(id))"
            )
        self.sweep()

    @staticmethod
    def _fingerprint(path: Path) -> tuple[int, str]:
        digest = hashlib.sha256()
        size = 0
        with path.open("rb") as audio:
            for block in iter(lambda: audio.read(1024 * 1024), b""):
                size += len(block)
                digest.update(block)
        return size, digest.hexdigest()

    @staticmethod
    def _asset(row: tuple) -> AudioAsset:
        return AudioAsset(row[0], Path(row[1]), row[5], row[6])

    @staticmethod
    def _gone(path: Path) -> bool:
        try:
            path.lstat()
        except FileNotFoundError:
            return True
        except OSError:
            return False
        return False

    def _safe_file(self, path: Path, base: Path) -> Path | None:
        path = Path(path).absolute()
        try:
            relative = path.relative_to(base)
            if ".." in relative.parts or not relative.parts:
                return None
            current = base
            for part in relative.parts:
                current = current / part
                if current.is_symlink():
                    return None
            resolved = path.resolve(strict=True)
            resolved.relative_to(base)
            if not stat.S_ISREG(resolved.stat().st_mode):
                return None
            return resolved
        except (OSError, ValueError):
            return None

    def register(self, path: Path, created_at: float | None = None) -> AudioAsset:
        with self._lock:
            source = self._safe_file(path, self.generated_dir)
            if source is None or source.suffix.lower() != ".wav":
                raise ValueError("expected a real generated WAV inside generated_dir")
            row = self._db.execute(
                "SELECT * FROM assets WHERE path = ?", (str(source),)
            ).fetchone()
            if row is not None:
                return self._asset(row)
            completed_at = source.stat().st_mtime if created_at is None else float(created_at)
            size, digest = self._fingerprint(source)
            asset = AudioAsset(uuid.uuid4().hex, source, completed_at, completed_at + self._ttl)
            with self._db:
                self._db.execute(
                    "INSERT INTO assets VALUES (?, ?, ?, ?, ?, ?, ?)",
                    (asset.id, str(source), source.name, digest, size, completed_at, asset.expires_at),
                )
            return asset

    def _match_cache(self, path: Path) -> tuple | None:
        try:
            size, digest = self._fingerprint(path)
        except OSError:
            return None
        rows = self._db.execute(
            "SELECT * FROM assets WHERE name = ? AND size = ? AND digest = ?",
            (path.name, size, digest),
        ).fetchall()
        if len(rows) != 1:
            return None
        row = rows[0]
        with self._db:
            self._db.execute(
                "INSERT OR REPLACE INTO aliases (path, asset_id) VALUES (?, ?)",
                (str(path), row[0]),
            )
        return row

    def find(self, path: Path) -> AudioAsset | None:
        with self._lock:
            source = self._safe_file(path, self.generated_dir)
            if source is not None:
                row = self._db.execute(
                    "SELECT * FROM assets WHERE path = ?", (str(source),)
                ).fetchone()
                return self._asset(row) if row else None
            alias = self._safe_file(path, self.cache_dir)
            if alias is None:
                return None
            row = self._match_cache(alias)
            return self._asset(row) if row else None

    def link_cache_copies(self, asset: AudioAsset) -> None:
        with self._lock:
            row = self._db.execute(
                "SELECT * FROM assets WHERE id = ? AND path = ?", (asset.id, str(asset.path))
            ).fetchone()
            if row is None:
                raise ValueError("unknown audio asset")
            for candidate in self.cache_dir.rglob(row[2]):
                alias = self._safe_file(candidate, self.cache_dir)
                if alias is not None:
                    self._match_cache(alias)

    def acquire(self, path: Path) -> AudioAsset:
        with self._lock:
            asset = self.find(path)
            if asset is None:
                raise FileNotFoundError(path)
            if self._clock() >= asset.expires_at:
                raise AudioExpired(str(path))
            self._leases[asset.id] = self._leases.get(asset.id, 0) + 1
            return asset

    def release(self, asset_id: str) -> None:
        with self._lock:
            count = self._leases.get(asset_id, 0)
            if count <= 1:
                self._leases.pop(asset_id, None)
            else:
                self._leases[asset_id] = count - 1

    def sweep(self) -> int:
        removed = 0
        with self._lock:
            rows = self._db.execute("SELECT * FROM assets").fetchall()
            for row in rows:
                self.link_cache_copies(self._asset(row))
            for row in rows:
                if self._clock() < row[6] or self._leases.get(row[0], 0):
                    continue
                aliases = self._db.execute(
                    "SELECT path FROM aliases WHERE asset_id = ?", (row[0],)
                ).fetchall()
                for (name,) in aliases:
                    alias = self._safe_file(Path(name), self.cache_dir)
                    if alias is not None:
                        try:
                            if self._fingerprint(alias) != (row[4], row[3]):
                                continue
                            alias.unlink()
                            removed += 1
                        except OSError:
                            pass
                source = self._safe_file(Path(row[1]), self.generated_dir)
                if source is not None:
                    try:
                        if self._fingerprint(source) != (row[4], row[3]):
                            continue
                        source.unlink()
                        removed += 1
                    except OSError:
                        pass
                if self._gone(Path(row[1])) and all(
                    self._gone(Path(name)) for (name,) in aliases
                ):
                    with self._db:
                        self._db.execute("DELETE FROM aliases WHERE asset_id = ?", (row[0],))
                        self._db.execute("DELETE FROM assets WHERE id = ?", (row[0],))
        return removed

    def close(self) -> None:
        with self._lock:
            self._db.close()
