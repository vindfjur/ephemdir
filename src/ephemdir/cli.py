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
from typing import Optional, Sequence

from . import __version__
from .core import registered, sweep, tempdir

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


def _format_timestamp(value: Optional[float]) -> str:
    """Render a Unix timestamp as a readable local time, or a dash."""
    if value is None:
        return "never"
    return datetime.fromtimestamp(value).strftime("%Y-%m-%d %H:%M:%S")


def _cmd_new(args: argparse.Namespace) -> int:
    directory = tempdir(
        lifetime=args.lifetime,
        remove_on_restart=not args.keep_on_restart,
        parent=args.parent,
        prefix=args.prefix,
        words=args.words,
    )
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

    new = sub.add_parser("new", help="create a new ephemeral directory")
    new.add_argument("-l", "--lifetime", default=None,
                     help='time to live, e.g. "2h", "30m", "1h30m" (default: until restart)')
    new.add_argument("--keep-on-restart", action="store_true",
                     help="do not remove the directory after a restart")
    new.add_argument("-p", "--parent", default=None,
                     help="where to create the directory (default: current directory)")
    new.add_argument("--prefix", default="", help="prefix for the generated name")
    new.add_argument("--words", type=int, default=2, help="words in the generated name (default: 2)")
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

    return parser


def main(argv: Optional[Sequence[str]] = None) -> int:
    """Entry point for the ``ephemdir`` console script."""
    parser = build_parser()
    args = parser.parse_args(argv)
    _configure_logging(args.verbose, args.quiet)
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
