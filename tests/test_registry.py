"""Tests for the persistent registry."""

from __future__ import annotations

import json
import os
import socket
import stat
import threading
from pathlib import Path

import pytest

from ephemdir import _registry as registry_module
from ephemdir._registry import (
    CorruptRegistryError,
    Registry,
    RegistryFormatError,
    RegistryTooLargeError,
    RegistryUnavailableError,
)


def test_load_missing_returns_empty(tmp_path):
    reg = Registry(path=tmp_path / "absent.json")
    assert reg.load() == {}


def test_load_corrupt_raises_and_leaves_active_path(tmp_path):
    path = tmp_path / "registry.json"
    path.write_text("{ this is not valid json")
    path.chmod(0o600)
    reg = Registry(path=path)
    with pytest.raises(CorruptRegistryError):
        reg.load()
    assert path.exists()
    assert not list(tmp_path.glob("registry.json.corrupt-*"))


def test_read_only_corrupt_registry_is_explicit_error(tmp_path):
    path = tmp_path / "registry.json"
    path.write_text("{ this is not valid json")
    path.chmod(0o600)

    with pytest.raises(CorruptRegistryError):
        Registry(path=path).load(read_only=True)

    assert path.exists()
    assert not list(tmp_path.glob("registry.json.corrupt-*"))


@pytest.mark.skipif(not hasattr(os, "symlink"), reason="symlink support required")
def test_read_only_registry_symlink_raises_typed_error(tmp_path):
    target = tmp_path / "real-registry.json"
    Registry(path=target).save({})
    path = tmp_path / "registry.json"
    path.symlink_to(target)

    with pytest.raises(RegistryUnavailableError):
        Registry(path=path).load(read_only=True)


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
    result: list[type[BaseException]] = []

    def load_fifo() -> None:
        try:
            Registry(path=path).load()
        except BaseException as error:
            result.append(type(error))

    thread = threading.Thread(
        target=load_fifo,
        daemon=True,
    )
    thread.start()
    thread.join(timeout=1.0)

    assert not thread.is_alive(), "registry load blocked while opening a FIFO"
    assert result == [RegistryUnavailableError]
    assert not list(tmp_path.glob("registry.json.corrupt-*"))


@pytest.mark.skipif(not hasattr(os, "mkfifo"), reason="FIFO support required")
def test_transaction_fifo_is_unavailable_not_quarantined(tmp_path):
    path = tmp_path / "registry.json"
    os.mkfifo(path, 0o600)

    with pytest.raises(RegistryUnavailableError):
        with Registry(path=path).transaction():
            pass

    assert path.exists()
    assert not list(tmp_path.glob("registry.json.corrupt-*"))


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
        with pytest.raises(RegistryUnavailableError):
            Registry(path=path).load()
        assert not list(path.parent.glob("registry.json.corrupt-*"))
    finally:
        server.close()
        if fallback_dir is not None:
            shutil.rmtree(fallback_dir, ignore_errors=True)


@pytest.mark.skipif(os.name != "posix", reason="POSIX device files required")
def test_load_rejects_device_file():
    with pytest.raises(RegistryUnavailableError):
        Registry(path=Path(os.devnull)).load()


@pytest.mark.skipif(not hasattr(os, "symlink"), reason="symlink support required")
def test_direct_registry_symlink_load_is_unavailable_not_empty(tmp_path):
    target = tmp_path / "real-registry.json"
    Registry(path=target).save({})
    path = tmp_path / "registry.json"
    path.symlink_to(target)

    with pytest.raises(RegistryUnavailableError):
        Registry(path=path).load()
    assert path.is_symlink()
    assert not list(tmp_path.glob("registry.json.corrupt-*"))


def test_load_rejects_oversized_registry(tmp_path):
    path = tmp_path / "registry.json"
    path.write_bytes(b"{}" + b" " * registry_module._MAX_REGISTRY_BYTES)
    path.chmod(0o600)

    reg = Registry(path=path)
    with pytest.raises(RegistryTooLargeError):
        reg.load()
    with pytest.raises(RegistryTooLargeError):
        with reg.transaction():
            pass

    with pytest.raises(RegistryTooLargeError):
        reg.load()
    assert path.exists()
    assert not list(tmp_path.glob("registry.json.corrupt-*"))


def test_save_refuses_registry_it_cannot_later_read(tmp_path):
    reg = Registry(path=tmp_path / "registry.json")
    payload = {
        f"/tmp/ephemdir-{index:05d}": _entry(marker_id="0" * 32)
        for index in range(registry_module._MAX_REGISTRY_ENTRIES + 1)
    }

    with pytest.raises(RegistryTooLargeError):
        reg.save(payload)

    assert not reg.path.exists()


def test_transaction_does_not_replace_existing_registry_when_new_state_is_too_large(tmp_path):
    reg = Registry(path=tmp_path / "registry.json")
    original = {"/tmp/original": _entry(marker_id="0" * 32)}
    reg.save(original)
    before = reg.path.read_bytes()

    with pytest.raises(RegistryTooLargeError):
        with reg.transaction() as state:
            for index in range(registry_module._MAX_REGISTRY_ENTRIES + 1):
                state[f"/tmp/ephemdir-{index:05d}"] = _entry(marker_id="1" * 32)

    assert reg.path.read_bytes() == before
    assert reg.load() == original


def test_save_rejects_active_entry_with_claim_or_staging(tmp_path):
    reg = Registry(path=tmp_path / "registry.json")
    staging = tmp_path / ".x.123-abcdef12.deleting"
    payload = {
        str(tmp_path / "x"): _entry(
            state="active",
            claim_id="1" * 32,
            staging_path=str(staging),
        )
    }

    with pytest.raises(ValueError):
        reg.save(payload)

    assert not reg.path.exists()


@pytest.mark.parametrize(
    "field, value",
    [
        ("marker_id", "not-hex"),
        ("claim_id", "not-hex"),
        ("claim_id", "A" * 32),
    ],
)
def test_save_rejects_invalid_marker_or_claim_id(tmp_path, field, value):
    reg = Registry(path=tmp_path / "registry.json")
    entry = _entry(state="moving", claim_id="1" * 32, staging_path=str(tmp_path / ".x.1.deleting"))
    entry[field] = value

    with pytest.raises(ValueError):
        reg.save({str(tmp_path / "x"): entry})

    assert not reg.path.exists()


def test_read_only_malformed_entry_blocks_entire_registry(tmp_path):
    path = tmp_path / "registry.json"
    payload = {
        "schema_version": 2,
        "writer": {"name": "ephemdir", "format": "registry-v2"},
        "entries": {
            str(tmp_path / "good"): _entry(marker_id="1" * 32),
            str(tmp_path / "bad"): _entry(marker_id="broken"),
        },
    }
    path.write_text(json.dumps(payload), encoding="utf-8")
    path.chmod(0o600)

    with pytest.raises(CorruptRegistryError, match="bad"):
        Registry(path=path).load(read_only=True)

    assert path.exists()
    assert not list(tmp_path.glob("registry.json.corrupt-*"))


def test_transaction_never_saves_filtered_registry(tmp_path):
    path = tmp_path / "registry.json"
    original = {
        str(tmp_path / "good"): _entry(marker_id="1" * 32),
        str(tmp_path / "bad"): _entry(marker_id="broken"),
    }
    path.write_text(json.dumps(original), encoding="utf-8")
    path.chmod(0o600)
    original_bytes = path.read_bytes()

    with pytest.raises(CorruptRegistryError):
        with Registry(path=path).transaction() as state:
            state.clear()

    quarantines = list(tmp_path.glob("registry.json.corrupt-*"))
    assert len(quarantines) == 1
    assert quarantines[0].read_bytes() == original_bytes
    assert path.read_bytes() == original_bytes


@pytest.mark.parametrize(
    "key",
    [
        "/tmp/with\x00nul",
        "/tmp/with\nnewline",
        "/tmp/with\rreturn",
    ],
)
def test_registry_control_character_path_is_corrupt(tmp_path, key):
    path = tmp_path / "registry.json"
    path.write_text(json.dumps({key: _entry(marker_id="1" * 32)}), encoding="utf-8")
    path.chmod(0o600)

    with pytest.raises(CorruptRegistryError):
        Registry(path=path).load(read_only=True)


@pytest.mark.parametrize(
    "entry",
    [
        _entry(cleanup_policy="surprise"),
        _entry(max_size=-1),
        _entry(platform=""),
        _entry(backend=""),
        _entry(name_style="purple"),
    ],
)
def test_save_rejects_invalid_policy_ranges_and_identity_fields(tmp_path, entry):
    reg = Registry(path=tmp_path / "registry.json")

    with pytest.raises(ValueError):
        reg.save({str(tmp_path / "x"): entry})

    assert not reg.path.exists()


def test_legacy_registry_migration_creates_backup(tmp_path):
    path = tmp_path / "registry.json"
    legacy = {"/tmp/legacy": _entry(marker_id="0" * 32)}
    path.write_text(json.dumps(legacy), encoding="utf-8")
    path.chmod(0o600)

    with Registry(path=path).transaction() as state:
        state["/tmp/new"] = _entry(marker_id="1" * 32)

    backup = tmp_path / "registry.json.v1.bak"
    assert json.loads(backup.read_text(encoding="utf-8")) == legacy
    assert Registry(path=path).load()["/tmp/new"]["marker_id"] == "1" * 32


def test_legacy_registry_migration_does_not_overwrite_existing_backup(tmp_path):
    path = tmp_path / "registry.json"
    legacy = {"/tmp/legacy": _entry(marker_id="0" * 32)}
    existing = {"/tmp/existing": _entry(marker_id="2" * 32)}
    path.write_text(json.dumps(legacy), encoding="utf-8")
    path.chmod(0o600)
    primary_backup = tmp_path / "registry.json.v1.bak"
    primary_backup.write_text(json.dumps(existing), encoding="utf-8")
    primary_backup.chmod(0o600)

    with Registry(path=path).transaction() as state:
        state["/tmp/new"] = _entry(marker_id="1" * 32)

    assert json.loads(primary_backup.read_text(encoding="utf-8")) == existing
    alternates = sorted(tmp_path.glob("registry.json.v1.*.bak"))
    assert len(alternates) == 1
    assert json.loads(alternates[0].read_text(encoding="utf-8")) == legacy
    assert Registry(path=path).load()["/tmp/new"]["marker_id"] == "1" * 32


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
    from ephemdir._registry import UnsafeRegistryError

    path = tmp_path / "registry.json"
    path.write_text("{}", encoding="utf-8")
    path.chmod(0o600)
    real_uid = os.stat(path).st_uid
    monkeypatch.setattr(registry_module, "_ensure_private_directory", lambda path: None)
    monkeypatch.setattr(registry_module.os, "getuid", lambda: real_uid + 1)

    with pytest.raises(UnsafeRegistryError):
        Registry(path=path).load()
    assert path.exists()
    assert not list(tmp_path.glob("registry.json.corrupt-*"))


def test_transaction_does_not_replace_foreign_owned_registry(tmp_path, monkeypatch):
    from ephemdir._registry import UnsafeRegistryError

    path = tmp_path / "registry.json"
    payload = b'{"schema_version": 2, "entries": {}, "writer": {}}\n'
    path.write_bytes(payload)
    path.chmod(0o600)
    real_uid = os.stat(path).st_uid
    monkeypatch.setattr(registry_module, "_ensure_private_directory", lambda path: None)
    monkeypatch.setattr(registry_module.os, "getuid", lambda: real_uid + 1)

    with pytest.raises(UnsafeRegistryError):
        with Registry(path=path).transaction():
            pass

    assert path.read_bytes() == payload
    assert not list(tmp_path.glob("registry.json.corrupt-*"))


def test_future_schema_is_reported_not_treated_as_empty(tmp_path):
    path = tmp_path / "registry.json"
    path.write_text('{"schema_version": 999, "entries": {}}', encoding="utf-8")
    path.chmod(0o600)

    with pytest.raises(RegistryFormatError):
        Registry(path=path).load()
    with pytest.raises(RegistryFormatError):
        with Registry(path=path).transaction():
            pass
    assert path.exists()
    assert not list(tmp_path.glob("registry.json.corrupt-*"))


def test_low_level_file_helpers_fail_closed(tmp_path, monkeypatch):
    registry_module._fsync_directory(tmp_path / "missing")

    fd = os.open(tmp_path, os.O_RDONLY)
    try:
        monkeypatch.setattr(
            registry_module.os,
            "fsync",
            lambda fd: (_ for _ in ()).throw(OSError("fsync failed")),
        )
        registry_module._fsync_dir_fd(fd)
    finally:
        os.close(fd)

    if hasattr(os, "fchmod"):
        monkeypatch.setattr(registry_module.os, "fchmod", lambda fd, mode: None)
    opened = registry_module._open_owner_file(tmp_path / "support.lock", os.O_CREAT | os.O_RDWR)
    os.close(opened)

    if hasattr(os, "fchmod"):
        monkeypatch.setattr(
            registry_module.os,
            "fchmod",
            lambda fd, mode: (_ for _ in ()).throw(OSError("chmod failed")),
        )
        with pytest.raises(OSError, match="chmod failed"):
            registry_module._open_owner_file(tmp_path / "bad.lock", os.O_CREAT | os.O_RDWR)

    with pytest.raises(ValueError, match="unsafe registry filename"):
        registry_module._safe_child_name(Path("/"))


def test_random_temp_helpers_report_exhaustion(tmp_path, monkeypatch):
    target = tmp_path / "registry.json"
    monkeypatch.setattr(registry_module, "_TEMP_OPEN_ATTEMPTS", 1)
    monkeypatch.setattr(
        registry_module,
        "_open_owner_file",
        lambda path, flags: (_ for _ in ()).throw(FileExistsError),
    )
    with pytest.raises(FileExistsError, match="unique temporary"):
        registry_module._open_random_temp(target)

    dir_fd = os.open(tmp_path, os.O_RDONLY)
    try:
        monkeypatch.setattr(
            registry_module,
            "_open_owner_file_at",
            lambda dir_fd, name, flags: (_ for _ in ()).throw(FileExistsError),
        )
        with pytest.raises(FileExistsError, match="unique temporary"):
            registry_module._open_random_temp_at(dir_fd, "registry.json")
    finally:
        os.close(dir_fd)


def test_tighten_owner_only_reports_unfixable_modes(tmp_path, monkeypatch):
    path = tmp_path / "registry.json"
    path.write_text("{}", encoding="utf-8")
    fd = os.open(path, os.O_RDWR)
    try:
        original_fstat = os.fstat
        if hasattr(os, "fchmod"):
            monkeypatch.setattr(
                registry_module.os,
                "fchmod",
                lambda fd, mode: (_ for _ in ()).throw(OSError("chmod failed")),
            )
            with pytest.raises(ValueError, match="could not be made private"):
                registry_module._tighten_owner_only(fd, original_fstat(fd))

            monkeypatch.setattr(registry_module.os, "fchmod", lambda fd, mode: None)
            monkeypatch.setattr(
                registry_module.os,
                "fstat",
                lambda fd: os.stat_result(
                    (
                        stat.S_IFREG | 0o644,
                        1,
                        1,
                        1,
                        os.getuid() if hasattr(os, "getuid") else 0,
                        0,
                        2,
                        0,
                        0,
                        0,
                    )
                ),
            )
            with pytest.raises(ValueError, match="still accessible"):
                registry_module._tighten_owner_only(fd, original_fstat(fd))
    finally:
        os.close(fd)


@pytest.mark.parametrize(
    ("key", "entry"),
    [
        ("/tmp/x", object()),
        ("relative", _entry(marker_id="0" * 32)),
        ("/tmp/x", {}),
        ("/tmp/x", _entry(created_at=True)),
        ("/tmp/x", _entry(created_at="now")),
        ("/tmp/x", _entry(created_at=float("inf"))),
        ("/tmp/x", _entry(dev=-1)),
        ("/tmp/x", _entry(last_error="x" * 2000)),
        ("/tmp/x", _entry(platform="bad\nplatform")),
        ("/tmp/x", _entry(staging_path="relative")),
        ("/tmp/x", _entry(state="surprise")),
        ("/tmp/x", _entry(marker_id="BAD")),
        ("/tmp/x", _entry(state="moving", claim_id=None, staging_path="/tmp/.x.1.deleting")),
        ("/tmp/x", _entry(state="deleting", staging_path="/tmp/not-staging")),
        ("/tmp/x", _entry(state="deleting", dev=None, staging_path="/tmp/.x.1.deleting")),
        ("/tmp/x", _entry(state="recovery", claim_id="1" * 32)),
    ],
)
def test_valid_entry_rejects_malformed_shapes(key, entry):
    assert registry_module._valid_entry(key, entry) is False


def test_registry_schema_helpers_reject_unsupported_envelopes():
    with pytest.raises(ValueError, match="top level"):
        registry_module._extract_entries([])
    with pytest.raises(RegistryFormatError, match="integer"):
        registry_module._extract_entries({"schema_version": True, "entries": {}})
    with pytest.raises(RegistryFormatError, match="unsupported"):
        registry_module._extract_entries({"schema_version": 1, "entries": {}})
    with pytest.raises(ValueError, match="entries"):
        registry_module._extract_entries({"schema_version": 2, "entries": []})


def test_state_bounds_reject_oversized_forward_compatible_entry(tmp_path):
    entry = _entry(marker_id="0" * 32)
    entry["future_payload"] = "x" * (registry_module._MAX_ENTRY_BYTES + 1)

    with pytest.raises(RegistryTooLargeError, match="larger"):
        registry_module._validate_state_bounds({str(tmp_path / "x"): entry})
