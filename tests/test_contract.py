"""Contract snapshot tests (0.8.0 freeze candidate, ТЗ APA-TST-006).

These tests pin the public surface: ``__all__``, callable signatures, the
exception hierarchy and its compatibility bases, the CLI command/option
inventory, exit-code semantics, and the required JSON keys per command. A
failure here means the public contract changed — that is allowed in 0.8.0 only
with a matching MIGRATION.md entry and a deliberate snapshot update.
"""

from __future__ import annotations

import inspect
import json

import pytest

import ephemdir
from ephemdir.cli import build_parser, main

# --- __all__ ----------------------------------------------------------------

EXPECTED_ALL = [
    "tempdir",
    "sweep",
    "registered",
    "keep",
    "extend",
    "remove",
    "resolve",
    "prune",
    "recover",
    "explain",
    "plan_sweep",
    "parse_size",
    "CleanupDecision",
    "CleanupPolicy",
    "SweepMode",
    "EphemeralDirectory",
    "EphemdirError",
    "RegistryError",
    "RegistryUnavailableError",
    "CorruptRegistryError",
    "UnsafeRegistryError",
    "UnsupportedPlatformError",
    "SafetyError",
    "TargetError",
    "__version__",
    "__author__",
]


def test_public_all_snapshot():
    assert ephemdir.__all__ == EXPECTED_ALL


def test_all_names_resolve_and_nothing_private_leaks():
    for name in ephemdir.__all__:
        assert hasattr(ephemdir, name), f"__all__ lists missing name {name!r}"
        assert not name.startswith("_") or name in ("__version__", "__author__")


# --- public signatures --------------------------------------------------------

SIGNATURES = {
    "tempdir": (
        "(lifetime=<UNSET>, *, cleanup=<UNSET>, remove_on_restart=<UNSET>, "
        "keep_while_in_use=<UNSET>, max_size=<UNSET>, name_style=<UNSET>, "
        "parent=<UNSET>, prefix=<UNSET>, words=<UNSET>, tags=None, "
        "description=None, registry=None)"
    ),
    "sweep": "(*, registry=None, force=False, dry_run=False, mode=None, tags=None)",
    "plan_sweep": "(*, registry=None, force=False, mode=None, tags=None)",
    "registered": "(*, registry=None)",
    "keep": "(target, *, registry=None)",
    "extend": "(target, lifetime=None, *, registry=None)",
    "remove": "(target, *, registry=None)",
    "resolve": "(target, *, registry=None)",
    "prune": "(*, registry=None)",
    "recover": "(target, *, action='retry', registry=None)",
    "explain": "(target, *, registry=None, mode=<SweepMode.FULL: 'full'>)",
    "parse_size": "(value)",
}


def _normalized_signature(func) -> str:
    sig = inspect.signature(func)
    parts = []
    for param in sig.parameters.values():
        text = param.name
        if param.kind is inspect.Parameter.KEYWORD_ONLY and "*" not in parts:
            parts.append("*")
        if param.default is not inspect.Parameter.empty:
            default = param.default
            rendered = repr(default)
            if "object object" in rendered:  # the _UNSET sentinel (APA-API-004)
                rendered = "<UNSET>"
            text += f"={rendered}"
        parts.append(text)
    return "(" + ", ".join(parts) + ")"


@pytest.mark.parametrize("name", sorted(SIGNATURES))
def test_public_signature_snapshot(name):
    assert _normalized_signature(getattr(ephemdir, name)) == SIGNATURES[name]


# --- exception hierarchy -------------------------------------------------------


def test_exception_hierarchy_and_compat_bases():
    E = ephemdir
    # single root
    for exc in (
        E.RegistryError,
        E.RegistryUnavailableError,
        E.CorruptRegistryError,
        E.UnsafeRegistryError,
        E.UnsupportedPlatformError,
        E.SafetyError,
        E.TargetError,
    ):
        assert issubclass(exc, E.EphemdirError), exc
    # registry family + legacy RuntimeError compatibility
    for exc in (E.RegistryUnavailableError, E.CorruptRegistryError, E.UnsafeRegistryError):
        assert issubclass(exc, E.RegistryError), exc
        assert issubclass(exc, RuntimeError), exc
    # documented builtin compatibility (APA-API-002)
    assert issubclass(E.TargetError, LookupError)
    assert issubclass(E.UnsupportedPlatformError, OSError)
    assert issubclass(E.SafetyError, OSError)


def test_target_error_raised_for_unknown_and_ambiguous(tmp_path):
    with pytest.raises(ephemdir.TargetError):
        ephemdir.resolve("no-such-directory-anywhere")
    a = ephemdir.tempdir(parent=tmp_path, prefix="twin-")
    b = ephemdir.tempdir(parent=tmp_path, prefix="twin-")
    with pytest.raises(ephemdir.TargetError, match="ambiguous"):
        ephemdir.resolve("twin-")
    # messages distinguish not-found from ambiguous (APA-API-030)
    with pytest.raises(ephemdir.TargetError, match="no tracked directory"):
        ephemdir.resolve("zzz-nothing")
    ephemdir.remove(str(a.path))
    ephemdir.remove(str(b.path))


def test_safety_error_raised_by_deletion_guard(tmp_path, monkeypatch):
    d = ephemdir.tempdir(parent=tmp_path)
    from ephemdir import core

    monkeypatch.setattr(core, "_deletion_guard", lambda path: "test guard engaged")
    with pytest.raises(ephemdir.SafetyError, match="refusing to delete"):
        ephemdir.remove(str(d.path))


def test_unsupported_platform_error_from_tempdir(tmp_path, monkeypatch):
    from ephemdir import core

    monkeypatch.setattr(core, "_safe_delete_backend_error", lambda: "no safe backend")
    with pytest.raises(ephemdir.UnsupportedPlatformError):
        ephemdir.tempdir(parent=tmp_path)


# --- CLI inventory --------------------------------------------------------------

EXPECTED_COMMANDS = {
    "new", "list", "tree", "path", "last", "stats", "keep", "extend", "rm",
    "sweep", "explain", "doctor", "completion", "menu", "prune", "recover",
    "watch", "shell-init", "service", "install-service", "uninstall-service",
}

EXPECTED_COMMAND_OPTIONS = {
    "new": {"--lifetime", "--keep-on-restart", "--keep-while-in-use", "--cleanup",
            "--until-sweep", "--max-size", "--name-style", "--parent", "--prefix",
            "--words", "--tag", "--desc", "--description"},
    "list": {"--json", "--plain", "--tag"},
    "tree": {"--json", "--plain", "--tag"},
    "path": {"--json"},
    "last": {"--json"},
    "stats": {"--json"},
    "keep": {"--tag"},
    "extend": {"--forever"},
    "rm": set(),
    "sweep": {"--force", "--dry-run", "--tag"},
    "explain": {"--json"},
    "doctor": {"--json"},
    "prune": set(),
    "watch": {"--interval"},
    "install-service": {"--interval", "--runtime-policy"},
    "uninstall-service": set(),
}


def _subparsers(parser):
    for action in parser._actions:  # noqa: SLF001 - contract introspection
        if hasattr(action, "choices") and isinstance(action.choices, dict):
            return action.choices
    return {}


def test_cli_command_inventory_snapshot():
    commands = _subparsers(build_parser())
    assert set(commands) == EXPECTED_COMMANDS


@pytest.mark.parametrize("command", sorted(EXPECTED_COMMAND_OPTIONS))
def test_cli_option_inventory_snapshot(command):
    sub = _subparsers(build_parser())[command]
    long_options = {
        opt
        for action in sub._actions  # noqa: SLF001 - contract introspection
        for opt in action.option_strings
        if opt.startswith("--") and opt != "--help"
    }
    assert long_options == EXPECTED_COMMAND_OPTIONS[command], command


def test_cli_global_options_snapshot():
    parser = build_parser()
    long_options = {
        opt
        for action in parser._actions  # noqa: SLF001 - contract introspection
        for opt in action.option_strings
        if opt.startswith("--")
    }
    assert long_options == {"--help", "--version", "--verbose", "--quiet", "--color"}


# --- exit-code semantics (0 / 2 / 1 only, APA-CLI-002) ---------------------------


def test_exit_codes_success_usage_runtime(capsys, tmp_path):
    assert main(["new", "-p", str(tmp_path)]) == 0            # success
    assert main(["new", "-p", str(tmp_path), "--words", "0"]) == 2  # usage/input
    assert main(["path", "definitely-not-there"]) == 1        # runtime failure
    capsys.readouterr()


def test_exit_code_runtime_unusable_parent_is_1_not_2(capsys, tmp_path):
    # A parent that exists but is a file is a RUNTIME failure (OSError), not a
    # usage error: the arguments themselves were fine.
    blocker = tmp_path / "actually-a-file"
    blocker.write_text("", encoding="utf-8")
    assert main(["new", "-p", str(blocker)]) == 1
    capsys.readouterr()


def test_exit_code_invalid_lifetime_on_extend_is_2_not_1(capsys, tmp_path):
    assert main(["new", "-p", str(tmp_path)]) == 0
    name = capsys.readouterr().out.strip().rsplit("/", 1)[-1]
    assert main(["extend", name, "not-a-lifetime-at-all!!"]) == 2
    capsys.readouterr()


def test_exit_code_invalid_lifetime_on_new_is_2_with_no_side_effects(capsys, tmp_path):
    assert main(["new", "-p", str(tmp_path), "--lifetime", "banana"]) == 2
    capsys.readouterr()
    assert list(tmp_path.iterdir()) == []  # validated before any side effect


def test_exit_code_unsupported_backend_is_1(capsys, tmp_path, monkeypatch):
    from ephemdir import core

    monkeypatch.setattr(core, "_safe_delete_backend_error", lambda: "no safe backend")
    assert main(["new", "-p", str(tmp_path)]) == 1
    capsys.readouterr()


def test_exit_code_deletion_guard_refusal_is_1(capsys, tmp_path, monkeypatch):
    assert main(["new", "-p", str(tmp_path)]) == 0
    name = capsys.readouterr().out.strip().rsplit("/", 1)[-1]
    from ephemdir import core

    monkeypatch.setattr(core, "_deletion_guard", lambda path: "guard engaged")
    assert main(["rm", name]) == 1
    capsys.readouterr()


# --- JSON required keys per command (ТЗ §9.1) ------------------------------------

LIST_KEYS = {
    "path", "name", "status", "lifecycle_state", "exists", "original_state",
    "staging_path", "staging_state", "created_at", "expires_at",
    "remaining_seconds", "remove_on_restart", "keep_while_in_use", "tags",
    "description",
}
TREE_KEYS = {"path", "name", "parent", "status", "size_bytes", "size_complete", "tags"}
EXPLAIN_KEYS = {
    "path", "name", "status", "due", "destructive_allowed", "remaining_seconds",
    "size_bytes", "max_size_bytes", "reasons", "blocked_by", "decision", "tags",
    "description",
}
STATS_KEYS = {"created", "swept", "kept", "removed", "currently_tracked"}
DOCTOR_KEYS = {"name", "ok", "message", "hint"}


def test_json_required_keys_snapshot(capsys, tmp_path):
    assert main(["new", "-p", str(tmp_path), "--tag", "ct"]) == 0
    created = capsys.readouterr().out.strip()
    name = created.rsplit("/", 1)[-1]

    assert main(["list", "--json"]) == 0
    row = json.loads(capsys.readouterr().out)[0]
    assert set(row) >= LIST_KEYS

    assert main(["tree", "--json"]) == 0
    row = json.loads(capsys.readouterr().out)[0]
    assert set(row) >= TREE_KEYS

    for command in (["path", name, "--json"], ["last", "--json"]):
        assert main(command) == 0
        payload = json.loads(capsys.readouterr().out)
        assert set(payload) >= {"path", "name"}

    assert main(["explain", name, "--json"]) == 0
    payload = json.loads(capsys.readouterr().out)
    assert set(payload) >= EXPLAIN_KEYS
    assert all(set(check) >= {"check", "ok", "detail"} for check in payload["decision"])

    assert main(["stats", "--json"]) == 0
    payload = json.loads(capsys.readouterr().out)
    assert set(payload) >= STATS_KEYS
    assert all(isinstance(v, int) and v >= 0 for v in payload.values())

    assert main(["doctor", "--json"]) in {0, 1}
    checks = json.loads(capsys.readouterr().out)
    assert all(set(check) >= DOCTOR_KEYS for check in checks)
