"""Tests for the persistent registry."""

from __future__ import annotations

import os
import socket
import stat
import threading
from pathlib import Path

import pytest

from ephemdir import _registry as registry_module
from ephemdir._registry import Registry


def test_load_missing_returns_empty(tmp_path):
    reg = Registry(path=tmp_path / "absent.json")
    assert reg.load() == {}


def test_load_corrupt_returns_empty(tmp_path):
    path = tmp_path / "registry.json"
    path.write_text("{ this is not valid json")
    path.chmod(0o600)
    reg = Registry(path=path)
    assert reg.load() == {}


def _entry(**overrides):
    entry = {"created_at": 1.0, "expires_at": None, "remove_on_restart": True}
    entry.update(overrides)
    return entry


def test_save_and_load_roundtrip(registry):
    payload = {"/tmp/brave-otter": _entry()}
    registry.save(payload)
    assert registry.load() == payload


def test_transaction_persists_changes(registry):
    with registry.transaction() as state:
        state["/tmp/x"] = _entry(created_at=2.0)
    assert registry.load() == {"/tmp/x": _entry(created_at=2.0)}


def test_transaction_releases_lock(registry):
    # Two sequential transactions must both succeed (lock is released).
    with registry.transaction() as state:
        state["/tmp/a"] = _entry(created_at=1.0)
    with registry.transaction() as state:
        state["/tmp/b"] = _entry(created_at=2.0)
    assert set(registry.load()) == {"/tmp/a", "/tmp/b"}


@pytest.mark.skipif(not hasattr(os, "mkfifo"), reason="FIFO support required")
def test_load_fifo_is_non_blocking(tmp_path):
    path = tmp_path / "registry.json"
    os.mkfifo(path, 0o600)
    result: list[dict[str, object]] = []

    thread = threading.Thread(
        target=lambda: result.append(Registry(path=path).load()),
        daemon=True,
    )
    thread.start()
    thread.join(timeout=1.0)

    assert not thread.is_alive(), "registry load blocked while opening a FIFO"
    assert result == [{}]


@pytest.mark.skipif(not hasattr(socket, "AF_UNIX"), reason="Unix sockets required")
def test_load_rejects_unix_socket(tmp_path):
    # AF_UNIX limits sun_path to ~104 bytes on macOS; pytest's nested tmp_path
    # regularly exceeds that, so fall back to a private short-path directory.
    import shutil
    import tempfile

    path = tmp_path / "registry.json"
    server = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    fallback_dir: str | None = None
    try:
        try:
            server.bind(str(path))
        except OSError:
            fallback_dir = tempfile.mkdtemp(prefix="ephsock-")
            path = Path(fallback_dir) / "registry.json"
            try:
                server.bind(str(path))
            except OSError as exc:
                pytest.skip(f"Unix socket bind unavailable: {exc}")
        assert Registry(path=path).load() == {}
    finally:
        server.close()
        if fallback_dir is not None:
            shutil.rmtree(fallback_dir, ignore_errors=True)


@pytest.mark.skipif(os.name != "posix", reason="POSIX device files required")
def test_load_rejects_device_file():
    assert Registry(path=Path(os.devnull)).load() == {}


def test_load_rejects_oversized_registry(tmp_path):
    path = tmp_path / "registry.json"
    path.write_bytes(b"{}" + b" " * registry_module._MAX_REGISTRY_BYTES)
    path.chmod(0o600)

    reg = Registry(path=path)
    assert reg.load() == {}
    with reg.transaction():
        pass

    assert reg.load() == {}
    assert list(tmp_path.glob("registry.json.corrupt-*"))


@pytest.mark.skipif(os.name != "posix", reason="POSIX permission bits required")
def test_load_migrates_world_readable_registry_in_place(tmp_path):
    # A registry written by ephemdir <= 0.3 is owner-owned but world/group
    # readable (default umask). It is valid data, so loading must tighten the
    # mode in place and preserve the entries -- never quarantine them and
    # orphan every tracked directory.
    path = tmp_path / "registry.json"
    payload = {"/tmp/brave-otter": _entry()}
    Registry(path=path).save(payload)
    path.chmod(0o644)

    assert Registry(path=path).load() == payload
    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    assert not list(tmp_path.glob("registry.json.corrupt-*"))


@pytest.mark.skipif(os.name != "posix", reason="POSIX permission bits required")
@pytest.mark.parametrize("mode", [0o664, 0o666, 0o660, 0o622])
def test_load_refuses_group_or_world_writable_registry(tmp_path, mode):
    # A registry another local user could have *written* to (any group/world
    # write bit) may carry a tampered policy -- e.g. a forced expiry that would
    # make a sweep delete a directory the owner really created. It must be
    # refused outright: not parsed, not swept, not tightened, not quarantined,
    # and above all not overwritten with an empty state. The file is left
    # exactly as found for the user to inspect.
    from ephemdir._registry import UnsafeRegistryError

    path = tmp_path / "registry.json"
    payload = {"/tmp/x": _entry(created_at=2.0)}
    Registry(path=path).save(payload)
    path.chmod(mode)

    with pytest.raises(UnsafeRegistryError):
        Registry(path=path).load()

    # The file is untouched: same content, same mode, no quarantine sibling.
    assert path.exists()
    assert stat.S_IMODE(path.stat().st_mode) == mode
    assert not list(tmp_path.glob("registry.json.corrupt-*"))


@pytest.mark.skipif(os.name != "posix", reason="POSIX permission bits required")
def test_transaction_does_not_empty_a_writable_registry(tmp_path):
    # A failed (untrusted) load inside a transaction must abort before the
    # save step, so the registry is never replaced with an empty file.
    from ephemdir._registry import UnsafeRegistryError

    path = tmp_path / "registry.json"
    payload = {"/tmp/x": _entry(created_at=2.0)}
    Registry(path=path).save(payload)
    path.chmod(0o664)

    with pytest.raises(UnsafeRegistryError):
        with Registry(path=path).transaction():
            pass  # pragma: no cover - load() raises before the body runs

    # Re-readable after the owner tightens it by hand; the data survived.
    path.chmod(0o600)
    assert Registry(path=path).load() == payload


@pytest.mark.skipif(not hasattr(os, "getuid"), reason="POSIX ownership required")
def test_load_rejects_registry_owned_by_another_user(tmp_path, monkeypatch):
    path = tmp_path / "registry.json"
    path.write_text("{}", encoding="utf-8")
    path.chmod(0o600)
    real_uid = os.stat(path).st_uid
    monkeypatch.setattr(registry_module.os, "getuid", lambda: real_uid + 1)

    assert Registry(path=path).load() == {}
