"""Command-line interface for ephemdir.

Exposes the library through a small ``ephemdir`` command:

* ``ephemdir new``        -- create a new ephemeral directory and print its path
* ``ephemdir list``       -- show tracked directories with status and time left
* ``ephemdir path``       -- print the path of a tracked directory by name
* ``ephemdir keep``       -- stop tracking a directory (make it permanent)
* ``ephemdir extend``     -- give a directory a fresh lifetime
* ``ephemdir rm``         -- remove a tracked directory now
* ``ephemdir sweep``      -- remove every directory that is due for cleanup
* ``ephemdir prune``      -- forget entries whose directories were deleted manually
* ``ephemdir watch``      -- run a foreground loop that sweeps periodically
* ``ephemdir shell-init`` -- print shell functions (``ecd``, ``enew``) to eval
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import sys
import time
from collections.abc import Sequence
from datetime import datetime
from pathlib import Path
from typing import Any, TextIO

from . import __version__
from ._platform import boot_session_id, boot_time
from ._registry import Entry, UnsafeRegistryError
from ._service import ServiceError, install_service, uninstall_service
from .core import (
    _UNSET,
    _path_state,
    dir_status,
    extend,
    keep,
    prune,
    recover,
    registered,
    remove,
    resolve,
    sweep,
    tempdir,
)

logger = logging.getLogger("ephemdir")

# Status icons for `ephemdir list`, with an ASCII fallback for terminals whose
# encoding cannot represent emoji (e.g. some Windows consoles).
_ICONS = {
    "active": "🟢",
    "expiring": "🟡",
    "expired": "🔴",
    "until-restart": "🔄",
    "kept": "📌",
    "missing": "👻",
    "replaced": "⚠️",
    "legacy": "⚪",
    "deleting": "🗑️",
    "recovery": "🚧",
    "unavailable": "❓",
}
_ASCII_ICONS = {
    "active": "[ok]  ",
    "expiring": "[soon]",
    "expired": "[due] ",
    "until-restart": "[boot]",
    "kept": "[pin] ",
    "missing": "[gone]",
    "replaced": "[warn]",
    "legacy": "[old] ",
    "deleting": "[del] ",
    "recovery": "[rec] ",
    "unavailable": "[n/a] ",
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


def _format_timestamp(value: object) -> str:
    """Render a Unix timestamp as a readable local time, or ``never``."""
    if not isinstance(value, (int, float)):
        return "never"
    return datetime.fromtimestamp(value).strftime("%Y-%m-%d %H:%M:%S")


def _format_duration(seconds: float) -> str:
    """Humanize a duration: ``"1d 4h"``, ``"1h 23m"``, ``"5m 12s"``, ``"42s"``.

    At most the two largest non-zero units are shown.
    """
    total = max(0, int(seconds))
    components = [("d", total // 86400), ("h", total % 86400 // 3600),
                  ("m", total % 3600 // 60), ("s", total % 60)]
    nonzero = [(unit, value) for unit, value in components if value]
    if not nonzero:
        return "0s"
    return " ".join(f"{value}{unit}" for unit, value in nonzero[:2])


def _status_note(status: str, entry: Entry, now: float) -> str:
    """One short human phrase describing what happens to the directory next."""
    if status == "missing":
        return "gone (deleted manually)"
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
    if status == "kept":
        return "no auto-cleanup"
    if status == "until-restart":
        return "until restart"
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

    try:
        directory = tempdir(**kwargs)
    except (TypeError, ValueError, OSError, LookupError) as error:
        # Bad user input or an unwritable registry: a message, not a traceback.
        logger.error("%s", error)
        return 2
    # The path goes to stdout so it can be captured in shell pipelines;
    # all diagnostics go to stderr via the logger.
    print(directory.path)
    return 0


def _cmd_sweep(args: argparse.Namespace) -> int:
    count = sweep(force=args.force)
    logger.warning("swept %d director%s", count, "y" if count == 1 else "ies")
    return 0


def _cmd_list(args: argparse.Namespace) -> int:
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

    if args.json:
        payload = []
        for path, entry, status in rows:
            expires_at = entry.get("expires_at")
            remaining = (
                float(expires_at) - now if isinstance(expires_at, (int, float)) else None
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
                }
            )
        print(json.dumps(payload, indent=2))
        return 0

    if not rows:
        logger.warning("no tracked directories")
        return 0

    icons = _ICONS if not args.plain and _supports_emoji(sys.stdout) else _ASCII_ICONS
    name_width = max(len(path.name) for path, _, _ in rows)
    notes = [_status_note(status, entry, now) for _, entry, status in rows]
    note_width = max(len(note) for note in notes)
    for (path, _, status), note in zip(rows, notes, strict=True):
        print(f"{icons[status]} {path.name:<{name_width}}  {note:<{note_width}}  {path}")
    return 0


def _cmd_path(args: argparse.Namespace) -> int:
    try:
        if args.target is None:
            path = _latest_tracked()
        else:
            path = resolve(args.target)
    except LookupError as error:
        logger.error("%s", error)
        return 1
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
    try:
        path = keep(args.target)
    except LookupError as error:
        logger.error("%s", error)
        return 1
    logger.warning("kept %s -- it will not be auto-removed", path)
    print(path)
    return 0


def _cmd_extend(args: argparse.Namespace) -> int:
    if args.lifetime is None and not args.forever:
        logger.error("specify a lifetime (e.g. 2h) or --forever")
        return 2
    if args.lifetime is not None and args.forever:
        logger.error("--forever cannot be combined with a lifetime")
        return 2
    try:
        path = extend(args.target, None if args.forever else args.lifetime)
    except (LookupError, ValueError) as error:
        logger.error("%s", error)
        return 1
    if args.forever:
        logger.warning("extended %s -- no time limit (restart policy still applies)", path)
    else:
        logger.warning("extended %s by %s from now", path, args.lifetime)
    return 0


def _cmd_rm(args: argparse.Namespace) -> int:
    try:
        path = remove(args.target)
    except (LookupError, OSError) as error:
        logger.error("%s", error)
        return 1
    logger.warning("removed %s", path)
    return 0


def _cmd_recover(args: argparse.Namespace) -> int:
    action = "forget" if args.forget else "retry"
    try:
        path = recover(args.target, action=action)
    except (LookupError, OSError, ValueError) as error:
        logger.error("%s", error)
        return 1
    if action == "forget":
        logger.warning("forgot recovery entry for %s; no files were deleted", path)
    else:
        logger.warning("reconciled recovery entry for %s", path)
    return 0


def _cmd_prune(args: argparse.Namespace) -> int:
    count = prune()
    logger.warning("pruned %d stale entr%s", count, "y" if count == 1 else "ies")
    return 0


def _cmd_watch(args: argparse.Namespace) -> int:
    if args.interval < 1:
        logger.error("--interval must be >= 1 second")
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
        message = install_service(interval=args.interval)
    except (ServiceError, ValueError) as error:
        logger.error("%s", error)
        return 1
    logger.warning("%s", message)
    return 0


def _cmd_uninstall_service(args: argparse.Namespace) -> int:
    try:
        message = uninstall_service()
    except ServiceError as error:
        logger.error("%s", error)
        return 1
    logger.warning("%s", message)
    return 0


def build_parser() -> argparse.ArgumentParser:
    """Construct the argument parser for the ``ephemdir`` command."""
    parser = argparse.ArgumentParser(
        prog="ephemdir",
        description="Create self-cleaning ephemeral directories.",
    )
    parser.add_argument("--version", action="version", version=f"%(prog)s {__version__}")
    parser.add_argument("-v", "--verbose", action="count", default=0, help="increase verbosity")
    parser.add_argument("-q", "--quiet", action="store_true", help="only report errors")

    sub = parser.add_subparsers(dest="command", required=True)

    # Unset options default to the _UNSET sentinel so they can be resolved from
    # the user config file rather than always overriding it.
    new = sub.add_parser("new", help="create a new ephemeral directory")
    new.add_argument("-l", "--lifetime", default=_UNSET,
                     help='time to live, e.g. "2h", "30m", "1h30m" (default: until restart)')
    new.add_argument("--keep-on-restart", action="store_const", const=True, default=_UNSET,
                     help="do not remove the directory after a restart")
    new.add_argument("--keep-while-in-use", action="store_const", const=True, default=_UNSET,
                     help="do not delete while files are still open inside")
    new.add_argument("-p", "--parent", default=_UNSET,
                     help="where to create the directory (default: current directory)")
    new.add_argument("--prefix", default=_UNSET, help="prefix for the generated name")
    new.add_argument("--words", type=int, default=_UNSET,
                     help="words in the generated name (default: 2)")
    new.set_defaults(func=_cmd_new)

    list_cmd = sub.add_parser("list", help="show tracked directories with time left")
    list_cmd.add_argument("--json", action="store_true",
                          help="machine-readable output for scripting")
    list_cmd.add_argument("--plain", action="store_true",
                          help="use ASCII status tags instead of emoji")
    list_cmd.set_defaults(func=_cmd_list)

    path_cmd = sub.add_parser(
        "path", help="print the path of a tracked directory (by name, prefix or path)")
    path_cmd.add_argument("target", nargs="?", default=None,
                          help="directory name, unique prefix or path "
                               "(default: most recently created)")
    path_cmd.set_defaults(func=_cmd_path)

    keep_cmd = sub.add_parser(
        "keep", help="stop tracking a directory so it is never auto-removed")
    keep_cmd.add_argument("target", help="directory name, unique prefix or path")
    keep_cmd.set_defaults(func=_cmd_keep)

    extend_cmd = sub.add_parser("extend", help="give a directory a fresh lifetime from now")
    extend_cmd.add_argument("target", help="directory name, unique prefix or path")
    extend_cmd.add_argument("lifetime", nargs="?", default=None,
                            help='new time to live, e.g. "2h" or "1d"')
    extend_cmd.add_argument("--forever", action="store_true",
                            help="remove the time limit (restart policy still applies)")
    extend_cmd.set_defaults(func=_cmd_extend)

    rm = sub.add_parser("rm", help="remove a tracked directory now")
    rm.add_argument("target", help="directory name, unique prefix or path")
    rm.set_defaults(func=_cmd_rm)

    sweep_cmd = sub.add_parser("sweep", help="remove directories that are due for cleanup")
    sweep_cmd.add_argument("--force", action="store_true",
                           help="remove every tracked directory regardless of policy")
    sweep_cmd.set_defaults(func=_cmd_sweep)

    prune_cmd = sub.add_parser(
        "prune", help="forget entries whose directories were deleted manually")
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
    install.set_defaults(func=_cmd_install_service)

    uninstall = sub.add_parser("uninstall-service", help="remove the scheduled sweep service")
    uninstall.set_defaults(func=_cmd_uninstall_service)

    return parser


def main(argv: Sequence[str] | None = None) -> int:
    """Entry point for the ``ephemdir`` console script."""
    parser = build_parser()
    args = parser.parse_args(argv)
    _configure_logging(args.verbose, args.quiet)
    try:
        exit_code: int = args.func(args)
    except TimeoutError as error:
        # The registry lock could not be acquired; nothing was modified.
        logger.error("%s", error)
        return 1
    except UnsafeRegistryError as error:
        # The registry is writable by other users and was left untouched: a
        # clear message, not a traceback, and definitely no destructive action.
        logger.error("%s", error)
        return 1
    return exit_code


if __name__ == "__main__":
    sys.exit(main())
