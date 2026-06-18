"""Small interactive text menu built on the public command handlers."""

from __future__ import annotations

import sys
from collections.abc import Callable

__all__ = ["run_menu"]


def run_menu(
    dispatch: Callable[[list[str]], int],
    *,
    input_func: Callable[[str], str] = input,
    print_func: Callable[..., None] = print,
) -> int:
    if input_func is input and (not sys.stdin.isatty() or not sys.stdout.isatty()):
        print_func("ephemdir menu requires an interactive terminal")
        return 2
    while True:
        print_func("ephemdir menu")
        print_func("1. List tracked directories")
        print_func("2. Create directory")
        print_func("3. Sweep due directories")
        print_func("4. Doctor")
        print_func("q. Quit")
        try:
            choice = input_func("> ").strip().lower()
        except (EOFError, KeyboardInterrupt):
            print_func("")
            return 0
        if choice in {"q", "quit", "exit"}:
            return 0
        if choice == "1":
            dispatch(["list"])
        elif choice == "2":
            dispatch(["new"])
        elif choice == "3":
            dispatch(["sweep", "--dry-run"])
            try:
                confirm = input_func("Sweep now? [y/N] ").strip().lower()
            except (EOFError, KeyboardInterrupt):
                print_func("")
                return 0
            if confirm == "y":
                dispatch(["sweep"])
        elif choice == "4":
            dispatch(["doctor"])
        else:
            print_func("unknown choice")
