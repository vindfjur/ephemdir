"""Tags and description metadata (0.7.0 feature tier)."""

from __future__ import annotations

import json
import os
from pathlib import Path

import pytest

from ephemdir import registered, sweep, tempdir
from ephemdir.cli import main

# --- library API -----------------------------------------------------------


def test_tempdir_stores_tags_and_description(tmp_path):
    d = tempdir(parent=tmp_path, tags=["rust", "build"], description="cargo bench")
    entry = registered()[str(d.path)]
    assert entry["tags"] == ["rust", "build"]
    assert entry["description"] == "cargo bench"


def test_tempdir_without_metadata_omits_keys(tmp_path):
    d = tempdir(parent=tmp_path)
    entry = registered()[str(d.path)]
    # Untagged directories keep a byte-identical entry to earlier versions.
    assert "tags" not in entry
    assert "description" not in entry


def test_tempdir_dedupes_tags_preserving_order(tmp_path):
    d = tempdir(parent=tmp_path, tags=["a", "b", "a"])
    assert registered()[str(d.path)]["tags"] == ["a", "b"]


@pytest.mark.parametrize(
    "bad",
    [["UPPER"], ["has space"], ["-lead"], ["x" * 33], [f"t{i}" for i in range(17)]],
)
def test_tempdir_rejects_invalid_tags(tmp_path, bad):
    with pytest.raises(ValueError):
        tempdir(parent=tmp_path, tags=bad)


def test_tempdir_rejects_string_tags(tmp_path):
    with pytest.raises(TypeError):
        tempdir(parent=tmp_path, tags="rust")  # type: ignore[arg-type]


@pytest.mark.parametrize(
    "bad",
    [
        "x" * 257,
        "line\nbreak",
        "tab\tchar",
        "rtl" + chr(0x202E) + "flip",   # RC5-1: Unicode Cf
        "byte" + chr(0xDC9B) + "smuggle",  # RC6-1: surrogateescape raw C1 byte
    ],
)
def test_tempdir_rejects_invalid_description(tmp_path, bad):
    with pytest.raises(ValueError):
        tempdir(parent=tmp_path, description=bad)


def test_tempdir_rejects_surrogateescape_prefix(tmp_path):
    # RC6-1: a raw undecodable byte (surrogateescape, category Cs) in a name
    # prefix is refused, not silently baked into a directory name.
    with pytest.raises(ValueError):
        tempdir(parent=tmp_path, prefix="bad" + chr(0xDC9B))


@pytest.mark.parametrize("marker", [chr(0x202E), chr(0xDC9B)])
def test_tempdir_rejects_control_chars_in_parent(tmp_path, marker):
    # RC7-1: a parent path carrying control/format/surrogate characters fails at
    # validation, before any existence check or side effect. (The parent need
    # not exist; a lone surrogate byte cannot even be created on some
    # filesystems, which is exactly why validation must precede the FS touch.)
    bad_parent = tmp_path / f"pa{marker}rent"
    with pytest.raises(ValueError):
        tempdir(parent=bad_parent)


def test_new_rejects_control_chars_in_parent(capsys, tmp_path):
    bad_parent = tmp_path / f"pa{chr(0x202E)}rent"
    bad_parent.mkdir()
    assert main(["new", "-p", str(bad_parent)]) == 2
    assert "parent" in capsys.readouterr().err
    assert list(bad_parent.iterdir()) == []


def test_invalid_parent_creates_no_config_dir(capsys, tmp_path):
    # RC8-1: reading config must not create the config directory, so an invalid
    # parent fails with no filesystem side effect at all.
    config_dir = Path(os.environ["EPHEMDIR_CONFIG_DIR"])
    assert not config_dir.exists()
    bad_parent = tmp_path / f"pa{chr(0x202E)}rent"
    bad_parent.mkdir()
    assert main(["new", "-p", str(bad_parent)]) == 2
    assert not config_dir.exists()


# --- CLI -------------------------------------------------------------------


def _create(capsys, tmp_path, *args):
    assert main(["new", "-p", str(tmp_path), *args]) == 0
    return capsys.readouterr().out.strip()


def test_new_with_tags_and_description_in_json(capsys, tmp_path):
    created = _create(capsys, tmp_path, "--tag", "rust", "--tag", "build",
                      "--desc", "cargo benchmark")
    assert main(["list", "--json"]) == 0
    row = next(r for r in json.loads(capsys.readouterr().out) if r["path"] == created)
    assert row["tags"] == ["rust", "build"]
    assert row["description"] == "cargo benchmark"


def test_new_rejects_invalid_tag(capsys, tmp_path):
    assert main(["new", "-p", str(tmp_path), "--tag", "Nope!"]) == 2
    assert "invalid tag" in capsys.readouterr().err


def test_list_human_shows_tags_and_description(capsys, tmp_path):
    _create(capsys, tmp_path, "--tag", "rust", "--desc", "build the thing")
    assert main(["--color", "never", "list"]) == 0
    out = capsys.readouterr().out
    assert "#rust" in out
    assert "build the thing" in out


def test_list_tag_filter(capsys, tmp_path):
    a = _create(capsys, tmp_path, "--tag", "rust")
    b = _create(capsys, tmp_path, "--tag", "docs")
    assert main(["list", "--json", "--tag", "rust"]) == 0
    paths = {r["path"] for r in json.loads(capsys.readouterr().out)}
    assert a in paths and b not in paths


def test_tree_tag_filter_and_json_tags(capsys, tmp_path):
    a = _create(capsys, tmp_path, "--tag", "build")
    _create(capsys, tmp_path, "--tag", "docs")
    assert main(["tree", "--json", "--tag", "build"]) == 0
    rows = json.loads(capsys.readouterr().out)
    assert [r["path"] for r in rows] == [a]
    assert rows[0]["tags"] == ["build"]


def test_list_tag_filter_rejects_invalid_value(capsys, tmp_path):
    _create(capsys, tmp_path, "--tag", "rust")
    assert main(["list", "--tag", "Nope!"]) == 2
    assert "invalid tag" in capsys.readouterr().err


def test_tree_tag_filter_rejects_invalid_value(capsys, tmp_path):
    _create(capsys, tmp_path, "--tag", "rust")
    assert main(["tree", "--tag", "Nope!"]) == 2
    assert "invalid tag" in capsys.readouterr().err


def test_explain_shows_and_reports_metadata(capsys, tmp_path):
    created = _create(capsys, tmp_path, "--tag", "rust", "--desc", "the note")
    name = created.rsplit("/", 1)[-1]
    assert main(["explain", name]) == 0
    out = capsys.readouterr().out
    assert "#rust" in out and "the note" in out
    assert main(["explain", name, "--json"]) == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["tags"] == ["rust"]
    assert payload["description"] == "the note"


# --- tag-based destructive filters -----------------------------------------


def test_sweep_tag_only_removes_matching(tmp_path):
    a = tempdir(parent=tmp_path, tags=["build"])
    b = tempdir(parent=tmp_path, tags=["docs"])
    assert sweep(force=True, tags=["build"]) == 1
    assert not a.path.exists()
    assert b.path.exists()  # the docs directory was never touched


def test_sweep_tag_invalid_value_is_usage_error(capsys, tmp_path):
    tempdir(parent=tmp_path, tags=["build"])
    assert main(["sweep", "--tag", "Nope!"]) == 2
    assert "invalid tag" in capsys.readouterr().err


def test_keep_tag_keeps_all_matching(capsys, tmp_path):
    a = _create(capsys, tmp_path, "--tag", "build")
    b = _create(capsys, tmp_path, "--tag", "docs")
    c = _create(capsys, tmp_path, "--tag", "docs")
    assert main(["keep", "--tag", "docs"]) == 0
    capsys.readouterr()
    assert main(["list", "--json"]) == 0
    tracked = {row["path"] for row in json.loads(capsys.readouterr().out)}
    assert a in tracked          # build directory still tracked
    assert b not in tracked and c not in tracked  # docs ones kept (untracked)
    assert Path(b).is_dir() and Path(c).is_dir()  # kept on disk


def test_keep_tag_with_target_is_usage_error(capsys, tmp_path):
    _create(capsys, tmp_path, "--tag", "x")
    assert main(["keep", "somedir", "--tag", "x"]) == 2
    assert "not both" in capsys.readouterr().err


def test_keep_tag_partial_failure_exits_nonzero(capsys, tmp_path, monkeypatch):
    # RC9-1: if any directory in the batch cannot be kept, the command fails
    # (exit 1) while still keeping the ones that succeed.
    from ephemdir import cli

    good = _create(capsys, tmp_path, "--tag", "docs")
    bad = _create(capsys, tmp_path, "--tag", "docs")
    real_keep = cli.keep

    def flaky_keep(target, **kwargs):
        if str(target) == bad:
            raise OSError("simulated concurrent claim")
        return real_keep(target, **kwargs)

    monkeypatch.setattr(cli, "keep", flaky_keep)
    assert main(["keep", "--tag", "docs"]) == 1
    err = capsys.readouterr().err
    assert "ephemdir: keep:" in err
    # the healthy directory was still kept (untracked)
    assert main(["list", "--json"]) == 0
    tracked = {row["path"] for row in json.loads(capsys.readouterr().out)}
    assert good not in tracked
