import os
from pathlib import Path

import pytest

from extensions.story_bridge.temp_audio import AudioExpired, AudioStore


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
