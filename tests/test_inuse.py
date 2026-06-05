"""Tests for the keep_while_in_use cleanup protection."""

from __future__ import annotations

import time

from ephemdir.core import sweep, tempdir


def _expire(registry, path):
    with registry.transaction() as state:
        state[str(path)]["expires_at"] = time.time() - 1


def test_in_use_directory_is_deferred(tmp_path, registry, monkeypatch):
    monkeypatch.setattr("ephemdir.core.is_in_use", lambda path: True)
    d = tempdir(lifetime="1h", keep_while_in_use=True, parent=tmp_path, registry=registry)
    _expire(registry, d.path)

    assert sweep(registry=registry) == 0
    assert d.path.is_dir()  # protected because it is "in use"


def test_in_use_directory_removed_once_free(tmp_path, registry, monkeypatch):
    monkeypatch.setattr("ephemdir.core.is_in_use", lambda path: False)
    d = tempdir(lifetime="1h", keep_while_in_use=True, parent=tmp_path, registry=registry)
    _expire(registry, d.path)

    assert sweep(registry=registry) == 1
    assert not d.path.exists()


def test_force_ignores_in_use(tmp_path, registry, monkeypatch):
    monkeypatch.setattr("ephemdir.core.is_in_use", lambda path: True)
    d = tempdir(keep_while_in_use=True, parent=tmp_path, registry=registry)

    assert sweep(registry=registry, force=True) == 1
    assert not d.path.exists()


def test_locked_directory_is_deferred_without_partial_delete(tmp_path, registry, monkeypatch):
    # Simulate a Windows-style lock: the atomic rename fails. The directory and
    # its contents must survive intact, and the entry stays tracked for retry.
    monkeypatch.setattr("ephemdir.core.is_in_use", lambda path: False)

    # Fail only the staging rename in _delete_tree, not the registry's atomic
    # save (which also uses os.replace).
    import os

    real_replace = os.replace

    def _raise(src, dst):
        if ".deleting" in str(dst):
            raise OSError("file is in use")
        return real_replace(src, dst)

    monkeypatch.setattr("ephemdir.core.os.replace", _raise)

    d = tempdir(lifetime="1h", parent=tmp_path, registry=registry)
    (d.path / "open.txt").write_text("still here")
    _expire(registry, d.path)

    assert sweep(registry=registry) == 0
    assert (d.path / "open.txt").read_text() == "still here"  # nothing deleted

    from ephemdir.core import registered
    assert str(d.path) in registered(registry=registry)  # still tracked


def test_delete_tree_is_atomic_on_success(tmp_path):
    from ephemdir.core import _delete_tree

    target = tmp_path / "victim"
    target.mkdir()
    (target / "a.txt").write_text("x")

    assert _delete_tree(target) is True
    assert not target.exists()
    # No leftover staging directories in the parent.
    assert list(tmp_path.iterdir()) == []
