"""Command-line interface for ephemdir.

Exposes the library through a small ``ephemdir`` command:

* ``ephemdir new``    -- create a new ephemeral directory and print its path
* ``ephemdir sweep``  -- remove every directory that is due for cleanup
* ``ephemdir list``   -- show all tracked directories
* ``ephemdir watch``  -- run a foreground loop that sweeps periodically
"""

from __future__ import annotations

import argparse
import logging
import sys
import time
from datetime import datetime
from typing import Any, Sequence

from . import __version__
from ._service import install_service, uninstall_service
from .core import _UNSET, registered, sweep, tempdir

logger = logging.getLogger("ephemdir")


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
    logging.basicConfig(level=level, format="%(message)s", stream=sys.stderr)


def _format_timestamp(value: object) -> str:
    """Render a Unix timestamp as a readable local time, or ``never``."""
    if not isinstance(value, (int, float)):
        return "never"
    return datetime.fromtimestamp(value).strftime("%Y-%m-%d %H:%M:%S")


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

    directory = tempdir(**kwargs)
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
    if not state:
        logger.warning("no tracked directories")
        return 0
    for path, entry in sorted(state.items()):
        expires = _format_timestamp(entry.get("expires_at"))
        restart = "yes" if entry.get("remove_on_restart") else "no"
        print(f"{path}\n    expires: {expires}    remove-on-restart: {restart}")
    return 0


def _cmd_watch(args: argparse.Namespace) -> int:
    logger.warning("watching; sweeping every %d seconds (Ctrl-C to stop)", args.interval)
    try:
        while True:
            sweep()
            time.sleep(args.interval)
    except KeyboardInterrupt:
        logger.warning("stopped")
    return 0


def _cmd_install_service(args: argparse.Namespace) -> int:
    message = install_service(interval=args.interval)
    logger.warning("%s", message)
    return 0


def _cmd_uninstall_service(args: argparse.Namespace) -> int:
    logger.warning("%s", uninstall_service())
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

    sweep_cmd = sub.add_parser("sweep", help="remove directories that are due for cleanup")
    sweep_cmd.add_argument("--force", action="store_true",
                           help="remove every tracked directory regardless of policy")
    sweep_cmd.set_defaults(func=_cmd_sweep)

    list_cmd = sub.add_parser("list", help="show all tracked directories")
    list_cmd.set_defaults(func=_cmd_list)

    watch = sub.add_parser("watch", help="sweep periodically in the foreground")
    watch.add_argument("--interval", type=int, default=600,
                       help="seconds between sweeps (default: 600)")
    watch.set_defaults(func=_cmd_watch)

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
    exit_code: int = args.func(args)
    return exit_code


if __name__ == "__main__":
    sys.exit(main())
