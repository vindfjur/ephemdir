"""Registry schema migration v2 -> v3 (0.7.0 Tier 0).

The v2 -> v3 step only adds the optional ``tags``/``description`` fields, so a
v2 file reads cleanly and is rewritten in v3 form on the next save, after a
timestamped backup is taken. These tests pin the safety contract: no entry is
ever lost, an existing backup is never overwritten, read-only loads never
rewrite, and a newer-than-supported schema is still refused.
"""

from __future__ import annotations

import json

import pytest

from ephemdir import _registry as registry_module
from ephemdir._registry import Registry


def _entry(**overrides):
    entry = {"created_at": 1.0, "expires_at": None, "remove_on_restart": True}
    entry.update(overrides)
    return entry


def _v2_file(path, entries):
    payload = {
        "schema_version": 2,
        "writer": {"name": "ephemdir", "format": "registry-v2"},
        "entries": entries,
    }
    path.write_text(json.dumps(payload), encoding="utf-8")
    path.chmod(0o600)


def _on_disk(path):
    return json.loads(path.read_text(encoding="utf-8"))


def test_v2_registry_migrates_to_v3_on_write(tmp_path):
    path = tmp_path / "registry.json"
    original = {"/tmp/old": _entry(marker_id="0" * 32)}
    _v2_file(path, original)

    with Registry(path=path).transaction() as state:
        state["/tmp/new"] = _entry(marker_id="1" * 32)

    disk = _on_disk(path)
    assert disk["schema_version"] == registry_module._REGISTRY_SCHEMA_VERSION == 3
    assert disk["writer"]["format"] == "registry-v3"
    assert set(disk["entries"]) == {"/tmp/old", "/tmp/new"}

    backup = tmp_path / "registry.json.v2.bak"
    assert backup.exists()
    assert _on_disk(backup)["entries"] == original


def test_v2_migration_preserves_every_entry(tmp_path):
    path = tmp_path / "registry.json"
    original = {
        f"/tmp/dir-{i}": _entry(marker_id=str(i) * 32, created_at=float(i))
        for i in range(5)
    }
    _v2_file(path, original)

    # A transaction that changes nothing must still migrate without dropping any
    # entry (transaction() always saves on exit).
    with Registry(path=path).transaction():
        pass

    loaded = Registry(path=path).load()
    assert set(loaded) == set(original)
    assert _on_disk(path)["schema_version"] == 3


def test_v2_migration_is_idempotent(tmp_path):
    path = tmp_path / "registry.json"
    _v2_file(path, {"/tmp/old": _entry(marker_id="0" * 32)})

    with Registry(path=path).transaction():
        pass
    with Registry(path=path).transaction():
        pass

    # Exactly one backup from the single v2 -> v3 migration; the already-v3 file
    # is never backed up again.
    assert len(list(tmp_path.glob("registry.json.v2*.bak"))) == 1
    assert _on_disk(path)["schema_version"] == 3


def test_v2_migration_does_not_overwrite_existing_backup(tmp_path):
    path = tmp_path / "registry.json"
    original = {"/tmp/old": _entry(marker_id="0" * 32)}
    _v2_file(path, original)
    primary = tmp_path / "registry.json.v2.bak"
    primary.write_text(json.dumps({"preexisting": True}), encoding="utf-8")
    primary.chmod(0o600)

    with Registry(path=path).transaction():
        pass

    assert _on_disk(primary) == {"preexisting": True}
    alternates = sorted(tmp_path.glob("registry.json.v2.*.bak"))
    assert len(alternates) == 1
    assert _on_disk(alternates[0])["entries"] == original


def test_read_only_load_does_not_migrate_v2(tmp_path):
    path = tmp_path / "registry.json"
    _v2_file(path, {"/tmp/old": _entry(marker_id="0" * 32)})

    loaded = Registry(path=path).load(read_only=True)

    assert set(loaded) == {"/tmp/old"}
    assert _on_disk(path)["schema_version"] == 2  # untouched
    assert not list(tmp_path.glob("registry.json.v2*.bak"))


def test_v3_registry_loads_without_migration(tmp_path):
    path = tmp_path / "registry.json"
    Registry(path=path).save({"/tmp/a": _entry(marker_id="0" * 32)})
    assert _on_disk(path)["schema_version"] == 3

    with Registry(path=path).transaction():
        pass

    assert not list(tmp_path.glob("registry.json.v2*.bak"))
    assert not list(tmp_path.glob("registry.json.v3*.bak"))


def test_newer_schema_is_refused_not_migrated(tmp_path):
    path = tmp_path / "registry.json"
    future = registry_module._REGISTRY_SCHEMA_VERSION + 1
    path.write_text(
        json.dumps({"schema_version": future, "entries": {}}), encoding="utf-8"
    )
    path.chmod(0o600)

    with pytest.raises(registry_module.RegistryFormatError):
        with Registry(path=path).transaction():
            pass
    assert _on_disk(path)["schema_version"] == future


# --- tags / description fields (schema v3) ---------------------------------


def test_tags_and_description_roundtrip(tmp_path):
    path = tmp_path / "registry.json"
    entry = _entry(
        marker_id="0" * 32,
        tags=["rust", "build", "bench-01"],
        description="cargo benchmark sqlite",
    )
    reg = Registry(path=path)
    reg.save({"/tmp/a": entry})

    loaded = reg.load()["/tmp/a"]
    assert loaded["tags"] == ["rust", "build", "bench-01"]
    assert loaded["description"] == "cargo benchmark sqlite"
    assert _on_disk(path)["schema_version"] == 3


@pytest.mark.parametrize(
    "tags",
    [
        ["UPPER"],            # uppercase not allowed
        ["-leading"],         # must start alphanumeric
        ["has space"],        # no spaces
        ["a" * 33],           # too long
        ["ok", 123],          # non-string element
        [["nested"]],         # non-string element
        ["x"] * 17,           # too many tags
    ],
)
def test_invalid_tags_are_rejected(tmp_path, tags):
    path = tmp_path / "registry.json"
    reg = Registry(path=path)
    with pytest.raises(ValueError):
        reg.save({"/tmp/a": _entry(marker_id="0" * 32, tags=tags)})
    assert not path.exists()


@pytest.mark.parametrize(
    "description",
    [
        "x" * 257,            # over the byte budget
        "line\nbreak",        # control character
        "tab\tchar",          # control character
    ],
)
def test_invalid_descriptions_are_rejected(tmp_path, description):
    path = tmp_path / "registry.json"
    reg = Registry(path=path)
    with pytest.raises(ValueError):
        reg.save({"/tmp/a": _entry(marker_id="0" * 32, description=description)})
    assert not path.exists()


def test_valid_tag_accepts_expected_shapes():
    for tag in ("rust", "build", "a", "0", "bench.01", "a_b-c", "x" * 32):
        assert registry_module._valid_tag(tag) is True
    for tag in ("", "X", "-x", ".x", "white space", "a" * 33, 5, None):
        assert registry_module._valid_tag(tag) is False


# --- failure injection during migration (no entry loss, no partial state) ---


def test_migration_replace_failure_leaves_v2_intact_then_retry_succeeds(tmp_path, monkeypatch):
    path = tmp_path / "registry.json"
    original = {"/tmp/old": _entry(marker_id="0" * 32)}
    _v2_file(path, original)

    def boom(*args, **kwargs):
        raise OSError("simulated replace failure")

    monkeypatch.setattr(registry_module, "_OS_REPLACE", boom)
    with pytest.raises(OSError):
        with Registry(path=path).transaction():
            pass

    # The original file is still the intact v2 registry; nothing partial.
    disk = _on_disk(path)
    assert disk["schema_version"] == 2
    assert set(disk["entries"]) == {"/tmp/old"}
    assert not list(tmp_path.glob("registry.json.tmp*"))

    # A later transaction (replace working again) migrates cleanly.
    monkeypatch.undo()
    with Registry(path=path).transaction():
        pass
    assert _on_disk(path)["schema_version"] == 3
    assert "/tmp/old" in _on_disk(path)["entries"]
    assert list(tmp_path.glob("registry.json.v2*.bak"))


def test_migration_backup_failure_leaves_v2_intact_with_no_partial_backup(tmp_path, monkeypatch):
    path = tmp_path / "registry.json"
    original = {"/tmp/old": _entry(marker_id="0" * 32)}
    _v2_file(path, original)

    def boom(self, dir_fd, label):
        raise OSError("simulated backup failure")

    monkeypatch.setattr(Registry, "_write_format_backup", boom)
    with pytest.raises(OSError):
        with Registry(path=path).transaction():
            pass

    assert _on_disk(path)["schema_version"] == 2
    assert not list(tmp_path.glob("registry.json.v2*.bak"))

    # Retry without the injected failure backs up and migrates.
    monkeypatch.undo()
    with Registry(path=path).transaction():
        pass
    assert _on_disk(path)["schema_version"] == 3
    assert list(tmp_path.glob("registry.json.v2*.bak"))
