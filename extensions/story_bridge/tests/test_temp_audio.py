import os
import sqlite3
import wave
from pathlib import Path

import pytest

from extensions.story_bridge.temp_audio import AudioExpired, AudioStore


@pytest.mark.parametrize("restart_at", [1299, 1300])
def test_restart_recovers_unregistered_completed_wav_with_original_deadline(tmp_path, restart_at):
    root = tmp_path / "owned"
    store = AudioStore(root, clock=lambda: 1000)
    source = store.generated_dir / "orphan.wav"
    with wave.open(str(source), "wb") as wav:
        wav.setnchannels(1)
        wav.setsampwidth(2)
        wav.setframerate(8000)
        wav.writeframes(b"\0\0" * 80)
    os.utime(source, (1000, 1000))
    alias = store.cache_dir / source.name
    alias.write_bytes(source.read_bytes())
    unrelated = store.cache_dir / "different" / source.name
    unrelated.parent.mkdir()
    unrelated.write_bytes(b"different bytes")
    outside = tmp_path / "outside.wav"
    outside.write_bytes(source.read_bytes())
    store.close()  # Simulate completion followed by a crash before registration.

    now = [restart_at]
    restarted = AudioStore(root, clock=lambda: now[0])
    if restart_at == 1299:
        asset = restarted.find(source)
        assert asset is not None
        assert (asset.created_at, asset.expires_at) == (1000, 1300)
        assert restarted.acquire(alias) == asset
        now[0] = 1300
        assert restarted.sweep() == 0  # Recovered aliases obey read leases too.
        restarted.release(asset.id)
        assert restarted.sweep() == 2
    assert not source.exists()
    assert not alias.exists()
    assert outside.exists()
    assert unrelated.read_bytes() == b"different bytes"
    restarted.close()


def test_restart_does_not_claim_replaced_registered_bytes_or_incomplete_wavs(tmp_path):
    root = tmp_path / "owned"
    store = AudioStore(root, clock=lambda: 1000)
    source = store.generated_dir / "replaced.wav"
    source.write_bytes(b"original registered bytes")
    asset = store.register(source, created_at=1000)
    store.close()
    with wave.open(str(source), "wb") as wav:
        wav.setnchannels(1)
        wav.setsampwidth(2)
        wav.setframerate(8000)
        wav.writeframes(b"\0\0" * 80)
    replacement = source.read_bytes()
    partial = source.with_name("partial.wav")
    partial.write_bytes(replacement[:-2])
    os.utime(source, (700, 700))
    os.utime(partial, (700, 700))

    restarted = AudioStore(root, clock=lambda: 1300)
    assert source.read_bytes() == replacement
    assert restarted.find(source) == asset
    assert partial.exists()
    assert restarted.find(partial) is None
    restarted.close()


def test_expiry_boundary_denies_new_reads_and_removes_generated_wav(tmp_path):
    now = [1000.0]
    store = AudioStore(tmp_path / "owned", clock=lambda: now[0])
    source = store.generated_dir / "speech.wav"
    source.write_bytes(b"wave-test")
    asset = store.register(source, created_at=1000)

    assert asset.created_at == 1000
    assert asset.expires_at == 1300
    now[0] = 1299
    assert store.acquire(source).id == asset.id
    store.release(asset.id)
    now[0] = 1300
    with pytest.raises(AudioExpired):
        store.acquire(source)
    assert store.sweep() == 1
    assert not source.exists()
    store.close()


def test_registration_uses_file_mtime_and_does_not_refresh_deadline(tmp_path):
    store = AudioStore(tmp_path / "owned", clock=lambda: 1000.0)
    source = store.generated_dir / "speech.wav"
    source.write_bytes(b"wave-test")
    os.utime(source, (700, 700))
    original = store.register(source)
    again = store.register(source, created_at=950)

    assert original.created_at == 700
    assert original.expires_at == 1000
    assert again == original
    with pytest.raises(AudioExpired):
        store.acquire(source)
    store.close()


def test_restart_recovers_and_removes_expired_generated_file(tmp_path):
    now = [1000.0]
    root = tmp_path / "owned"
    store = AudioStore(root, clock=lambda: now[0])
    source = store.generated_dir / "speech.wav"
    source.write_bytes(b"wave-test")
    store.register(source, created_at=1000)
    store.close()

    now[0] = 1301
    restarted = AudioStore(root, clock=lambda: now[0])
    assert not source.exists()
    restarted.close()


def test_restart_preserves_unexpired_source_and_discovers_late_alias(tmp_path):
    now = [1000.0]
    root = tmp_path / "owned"
    store = AudioStore(root, clock=lambda: now[0])
    source = store.generated_dir / "speech.wav"
    source.write_bytes(b"wave-test")
    asset = store.register(source, created_at=1000)
    store.close()

    alias = root / "cache" / "gradio" / "speech.wav"
    alias.parent.mkdir()
    alias.write_bytes(b"wave-test")
    now[0] = 1299
    restarted = AudioStore(root, clock=lambda: now[0])
    assert source.exists()
    assert restarted.find(alias) == asset
    now[0] = 1300
    assert restarted.sweep() == 2
    restarted.close()


def test_cache_alias_has_original_deadline_and_is_removed_with_source(tmp_path):
    now = [1000.0]
    store = AudioStore(tmp_path / "owned", clock=lambda: now[0])
    source = store.generated_dir / "speech.wav"
    source.write_bytes(b"wave-test")
    asset = store.register(source, created_at=1000)
    alias = store.cache_dir / "gradio" / "speech.wav"
    alias.parent.mkdir()
    alias.write_bytes(b"wave-test")

    store.link_cache_copies(asset)
    assert store.find(alias) == asset
    now[0] = 1300
    with pytest.raises(AudioExpired):
        store.acquire(alias)
    assert store.sweep() == 2
    assert not source.exists()
    assert not alias.exists()
    store.close()


def test_sweep_discovers_late_cache_copy_but_preserves_unrelated_files(tmp_path):
    now = [1000.0]
    store = AudioStore(tmp_path / "owned", clock=lambda: now[0])
    source = store.generated_dir / "speech.wav"
    source.write_bytes(b"wave-test")
    store.register(source, created_at=1000)
    unrelated = store.cache_dir / "other.wav"
    unrelated.write_bytes(b"other")
    same_name_different_bytes = store.cache_dir / "other" / "speech.wav"
    same_name_different_bytes.parent.mkdir()
    same_name_different_bytes.write_bytes(b"different")
    alias = store.cache_dir / "late" / "speech.wav"
    alias.parent.mkdir()
    alias.write_bytes(b"wave-test")

    now[0] = 1301
    assert store.sweep() == 2
    assert not source.exists()
    assert not alias.exists()
    assert unrelated.read_bytes() == b"other"
    assert same_name_different_bytes.read_bytes() == b"different"
    store.close()


def test_active_lease_pins_source_and_cache_alias_until_release(tmp_path):
    now = [1000.0]
    store = AudioStore(tmp_path / "owned", clock=lambda: now[0])
    source = store.generated_dir / "speech.wav"
    source.write_bytes(b"wave-test")
    asset = store.register(source, created_at=1000)
    alias = store.cache_dir / "speech.wav"
    alias.write_bytes(b"wave-test")
    assert store.acquire(alias) == asset

    now[0] = 1300
    assert store.sweep() == 0
    assert source.exists() and alias.exists()
    store.release(asset.id)
    assert store.sweep() == 2
    store.close()


def test_outside_paths_are_not_registered_or_deleted(tmp_path):
    store = AudioStore(tmp_path / "owned", clock=lambda: 1300.0)
    outside = tmp_path / "external.wav"
    outside.write_bytes(b"external")
    with pytest.raises(ValueError):
        store.register(outside, created_at=1000)
    assert store.find(outside) is None
    assert store.sweep() == 0
    assert outside.read_bytes() == b"external"
    store.close()


def test_symlink_to_external_file_is_not_registered_or_deleted(tmp_path):
    store = AudioStore(tmp_path / "owned", clock=lambda: 1300.0)
    outside = tmp_path / "external.wav"
    outside.write_bytes(b"external")

    source_link = store.generated_dir / "linked.wav"
    try:
        source_link.symlink_to(outside)
    except (OSError, NotImplementedError):
        store.close()
        pytest.skip("symlinks are unavailable")
    with pytest.raises(ValueError):
        store.register(source_link, created_at=1000)
    assert store.find(source_link) is None
    assert store.sweep() == 0
    assert outside.read_bytes() == b"external"
    store.close()

    restarted = AudioStore(tmp_path / "owned", clock=lambda: 1600.0)
    assert restarted.find(source_link) is None
    assert outside.read_bytes() == b"external"
    restarted.close()


def test_failed_delete_remains_retryable(tmp_path, monkeypatch):
    now = [1000.0]
    store = AudioStore(tmp_path / "owned", clock=lambda: now[0])
    source = store.generated_dir / "speech.wav"
    source.write_bytes(b"wave-test")
    store.register(source, created_at=1000)
    now[0] = 1300

    real_unlink = Path.unlink
    attempts = [0]

    def locked_once(path, *args, **kwargs):
        if path == source and attempts[0] == 0:
            attempts[0] += 1
            raise PermissionError("file in use")
        return real_unlink(path, *args, **kwargs)

    monkeypatch.setattr(Path, "unlink", locked_once)
    assert store.sweep() == 0
    assert source.exists()
    assert store.sweep() == 1
    assert not source.exists()
    store.close()


def test_failed_verification_read_remains_retryable(tmp_path, monkeypatch):
    now = [1000.0]
    store = AudioStore(tmp_path / "owned", clock=lambda: now[0])
    source = store.generated_dir / "speech.wav"
    source.write_bytes(b"wave-test")
    store.register(source, created_at=1000)
    now[0] = 1300

    real_open = Path.open
    attempts = [0]

    def locked_once(path, *args, **kwargs):
        if path == source and attempts[0] == 0:
            attempts[0] += 1
            raise PermissionError("file in use")
        return real_open(path, *args, **kwargs)

    monkeypatch.setattr(Path, "open", locked_once)
    assert store.sweep() == 0
    assert source.exists()
    assert store.sweep() == 1
    assert not source.exists()
    store.close()


def test_path_can_be_registered_again_after_expired_file_is_swept(tmp_path):
    now = [1000.0]
    store = AudioStore(tmp_path / "owned", clock=lambda: now[0])
    source = store.generated_dir / "speech.wav"
    source.write_bytes(b"first")
    first = store.register(source, created_at=1000)

    now[0] = 1300
    assert store.sweep() == 1
    source.write_bytes(b"second")
    second = store.register(source, created_at=1300)

    assert second.id != first.id
    assert second.expires_at == 1600
    assert store.acquire(source) == second
    store.release(second.id)
    store.close()


def test_sweep_retires_registry_rows_after_files_are_gone(tmp_path):
    now = [1000.0]
    root = tmp_path / "owned"
    store = AudioStore(root, clock=lambda: now[0])
    for index in range(5):
        source = store.generated_dir / f"speech-{index}.wav"
        source.write_bytes(f"wave-{index}".encode())
        store.register(source, created_at=now[0])
        alias = store.cache_dir / source.name
        alias.write_bytes(source.read_bytes())
        now[0] += 300
        assert store.sweep() == 2

    with sqlite3.connect(root / "audio.sqlite3") as db:
        assert db.execute("SELECT count(*) FROM assets").fetchone()[0] == 0
        assert db.execute("SELECT count(*) FROM aliases").fetchone()[0] == 0
    store.close()


def test_registry_retains_locked_alias_until_retry_succeeds(tmp_path, monkeypatch):
    now = [1000.0]
    root = tmp_path / "owned"
    store = AudioStore(root, clock=lambda: now[0])
    source = store.generated_dir / "speech.wav"
    source.write_bytes(b"wave-test")
    store.register(source, created_at=1000)
    alias = store.cache_dir / "speech.wav"
    alias.write_bytes(b"wave-test")
    now[0] = 1300

    real_unlink = Path.unlink
    attempts = [0]

    def locked_once(path, *args, **kwargs):
        if path == alias and attempts[0] == 0:
            attempts[0] += 1
            raise PermissionError("file in use")
        return real_unlink(path, *args, **kwargs)

    monkeypatch.setattr(Path, "unlink", locked_once)
    assert store.sweep() == 1
    assert alias.exists()
    with sqlite3.connect(root / "audio.sqlite3") as db:
        assert db.execute("SELECT count(*) FROM assets").fetchone()[0] == 1
    assert store.sweep() == 1
    with sqlite3.connect(root / "audio.sqlite3") as db:
        assert db.execute("SELECT count(*) FROM assets").fetchone()[0] == 0
    store.close()
