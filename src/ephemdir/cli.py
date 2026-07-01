"""Command-line interface for ephemdir.

Exposes the library through a small ``ephemdir`` command:

* ``ephemdir new``        -- create a new ephemeral directory and print its path
* ``ephemdir list``       -- show tracked directories with status and time left
* ``ephemdir tree``       -- show tracked directories grouped by parent, with sizes
* ``ephemdir path``       -- print the path of a tracked directory by name
* ``ephemdir last``       -- print the most recently created tracked directory
* ``ephemdir keep``       -- stop tracking a directory (make it permanent)
* ``ephemdir extend``     -- give a directory a fresh lifetime
* ``ephemdir rm``         -- remove a tracked directory now
* ``ephemdir sweep``      -- remove every directory that is due for cleanup
* ``ephemdir explain``    -- trace why a directory will or will not be removed
* ``ephemdir stats``      -- show lifetime usage counters
* ``ephemdir prune``      -- forget missing tracked directories explicitly
* ``ephemdir watch``      -- run a foreground loop that sweeps periodically
* ``ephemdir shell-init`` -- print shell functions (``ecd``, ``enew``) to eval

Read-only commands (``list``, ``tree``, ``path``, ``last``, ``explain``,
``doctor``) accept ``--json`` for a stable machine-readable contract; the global
``--color {auto,always,never}`` flag (honouring ``NO_COLOR``) controls ANSI
colour, which is off automatically when output is piped.

``ephemdir new`` accepts ``--tag`` (repeatable) and ``--desc`` to label a
directory; ``list``, ``tree``, ``sweep`` and ``keep`` accept ``--tag`` to act
only on directories carrying every given tag.
"""

from __future__ import annotations

import argparse
import logging
import os
import sys
import time
from collections.abc import Sequence
from pathlib import Path
from typing import Any, TextIO

from . import __version__
from ._completion import completion_script
from ._display import (
    Painter,
    color_enabled,
    emit_json,
    escape_human_text,
    explain_payload,
    explain_trace,
    format_decision,
    format_size,
    supports_unicode,
)
from ._display import (
    format_duration as _format_duration,
)
from ._display import (
    format_timestamp as _format_timestamp,  # noqa: F401 - re-exported for tests/scripts
)
from ._doctor import run_doctor
from ._menu import run_menu
from ._platform import boot_session_id, boot_time
from ._registry import (
    Entry,
    RegistryFormatError,
    RegistryUnavailableError,
    UnsafeRegistryError,
    _valid_tag,
)
from ._service import ServiceError, install_service, uninstall_service
from ._size import measure_tree
from ._stats import StatsLedger
from .core import (
    _UNSET,
    _current_target_path,
    _CurrentTargetNotFound,
    _path_state,
    dir_status,
    explain,
    extend,
    keep,
    parse_lifetime,
    plan_sweep,
    prune,
    recover,
    registered,
    remove,
    resolve,
    sweep,
    tempdir,
)

logger = logging.getLogger("ephemdir")


def _is_posix_platform() -> bool:
    """Return whether the current platform supports ephemdir operations."""
    return os.name == "posix"


# Status icons for `ephemdir list`, with an ASCII fallback for terminals whose
# encoding cannot represent emoji (e.g. some Windows consoles).
_ICONS = {
    "active": "🟢",
    "expiring": "🟡",
    "expired": "🔴",
    "until-restart": "🔄",
    "until-sweep": "🧹",
    "kept": "📌",
    "missing": "👻",
    "replaced": "⚠️",
    "legacy": "⚪",
    "deleting": "🗑️",
    "recovery": "🚧",
    "unavailable": "❓",
    "blocked": "⛔",
}
# Statuses ephemdir owns and may safely measure in `tree` (see _measured_size).
_MEASURABLE_STATUSES = frozenset(
    {"active", "expiring", "expired", "until-restart", "until-sweep", "kept"}
)
# Colour styles per status, applied to the note column of `ephemdir list`.
_STATUS_STYLES = {
    "active": ("green",),
    "expiring": ("yellow",),
    "expired": ("red",),
    "until-restart": ("cyan",),
    "until-sweep": ("cyan",),
    "kept": ("green",),
    "missing": ("dim",),
    "replaced": ("yellow",),
    "legacy": ("dim",),
    "deleting": ("yellow",),
    "recovery": ("red",),
    "unavailable": ("dim",),
    "blocked": ("red",),
}
_ASCII_ICONS = {
    "active": "[ok]  ",
    "expiring": "[soon]",
    "expired": "[due] ",
    "until-restart": "[boot]",
    "until-sweep": "[swp] ",
    "kept": "[pin] ",
    "missing": "[miss]",
    "replaced": "[warn]",
    "legacy": "[old] ",
    "deleting": "[del] ",
    "recovery": "[rec] ",
    "unavailable": "[n/a] ",
    "blocked": "[blk] ",
}

# Shell snippets emitted by `ephemdir shell-init`. A subprocess cannot change
# its parent shell's working directory, so navigation has to be a function
# defined in the user's shell -- the same technique zoxide and nvm use.
_POSIX_SNIPPET = """\
# ephemdir shell integration -- add to your shell rc file:
#   eval "$(ephemdir shell-init)"
ecd() {
    local target
    target="$(command ephemdir path "$@")" || return 1
    cd "$target" || return 1
}
enew() {
    local target
    target="$(command ephemdir new "$@")" || return 1
    cd "$target" || return 1
}
"""
_FISH_SNIPPET = """\
# ephemdir shell integration -- add to ~/.config/fish/config.fish:
#   ephemdir shell-init fish | source
function ecd --description "cd into a tracked ephemeral directory"
    set -l target (command ephemdir path $argv); or return 1
    cd $target
end
function enew --description "create an ephemeral directory and cd into it"
    set -l target (command ephemdir new $argv); or return 1
    cd $target
end
"""
_POWERSHELL_SNIPPET = """\
# ephemdir shell integration -- add to your PowerShell $PROFILE:
#   Invoke-Expression (& ephemdir shell-init powershell | Out-String)
function ecd {
    $target = ephemdir path @args
    if ($LASTEXITCODE -eq 0 -and $target) { Set-Location $target }
}
function enew {
    $target = ephemdir new @args
    if ($LASTEXITCODE -eq 0 -and $target) { Set-Location $target }
}
"""
_SHELL_SNIPPETS = {
    "bash": _POSIX_SNIPPET,
    "zsh": _POSIX_SNIPPET,
    "fish": _FISH_SNIPPET,
    "powershell": _POWERSHELL_SNIPPET,
}


class _NoAbbrevArgumentParser(argparse.ArgumentParser):
    """ArgumentParser variant that never accepts abbreviated long options."""

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        kwargs.setdefault("allow_abbrev", False)
        super().__init__(*args, **kwargs)


def _configure_logging(verbosity: int, quiet: bool) -> None:
    """Set up output verbosity for the CLI run.

    ``-q`` silences everything but errors; ``-v`` / ``-vv`` make it chattier.
    """
    if quiet:
        level = logging.ERROR
    elif verbosity >= 2:
        level = logging.DEBUG
    elif verbosity == 1:
        level = logging.INFO
    else:
        level = logging.WARNING
    # force=True rebinds the handler to the current sys.stderr on every run,
    # so repeated main() calls (tests, REPL) never log to a stale stream.
    logging.basicConfig(level=level, format="%(message)s", stream=sys.stderr, force=True)


def _painter(stream: TextIO, args: argparse.Namespace) -> Painter:
    """Build a colour painter for ``stream`` honouring the global --color flag."""
    return Painter(color_enabled(stream, getattr(args, "color", "auto")))


def _fail(args: argparse.Namespace, message: object) -> None:
    """Emit a uniform ``ephemdir: <command>: <reason>`` error to stderr."""
    command = getattr(args, "command", None) or "ephemdir"
    logger.error("ephemdir: %s: %s", command, message)


def _status_note(status: str, entry: Entry, now: float) -> str:
    """One short human phrase describing what happens to the directory next."""
    if status == "missing":
        return "missing; still tracked"
    if status == "replaced":
        return "replaced by another directory; will not be touched"
    if status == "legacy":
        return "from an older ephemdir; use rm or keep to resolve"
    if status == "deleting":
        return "partially deleted; sweeps will retry"
    if status == "recovery":
        return "interrupted deletion; use ephemdir recover"
    if status == "unavailable":
        return "temporarily inaccessible; still tracked"
    if status == "blocked":
        blockers = []
        backend = entry.get("backend")
        if isinstance(backend, str) and backend != "posix":
            blockers.append("unsupported backend")
        platform = entry.get("platform")
        if isinstance(platform, str) and platform != sys.platform:
            blockers.append("foreign platform")
        return "; ".join(blockers) if blockers else "blocked"
    if status == "kept":
        return "no auto-cleanup"
    if status == "until-restart":
        return "until restart"
    if status == "until-sweep":
        return "until next full sweep"
    expires_at = entry.get("expires_at")
    if status == "expired":
        if isinstance(expires_at, (int, float)) and now >= float(expires_at):
            return f"expired {_format_duration(now - float(expires_at))} ago"
        return "due now (machine restarted)"
    # active / expiring
    if isinstance(expires_at, (int, float)):
        return f"{_format_duration(float(expires_at) - now)} left"
    return "tracked"


def _supports_emoji(stream: TextIO) -> bool:
    """Return ``True`` when ``stream`` can encode the status emoji."""
    encoding = getattr(stream, "encoding", None) or ""
    try:
        "🟢".encode(encoding)
    except (LookupError, UnicodeEncodeError):
        return False
    return True


def _created_at(entry: Entry) -> float:
    value = entry.get("created_at")
    return float(value) if isinstance(value, (int, float)) else 0.0


def _entry_tags(entry: Entry) -> list[str]:
    tags = entry.get("tags")
    return [tag for tag in tags if isinstance(tag, str)] if isinstance(tags, list) else []


def _entry_description(entry: Entry) -> str | None:
    description = entry.get("description")
    return description if isinstance(description, str) else None


def _matches_tags(entry: Entry, wanted: list[str] | None) -> bool:
    """True when ``entry`` carries every requested tag (AND filter)."""
    if not wanted:
        return True
    have = set(_entry_tags(entry))
    return all(tag in have for tag in wanted)


def _reject_invalid_filter_tags(args: argparse.Namespace) -> bool:
    """Report (and signal exit 2 on) an invalid ``--tag`` filter value.

    A bogus filter tag is a usage error, mirroring tag validation at creation,
    rather than silently matching nothing.
    """
    for tag in getattr(args, "tag", None) or []:
        if not _valid_tag(tag):
            _fail(
                args,
                f"invalid tag {tag!r}: tags are lowercase, start with a letter or "
                "digit, use only a-z 0-9 . _ -, and are at most 32 characters",
            )
            return True
    return False


def _format_tags(tags: list[str], paint: Painter) -> str:
    return paint(" ".join(f"#{escape_human_text(tag)}" for tag in tags), "cyan")


def _cmd_new(args: argparse.Namespace) -> int:
    # Forward only options the user actually passed, so anything left unset is
    # resolved from the user config file (and then the built-in defaults).
    kwargs: dict[str, Any] = {}
    if args.lifetime is not _UNSET:
        kwargs["lifetime"] = args.lifetime
    if args.parent is not _UNSET:
        kwargs["parent"] = args.parent
    if args.prefix is not _UNSET:
        kwargs["prefix"] = args.prefix
    if args.words is not _UNSET:
        kwargs["words"] = args.words
    if args.keep_on_restart is not _UNSET:
        kwargs["remove_on_restart"] = not args.keep_on_restart
    if args.keep_while_in_use is not _UNSET:
        kwargs["keep_while_in_use"] = args.keep_while_in_use
    if args.cleanup is not _UNSET:
        kwargs["cleanup"] = args.cleanup
    if args.until_sweep is not _UNSET:
        kwargs["cleanup"] = "next-sweep"
    if args.max_size is not _UNSET:
        kwargs["max_size"] = args.max_size
    if args.name_style is not _UNSET:
        kwargs["name_style"] = args.name_style
    if args.tag:
        kwargs["tags"] = args.tag
    if args.desc is not None:
        kwargs["description"] = args.desc

    try:
        directory = tempdir(**kwargs)
    except (TypeError, ValueError, OSError, LookupError) as error:
        # Bad user input or an unwritable registry: a message, not a traceback.
        _fail(args, error)
        return 2
    # The path goes to stdout so it can be captured in shell pipelines;
    # all diagnostics go to stderr via the logger.
    print(directory.path)
    return 0


def _cmd_sweep(args: argparse.Namespace) -> int:
    if _reject_invalid_filter_tags(args):
        return 2
    tags = args.tag or None
    if args.dry_run:
        decisions = plan_sweep(force=args.force, tags=tags)
        count = 0
        for decision in decisions:
            if decision.due:
                print(format_decision(decision))
                if decision.destructive_allowed:
                    count += 1
        logger.warning("would sweep %d director%s", count, "y" if count == 1 else "ies")
        return 0
    count = sweep(force=args.force, tags=tags)
    logger.warning("swept %d director%s", count, "y" if count == 1 else "ies")
    return 0


def _cmd_explain(args: argparse.Namespace) -> int:
    try:
        target = args.target if args.target is not None else _current_target_path()
        decision = explain(target)
    except LookupError as error:
        _fail(args, error)
        return 1
    now = time.time()
    entry = registered().get(str(decision.path), {})
    if args.json:
        emit_json(explain_payload(decision, entry, now))
        return 0
    paint = _painter(sys.stdout, args)
    ascii_only = not supports_unicode(sys.stdout)
    print(explain_trace(decision, entry, now, paint, ascii_only=ascii_only))
    return 0


def _cmd_doctor(args: argparse.Namespace) -> int:
    checks = run_doctor()
    if args.json:
        emit_json([check.__dict__ for check in checks])
    else:
        paint = _painter(sys.stdout, args)
        for check in checks:
            status = paint("ok", "green") if check.ok else paint("fail", "red")
            print(f"{status:<4} {check.name:<15} {escape_human_text(check.message)}")
            if check.hint:
                print(f"     hint: {escape_human_text(check.hint)}")
    return 0 if all(check.ok for check in checks) else 1


def _cmd_stats(args: argparse.Namespace) -> int:
    counters = StatsLedger().snapshot()
    tracked = sum(
        1 for entry in registered().values() if entry.get("state", "active") == "active"
    )
    if args.json:
        emit_json({**counters, "currently_tracked": tracked})
        return 0
    rows = [
        ("created", counters["created"]),
        ("automatically swept", counters["swept"]),
        ("kept", counters["kept"]),
        ("manually removed", counters["removed"]),
        ("currently tracked", tracked),
    ]
    label_width = max(len(label) for label, _ in rows)
    for label, value in rows:
        print(f"{label:<{label_width}}  {value}")
    return 0


def _cmd_completion(args: argparse.Namespace) -> int:
    try:
        print(completion_script(args.shell), end="")
    except ValueError as error:
        _fail(args, error)
        return 2
    return 0


def _cmd_menu(args: argparse.Namespace) -> int:
    return run_menu(lambda argv: main(argv))


def _cmd_list(args: argparse.Namespace) -> int:
    if _reject_invalid_filter_tags(args):
        return 2
    state = registered()
    now = time.time()
    current_boot = boot_time()
    current_boot_id = boot_session_id()

    rows = []
    for path_str, entry in state.items():
        path = Path(path_str)
        status = dir_status(entry, path, now, current_boot, current_boot_id)
        rows.append((path, entry, status))
    rows.sort(key=lambda row: (_created_at(row[1]), str(row[0])))
    if args.tag:
        rows = [row for row in rows if _matches_tags(row[1], args.tag)]

    if args.json:
        payload = []
        for path, entry, status in rows:
            expires_at = entry.get("expires_at")
            # Duration fields are integer seconds in the JSON contract, matching
            # explain --json (CONTRACT.md / RC4-1).
            remaining = (
                int(float(expires_at) - now) if isinstance(expires_at, (int, float)) else None
            )
            staging_value = entry.get("staging_path")
            staging_path = Path(staging_value) if isinstance(staging_value, str) else None
            payload.append(
                {
                    "path": str(path),
                    "name": path.name,
                    "status": status,
                    "lifecycle_state": entry.get("state", "active"),
                    "exists": None if status == "unavailable" else status != "missing",
                    "original_state": _path_state(path),
                    "staging_path": str(staging_path) if staging_path is not None else None,
                    "staging_state": (
                        _path_state(staging_path) if staging_path is not None else "missing"
                    ),
                    "created_at": entry.get("created_at"),
                    "expires_at": expires_at,
                    "remaining_seconds": remaining,
                    "remove_on_restart": bool(entry.get("remove_on_restart")),
                    "keep_while_in_use": bool(entry.get("keep_while_in_use")),
                    "tags": _entry_tags(entry),
                    "description": _entry_description(entry),
                }
            )
        emit_json(payload)
        return 0

    if not rows:
        logger.warning("no tracked directories")
        return 0

    paint = _painter(sys.stdout, args)
    icons = _ICONS if not args.plain and _supports_emoji(sys.stdout) else _ASCII_ICONS
    name_width = max(len(path.name) for path, _, _ in rows)
    notes = [_status_note(status, entry, now) for _, entry, status in rows]
    note_width = max(len(note) for note in notes)
    for (path, entry, status), note in zip(rows, notes, strict=True):
        styles = _STATUS_STYLES.get(status, ())
        padded_note = paint(f"{note:<{note_width}}", *styles)
        tags = _entry_tags(entry)
        suffix = f"  {_format_tags(tags, paint)}" if tags else ""
        print(
            f"{icons[status]} {escape_human_text(path.name):<{name_width}}  "
            f"{padded_note}  {escape_human_text(path)}{suffix}"
        )
        description = _entry_description(entry)
        if description:
            print(f"     {paint(escape_human_text(description), 'dim')}")
    return 0


def _measured_size(path: Path, status: str) -> tuple[int | None, bool]:
    """Best-effort size for `tree`, restricted to ephemdir-owned directories.

    Only directories ephemdir still owns and is actively tracking are measured.
    A `replaced`/`legacy`/`blocked`/`recovery`/`deleting` directory is shown but
    never walked: ephemdir has already classified it as not-ours or off-limits,
    so fd-walking it would widen the read surface to a foreign tree and hand a
    local racer a per-directory scan-budget DoS lever (RC3-1).
    """
    if status not in _MEASURABLE_STATUSES or _path_state(path) != "present":
        return None, True
    result = measure_tree(path)
    return result.bytes, result.complete


def _render_size(num_bytes: int | None, complete: bool, paint: Painter) -> str:
    if num_bytes is None:
        return paint("?", "dim")
    text = format_size(num_bytes)
    if not complete:
        text = "≥" + text  # >= : a measured lower bound
    return paint(text, "dim")


def _cmd_tree(args: argparse.Namespace) -> int:
    if _reject_invalid_filter_tags(args):
        return 2
    state = registered()
    now = time.time()
    current_boot = boot_time()
    current_boot_id = boot_session_id()

    items = []
    for path_str, entry in state.items():
        if not _matches_tags(entry, args.tag):
            continue
        path = Path(path_str)
        status = dir_status(entry, path, now, current_boot, current_boot_id)
        size_bytes, complete = _measured_size(path, status)
        items.append((path, status, size_bytes, complete, _entry_tags(entry)))

    if args.json:
        emit_json(
            [
                {
                    "path": str(path),
                    "name": path.name,
                    "parent": str(path.parent),
                    "status": status,
                    "size_bytes": size_bytes,
                    "size_complete": complete,
                    "tags": tags,
                }
                for path, status, size_bytes, complete, tags in items
            ]
        )
        return 0

    if not items:
        logger.warning("no tracked directories")
        return 0

    paint = _painter(sys.stdout, args)
    icons = _ICONS if not args.plain and _supports_emoji(sys.stdout) else _ASCII_ICONS
    unicode_ok = supports_unicode(sys.stdout)
    branch = "  └ " if unicode_ok else "  - "

    groups: dict[str, list[tuple[Path, str, int | None, bool, list[str]]]] = {}
    for path, status, size_bytes, complete, tags in items:
        groups.setdefault(str(path.parent), []).append(
            (path, status, size_bytes, complete, tags)
        )

    for parent in sorted(groups):
        children = sorted(groups[parent], key=lambda row: row[0].name)
        measured = [b for _, _, b, _, _ in children if isinstance(b, int)]
        any_unknown = any(b is None for _, _, b, _, _ in children)
        any_incomplete = any(not c for _, _, b, c, _ in children if isinstance(b, int))
        if not measured:
            # Every child is unmeasured (non-owned/absent): "?" is honester than 0 B.
            subtotal_render = _render_size(None, True, paint)
        else:
            # An unmeasured or budget-capped child makes the total a lower bound.
            complete = not any_unknown and not any_incomplete
            subtotal_render = _render_size(sum(measured), complete, paint)
        print(f"{escape_human_text(parent)}/  {subtotal_render}")
        for path, status, size_bytes, complete, tags in children:
            styles = _STATUS_STYLES.get(status, ())
            name = paint(escape_human_text(path.name), *styles)
            suffix = f"  {_format_tags(tags, paint)}" if tags else ""
            size = _render_size(size_bytes, complete, paint)
            print(f"{branch}{icons[status]} {name}  {size}{suffix}")
    return 0


def _cmd_path(args: argparse.Namespace) -> int:
    try:
        if args.target is not None:
            path = resolve(args.target)
        else:
            # Inside a tracked ephemdir directory, print its root. Outside one,
            # preserve the old fallback to the most recently created directory.
            # A present-but-invalid marker (_CurrentTargetMismatch) fails closed
            # and is NOT silently replaced by the latest fallback.
            try:
                path = _current_target_path()
            except _CurrentTargetNotFound:
                path = _latest_tracked()
    except LookupError as error:
        _fail(args, error)
        return 1
    if getattr(args, "json", False):
        emit_json({"path": str(path), "name": path.name})
        return 0
    print(path)
    return 0


def _latest_tracked() -> Path:
    """Return the most recently created tracked directory that still exists."""
    state = registered()
    live = [
        (_created_at(entry), key)
        for key, entry in state.items()
        if entry.get("state", "active") == "active" and _path_state(Path(key)) == "present"
    ]
    if not live:
        raise LookupError("no tracked directories")
    live.sort()
    return Path(live[-1][1])


def _cmd_keep(args: argparse.Namespace) -> int:
    if args.tag:
        return _cmd_keep_by_tag(args)
    try:
        target = args.target if args.target is not None else _current_target_path()
        path = keep(target)
    except LookupError as error:
        _fail(args, error)
        return 1
    logger.warning("kept %s -- it will not be auto-removed", path)
    print(path)
    return 0


def _cmd_keep_by_tag(args: argparse.Namespace) -> int:
    """Keep every active directory carrying all of the requested tags."""
    if _reject_invalid_filter_tags(args):
        return 2
    if args.target is not None:
        _fail(args, "give a directory or --tag, not both")
        return 2
    matches = [
        Path(key)
        for key, entry in registered().items()
        if entry.get("state", "active") == "active" and _matches_tags(entry, args.tag)
    ]
    kept = 0
    failed = 0
    for path in matches:
        try:
            result = keep(str(path))
        except (LookupError, OSError) as error:
            # One unkeepable directory (e.g. claimed by a concurrent sweep) must
            # not abort the batch; report it and continue, but the command still
            # fails so scripts see that not everything was kept (RC9-1).
            failed += 1
            _fail(args, error)
            continue
        kept += 1
        print(result)
    logger.warning("kept %d director%s", kept, "y" if kept == 1 else "ies")
    return 1 if failed else 0


def _cmd_last(args: argparse.Namespace) -> int:
    try:
        path = _latest_tracked()
    except LookupError as error:
        _fail(args, error)
        return 1
    if getattr(args, "json", False):
        emit_json({"path": str(path), "name": path.name})
        return 0
    print(path)
    return 0


def _looks_like_lifetime(text: str) -> bool:
    """Whether ``text`` parses as a lifetime (used to disambiguate `extend`)."""
    try:
        parse_lifetime(text)
    except (ValueError, TypeError):
        return False
    return True


def _cmd_extend(args: argparse.Namespace) -> int:
    # Grammar (target optional when inside a tracked ephemdir directory):
    #   extend <target> <lifetime>      extend <target> --forever
    #   extend <lifetime>               extend --forever
    arg1, arg2, forever = args.arg1, args.arg2, args.forever
    if arg2 is not None:
        if forever:
            _fail(args, "--forever cannot be combined with a lifetime")
            return 2
        target, lifetime_str = arg1, arg2
    elif arg1 is not None:
        if forever:
            if _looks_like_lifetime(arg1):
                # `extend --forever 2h` (a lone lifetime + --forever) is a
                # combination error, not a directory named "2h".
                _fail(args, "--forever cannot be combined with a lifetime")
                return 2
            target, lifetime_str = arg1, None            # extend <target> --forever
        elif _looks_like_lifetime(arg1):
            target, lifetime_str = None, arg1            # extend <lifetime> (current)
        else:
            _fail(args, "specify a lifetime (e.g. 2h) or --forever")
            return 2
    elif forever:
        target, lifetime_str = None, None                # extend --forever (current)
    else:
        _fail(args, "specify a lifetime (e.g. 2h) or --forever")
        return 2
    try:
        resolved = target if target is not None else _current_target_path()
        path = extend(resolved, None if forever else lifetime_str)
    except (LookupError, ValueError) as error:
        _fail(args, error)
        return 1
    if forever:
        logger.warning("extended %s -- no time limit (restart policy still applies)", path)
    else:
        logger.warning("extended %s by %s from now", path, lifetime_str)
    return 0


def _cmd_rm(args: argparse.Namespace) -> int:
    try:
        target = args.target if args.target is not None else _current_target_path()
        path = remove(target)
    except (LookupError, OSError) as error:
        _fail(args, error)
        return 1
    logger.warning("removed %s", path)
    return 0


def _cmd_recover(args: argparse.Namespace) -> int:
    action = "forget" if args.forget else "retry"
    try:
        path = recover(args.target, action=action)
    except (LookupError, OSError, ValueError) as error:
        _fail(args, error)
        return 1
    if action == "forget":
        logger.warning("forgot recovery entry for %s; no files were deleted", path)
    else:
        logger.warning("reconciled recovery entry for %s", path)
    return 0


def _cmd_prune(args: argparse.Namespace) -> int:
    count = prune()
    logger.warning("pruned %d missing entr%s", count, "y" if count == 1 else "ies")
    return 0


def _cmd_watch(args: argparse.Namespace) -> int:
    if args.interval < 1:
        _fail(args, "--interval must be >= 1 second")
        return 2
    logger.warning("watching; sweeping every %d seconds (Ctrl-C to stop)", args.interval)
    try:
        while True:
            sweep()
            time.sleep(args.interval)
    except KeyboardInterrupt:
        logger.warning("stopped")
    return 0


def _cmd_shell_init(args: argparse.Namespace) -> int:
    shell = args.shell or _detect_shell()
    print(_SHELL_SNIPPETS[shell], end="")
    return 0


def _detect_shell() -> str:
    """Guess the user's shell from the environment; default to bash."""
    if sys.platform == "win32":
        return "powershell"
    name = Path(os.environ.get("SHELL", "")).name
    return name if name in _SHELL_SNIPPETS else "bash"


def _cmd_install_service(args: argparse.Namespace) -> int:
    try:
        message = install_service(
            interval=args.interval, runtime_policy=args.runtime_policy
        )
    except (ServiceError, ValueError) as error:
        _fail(args, error)
        return 1
    logger.warning("%s", message)
    return 0


def _cmd_uninstall_service(args: argparse.Namespace) -> int:
    try:
        message = uninstall_service()
    except ServiceError as error:
        _fail(args, error)
        return 1
    logger.warning("%s", message)
    return 0


def build_parser() -> argparse.ArgumentParser:
    """Construct the argument parser for the ``ephemdir`` command."""
    parser = _NoAbbrevArgumentParser(
        prog="ephemdir",
        description="Create self-cleaning ephemeral directories.",
    )
    parser.add_argument("--version", action="version", version=f"%(prog)s {__version__}")
    parser.add_argument("-v", "--verbose", action="count", default=0, help="increase verbosity")
    parser.add_argument("-q", "--quiet", action="store_true", help="only report errors")
    parser.add_argument(
        "--color",
        choices=["auto", "always", "never"],
        default="auto",
        help="when to colourize output (default: auto; also honours NO_COLOR)",
    )

    sub = parser.add_subparsers(
        dest="command",
        required=True,
        parser_class=_NoAbbrevArgumentParser,
    )

    # Unset options default to the _UNSET sentinel so they can be resolved from
    # the user config file rather than always overriding it.
    new = sub.add_parser("new", help="create a new ephemeral directory")
    new.add_argument("-l", "--lifetime", default=_UNSET,
                     help='time to live, e.g. "2h", "30m", "1h30m" (default: until restart)')
    new.add_argument("--keep-on-restart", action="store_const", const=True, default=_UNSET,
                     help="do not remove the directory after a restart")
    new.add_argument("--keep-while-in-use", action="store_const", const=True, default=_UNSET,
                     help="do not delete while files are still open inside")
    new.add_argument("--cleanup", choices=["auto", "next-sweep"], default=_UNSET,
                     help="cleanup policy (auto or next-sweep)")
    new.add_argument("--until-sweep", action="store_const", const=True, default=_UNSET,
                     help="keep until an explicit full sweep")
    new.add_argument("--max-size", default=_UNSET,
                     help='remove once directory exceeds this size, e.g. "2GiB"')
    new.add_argument("--name-style", choices=["auto", "clean", "secure", "funny"], default=_UNSET,
                     help="generated name style")
    new.add_argument("-p", "--parent", default=_UNSET,
                     help="where to create the directory (default: current directory)")
    new.add_argument("--prefix", default=_UNSET, help="prefix for the generated name")
    new.add_argument("--words", type=int, default=_UNSET,
                     help="words in the generated name (default: 2)")
    new.add_argument("--tag", action="append", default=None, metavar="TAG",
                     help="add a tag for grouping/filtering (repeatable)")
    new.add_argument("--desc", "--description", dest="desc", default=None,
                     help="one-line description shown in list and explain")
    new.set_defaults(func=_cmd_new)

    list_cmd = sub.add_parser("list", help="show tracked directories with time left")
    list_cmd.add_argument("--json", action="store_true",
                          help="machine-readable output for scripting")
    list_cmd.add_argument("--plain", action="store_true",
                          help="use ASCII status tags instead of emoji")
    list_cmd.add_argument("--tag", action="append", default=None, metavar="TAG",
                          help="only show directories carrying this tag (repeatable; AND)")
    list_cmd.set_defaults(func=_cmd_list)

    path_cmd = sub.add_parser(
        "path", help="print the path of a tracked directory (by name, prefix or path)")
    path_cmd.add_argument("target", nargs="?", default=None,
                          help="directory name, unique prefix or path (default: the "
                               "current ephemdir directory, else the most recently created)")
    path_cmd.add_argument("--json", action="store_true", help="machine-readable output")
    path_cmd.set_defaults(func=_cmd_path)

    last_cmd = sub.add_parser(
        "last", help="print the most recently created tracked directory")
    last_cmd.add_argument("--json", action="store_true", help="machine-readable output")
    last_cmd.set_defaults(func=_cmd_last)

    stats_cmd = sub.add_parser(
        "stats", help="show lifetime usage counters")
    stats_cmd.add_argument("--json", action="store_true", help="machine-readable output")
    stats_cmd.set_defaults(func=_cmd_stats)

    tree_cmd = sub.add_parser(
        "tree", help="show tracked directories grouped by parent, with sizes")
    tree_cmd.add_argument("--json", action="store_true",
                          help="machine-readable output for scripting")
    tree_cmd.add_argument("--plain", action="store_true",
                          help="use ASCII status tags instead of emoji")
    tree_cmd.add_argument("--tag", action="append", default=None, metavar="TAG",
                          help="only show directories carrying this tag (repeatable; AND)")
    tree_cmd.set_defaults(func=_cmd_tree)

    keep_cmd = sub.add_parser(
        "keep", help="stop tracking a directory so it is never auto-removed")
    keep_cmd.add_argument("target", nargs="?", default=None,
                          help="directory name, unique prefix or path "
                               "(default: the current ephemdir directory)")
    keep_cmd.add_argument("--tag", action="append", default=None, metavar="TAG",
                          help="keep every directory carrying this tag (repeatable; AND)")
    keep_cmd.set_defaults(func=_cmd_keep)

    extend_cmd = sub.add_parser("extend", help="give a directory a fresh lifetime from now")
    extend_cmd.add_argument("arg1", nargs="?", default=None,
                            help="directory name/prefix/path, or a lifetime like "
                                 '"2h" to extend the current ephemdir directory')
    extend_cmd.add_argument("arg2", nargs="?", default=None,
                            help='new time to live when a target is given, e.g. "2h" or "1d"')
    extend_cmd.add_argument("--forever", action="store_true",
                            help="remove the time limit (restart policy still applies)")
    extend_cmd.set_defaults(func=_cmd_extend)

    rm = sub.add_parser("rm", help="remove a tracked directory now")
    rm.add_argument("target", nargs="?", default=None,
                    help="directory name, unique prefix or path "
                         "(default: the current ephemdir directory)")
    rm.set_defaults(func=_cmd_rm)

    sweep_cmd = sub.add_parser("sweep", help="remove directories that are due for cleanup")
    sweep_cmd.add_argument("--force", action="store_true",
                           help="remove every tracked directory regardless of policy")
    sweep_cmd.add_argument("--dry-run", action="store_true",
                           help="preview what would be removed without deleting anything")
    sweep_cmd.add_argument("--tag", action="append", default=None, metavar="TAG",
                           help="only sweep directories carrying this tag (repeatable; AND)")
    sweep_cmd.set_defaults(func=_cmd_sweep)

    explain_cmd = sub.add_parser("explain", help="explain cleanup state for a directory")
    explain_cmd.add_argument("target", nargs="?", default=None,
                             help="directory name, unique prefix or path "
                                  "(default: the current ephemdir directory)")
    explain_cmd.add_argument("--json", action="store_true", help="machine-readable output")
    explain_cmd.set_defaults(func=_cmd_explain)

    doctor_cmd = sub.add_parser("doctor", help="diagnose ephemdir safety prerequisites")
    doctor_cmd.add_argument("--json", action="store_true", help="machine-readable output")
    doctor_cmd.set_defaults(func=_cmd_doctor)

    completion = sub.add_parser("completion", help="print shell completion scripts")
    completion_sub = completion.add_subparsers(
        dest="completion_command",
        required=True,
        parser_class=_NoAbbrevArgumentParser,
    )
    for command, help_text in (
        ("install", "print a completion script; does not modify shell startup files"),
        ("show", "print a completion script"),
    ):
        completion_print = completion_sub.add_parser(
            command,
            help=help_text,
            description=help_text,
        )
        completion_print.add_argument(
            "shell",
            nargs="?",
            choices=["bash", "zsh", "fish", "powershell"],
            default=_detect_shell(),
            help="shell to target",
        )
        completion_print.set_defaults(func=_cmd_completion)

    menu = sub.add_parser("menu", help="open an interactive text menu")
    menu.set_defaults(func=_cmd_menu)

    prune_cmd = sub.add_parser(
        "prune", help="forget missing tracked directories explicitly")
    prune_cmd.set_defaults(func=_cmd_prune)

    recover_cmd = sub.add_parser(
        "recover", help="retry or forget an interrupted deletion journal entry")
    recover_cmd.add_argument("target", help="directory name, unique prefix or original path")
    recover_cmd.add_argument(
        "--forget",
        action="store_true",
        help="forget the registry entry without deleting original or staging files",
    )
    recover_cmd.set_defaults(func=_cmd_recover)

    watch = sub.add_parser("watch", help="sweep periodically in the foreground")
    watch.add_argument("--interval", type=int, default=600,
                       help="seconds between sweeps (default: 600)")
    watch.set_defaults(func=_cmd_watch)

    shell_init = sub.add_parser(
        "shell-init",
        help="print shell functions (ecd, enew) for cd-ing into directories")
    shell_init.add_argument("shell", nargs="?", choices=sorted(_SHELL_SNIPPETS),
                            default=None, help="shell to target (default: autodetect)")
    shell_init.set_defaults(func=_cmd_shell_init)

    install = sub.add_parser("install-service",
                             help="install a scheduled sweep service for this platform")
    install.add_argument("--interval", type=int, default=600,
                         help="seconds between sweeps (default: 600)")
    install.add_argument(
        "--runtime-policy",
        choices=["strict", "balanced"],
        default=None,
        help="runtime-trust policy for the service interpreter/package "
             "(default: balanced on macOS, strict elsewhere; "
             "env EPHEMDIR_SERVICE_RUNTIME_POLICY also applies)",
    )
    install.set_defaults(func=_cmd_install_service)

    uninstall = sub.add_parser("uninstall-service", help="remove the scheduled sweep service")
    uninstall.set_defaults(func=_cmd_uninstall_service)

    return parser


def main(argv: Sequence[str] | None = None) -> int:
    """Entry point for the ``ephemdir`` console script."""
    parser = build_parser()
    args = parser.parse_args(argv)
    _configure_logging(args.verbose, args.quiet)
    # ephemdir is POSIX-only. On other platforms refuse with one clear message
    # instead of leaking a confusing OSError from a failed data-dir create or a
    # misleading "no tracked directories". `doctor` still runs so the user can
    # see why.
    if not _is_posix_platform() and getattr(args, "command", None) != "doctor":
        logger.error(
            "ephemdir: %s: ephemdir is not supported on this platform; it requires "
            "a POSIX system (Linux or macOS). Run `ephemdir doctor` for details.",
            getattr(args, "command", "ephemdir"),
        )
        return 1
    try:
        exit_code: int = args.func(args)
    except TimeoutError as error:
        # The registry lock could not be acquired; nothing was modified.
        _fail(args, error)
        return 1
    except UnsafeRegistryError as error:
        # The registry is writable by other users and was left untouched: a
        # clear message, not a traceback, and definitely no destructive action.
        _fail(args, error)
        return 1
    except PermissionError as error:
        _fail(args, error)
        return 1
    except (RegistryFormatError, RegistryUnavailableError) as error:
        _fail(args, error)
        return 1
    return exit_code


if __name__ == "__main__":
    sys.exit(main())
