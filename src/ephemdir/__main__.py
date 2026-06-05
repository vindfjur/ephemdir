"""Allow running the CLI via ``python -m ephemdir``."""

from .cli import main

if __name__ == "__main__":
    raise SystemExit(main())
