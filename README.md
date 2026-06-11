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

# Do not delete while a process still has files open inside (Linux/macOS).
busy = tempdir(lifetime="1h", keep_while_in_use=True)

# Use as a context manager: removed automatically when the block exits.
with tempdir() as scratch:
    (scratch / "tmp.bin").write_bytes(b"...")
```

### `tempdir()` parameters

| Parameter           | Default       | Description                                                       |
| ------------------- | ------------- | ----------------------------------------------------------------- |
| `lifetime`          | `None`        | Time to live: seconds, `timedelta`, or `"2h"`/`"1h30m"`. `None` = until restart. |
| `remove_on_restart` | `True`        | Remove the directory after the machine reboots.                   |
| `keep_while_in_use` | `False`       | Defer deletion while files are still open inside (Linux/macOS).   |
| `parent`            | current dir   | Where to create the directory.                                    |
| `prefix`            | `""`          | Prefix prepended to the generated name.                           |
| `words`             | `2`           | Number of words in the generated name.                            |

Any option left unset falls back to the [user config file](#configuration), then
to the built-in default shown above.

### Managing a directory

```python
d = tempdir()
d.path          # pathlib.Path to the directory
d.created_at    # creation timestamp
d.expires_at    # expiry timestamp or None
d.remove()      # delete now and stop tracking
d.keep()        # stop tracking but keep on disk (becomes permanent)
d.extend("2h")  # fresh lifetime counted from now (None = no time limit)
```

You can also manage any tracked directory later — by full path, exact name or
a unique prefix of the name:

```python
from ephemdir import keep, extend, remove, resolve, prune

keep("brave-otter")          # liked it? make it permanent
extend("brave-otter", "2h")  # give it two more hours from now
remove("brave-otter")        # delete it right away
resolve("bra")               # -> Path to the matching tracked directory
prune()                      # forget entries whose dirs were deleted manually
```

## How cleanup works

ephemdir records each directory in a small JSON registry in your per-user data
directory. Cleanup happens in two ways:

1. **Lazily** — every call to `tempdir()` first sweeps anything already due.
2. **On demand** — run `ephemdir sweep` from the command line.

To clean up reliably over time (and right after a reboot), install a scheduled
sweep service for your platform — one command, no manual unit files:

```bash
ephemdir install-service --interval 600   # launchd / systemd / Task Scheduler
ephemdir uninstall-service
```

Prefer to wire it up yourself? The equivalents are:

* **Linux (cron):** `*/10 * * * * ephemdir sweep`
* **macOS (launchd):** a `LaunchAgent` running `ephemdir sweep`; template in
  [`packaging/`](packaging/).
* **Windows (Task Scheduler):** a task running `ephemdir sweep` at logon and on
  a repeating interval.

You can also keep a foreground watcher running:

```bash
ephemdir watch --interval 600
```

## Configuration

Set per-user defaults in a `config.toml` file in your config directory
(`~/.config/ephemdir/` on Linux, `~/Library/Application Support/ephemdir/` on
macOS, `%APPDATA%\ephemdir\` on Windows). Every key is optional:

```toml
lifetime = "6h"
remove_on_restart = true
keep_while_in_use = true
prefix = "scratch-"
words = 2
```

These apply to any option you do not pass explicitly to `tempdir()` or the
`ephemdir new` command. Reading the file uses the standard-library `tomllib`
(Python 3.11+) or `tomli` on older versions.

The environment variables `EPHEMDIR_DATA_DIR` (registry location) and
`EPHEMDIR_CONFIG_DIR` (config location) override the platform defaults —
handy for sandboxes, tests and dotfile-managed setups.

## Command-line interface

```bash
ephemdir new                     # create a directory, print its path
ephemdir new --lifetime 2h       # with a lifetime
ephemdir new --keep-on-restart   # do not remove on restart
ephemdir list                    # tracked directories, status and time left
ephemdir path brave-otter        # print a tracked directory's path
ephemdir keep brave-otter        # liked it? stop tracking, keep forever
ephemdir extend brave-otter 2h   # fresh lifetime from now (--forever: no limit)
ephemdir rm brave-otter          # remove a tracked directory now
ephemdir sweep                   # remove everything due now
ephemdir sweep --force           # remove every tracked directory
ephemdir prune                   # forget entries deleted outside ephemdir
ephemdir watch                   # sweep periodically in the foreground
ephemdir install-service         # schedule sweeps via the OS scheduler
ephemdir uninstall-service       # remove the scheduled service
```

Every command that takes a directory accepts a full path, the exact name or a
unique prefix (`bra` for `brave-otter`). Add `-v` for more output or `-q` to
stay quiet.

### Listing with time left

`ephemdir list` shows each directory's status at a glance:

```
🟢 spotted-armadillo   1h 59m left              ~/work/spotted-armadillo
🟡 vermilion-mackerel  4m 59s left              ~/work/vermilion-mackerel
🔄 caped-dodo          until restart            ~/work/caped-dodo
🔴 furious-caiman      expired 5m ago           ~/work/furious-caiman
👻 lucky-yak           gone (deleted manually)  ~/work/lucky-yak
```

`🟢` counting down · `🟡` less than 15 minutes left · `🔴` due on the next
sweep · `🔄` until restart · `📌` no auto-cleanup · `👻` deleted outside
ephemdir (the entry is dropped automatically). Terminals without emoji support
fall back to ASCII tags; `--plain` forces them, `--json` prints
machine-readable output for scripting.

### Jumping into directories (`ecd` / `enew`)

A subprocess cannot change your shell's working directory, so ephemdir ships
shell functions instead (the same trick `zoxide` and `nvm` use). Add one line
to your shell rc file:

```bash
# ~/.bashrc or ~/.zshrc
eval "$(ephemdir shell-init)"

# ~/.config/fish/config.fish
ephemdir shell-init fish | source

# PowerShell $PROFILE
Invoke-Expression (& ephemdir shell-init powershell | Out-String)
```

Then:

```bash
enew -l 2h        # create a directory and cd into it
ecd brave-otter   # cd into a tracked directory by name or prefix
ecd               # cd into the most recently created one
```

The `new` and `path` commands print the path to **stdout** and all diagnostics
to **stderr**, so they also compose cleanly by hand: `cd "$(ephemdir new)"`.

## Development

```bash
pip install -e ".[dev]"
pytest
```

## License

MIT © vindfjur
