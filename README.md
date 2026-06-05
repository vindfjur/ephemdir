# ephemdir

[![PyPI version](https://img.shields.io/pypi/v/ephemdir.svg)](https://pypi.org/project/ephemdir/)
[![Python versions](https://img.shields.io/pypi/pyversions/ephemdir.svg)](https://pypi.org/project/ephemdir/)
[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](LICENSE)

**ephemdir** (*ephemeral directory*) creates temporary directories that clean
themselves up — automatically removed once their lifetime expires or after the
machine restarts. Names are playful two-word slugs like `brave-otter` or
`nimble-marmot` instead of dull ones like `tmp_data`.

Works on **Linux, macOS and Windows** with a single small dependency
([`coolname`](https://pypi.org/project/coolname/)).

## Installation

```bash
pip install ephemdir
```

## Quick start

```python
from ephemdir import tempdir

# Lives until the next system restart (the default).
work = tempdir()
print(work)                       # -> /current/dir/brave-otter
(work.path / "data.txt").write_text("hello")

# Also expires after a given lifetime.
cache = tempdir(lifetime="2h")    # "30m", "1h30m", 3600, timedelta(hours=2) ...

# Survive restarts and rely only on the lifetime.
keep = tempdir(lifetime="7d", remove_on_restart=False)

# Use as a context manager: removed automatically when the block exits.
with tempdir() as scratch:
    (scratch / "tmp.bin").write_bytes(b"...")
```

### `tempdir()` parameters

| Parameter           | Default       | Description                                                       |
| ------------------- | ------------- | ----------------------------------------------------------------- |
| `lifetime`          | `None`        | Time to live: seconds, `timedelta`, or `"2h"`/`"1h30m"`. `None` = until restart. |
| `remove_on_restart` | `True`        | Remove the directory after the machine reboots.                   |
| `parent`            | current dir   | Where to create the directory.                                    |
| `prefix`            | `""`          | Prefix prepended to the generated name.                           |
| `words`             | `2`           | Number of words in the generated name.                            |

### Managing a directory

```python
d = tempdir()
d.path          # pathlib.Path to the directory
d.created_at    # creation timestamp
d.expires_at    # expiry timestamp or None
d.remove()      # delete now and stop tracking
d.keep()        # stop tracking but keep on disk (becomes permanent)
```

## How cleanup works

ephemdir records each directory in a small JSON registry in your per-user data
directory. Cleanup happens in two ways:

1. **Lazily** — every call to `tempdir()` first sweeps anything already due.
2. **On demand** — run `ephemdir sweep` from the command line.

To clean up reliably over time (and right after a reboot), schedule the sweep:

* **Linux (cron):** `*/10 * * * * ephemdir sweep`
* **macOS (launchd):** load a `LaunchAgent` that runs `ephemdir sweep` with
  `RunAtLoad` and a `StartInterval`. A template lives in
  [`packaging/`](packaging/).
* **Windows (Task Scheduler):** create a task that runs `ephemdir sweep` at
  logon and on a repeating interval.

You can also keep a foreground watcher running:

```bash
ephemdir watch --interval 600
```

## Command-line interface

```bash
ephemdir new                     # create a directory, print its path
ephemdir new --lifetime 2h       # with a lifetime
ephemdir new --keep-on-restart   # do not remove on restart
ephemdir list                    # show tracked directories
ephemdir sweep                   # remove everything due now
ephemdir sweep --force           # remove every tracked directory
ephemdir watch                   # sweep periodically in the foreground
```

Add `-v` for more output or `-q` to stay quiet. The `new` command prints the
path to **stdout** and all diagnostics to **stderr**, so it composes cleanly:

```bash
cd "$(ephemdir new)"
```

## Development

```bash
pip install -e ".[dev]"
pytest
```

## License

MIT © vindfjur
