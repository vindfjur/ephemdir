# ephemdir

[![PyPI version](https://img.shields.io/pypi/v/ephemdir.svg)](https://pypi.org/project/ephemdir/)
[![Python versions](https://img.shields.io/pypi/pyversions/ephemdir.svg)](https://pypi.org/project/ephemdir/)
[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](LICENSE)

**ephemdir** (*ephemeral directory*) creates temporary directories that clean
themselves up — automatically removed once their lifetime expires or after the
machine restarts. Names are playful two-word slugs with a random token, like
`brave-otter-a81f42c9d047315b`, instead of dull ones like `tmp_data` — and you
can address them by any unique prefix (`brave` or `brave-otter`).

Supports **Python 3.10+ on Linux and macOS** with safe fd-relative cleanup.
Windows is intentionally unsupported until Python or a dedicated backend can
provide handle-bound recursive deletion; `tempdir()` fails before creating
anything on unsupported platforms. The package has no third-party runtime
dependencies on Python 3.11+; Python 3.10 uses `tomli` only for TOML
configuration parsing.

v0.5.0 is a POSIX-only hardening release. It adds next-sweep cleanup,
max-size cleanup thresholds, clean/secure naming policy, dry-run sweep
previews, stricter registry validation, improved scheduler safety and better
CLI diagnostics. Windows remains unsupported.

## Installation

> [!IMPORTANT]
> Windows is **not supported in ephemdir 0.5.0**. The package may install on
> Windows, but `tempdir()` and `ephemdir new` fail before creating anything
> because Python does not expose the safe handle-bound recursive deletion
> primitives ephemdir requires. Supported platforms are Linux and macOS.

### macOS (Homebrew)

```bash
brew install vindfjur/tap/ephemdir
```

Or tap once and install by name:

```bash
brew tap vindfjur/tap
brew install ephemdir
```

### Linux

For command-line use, `pipx` keeps ephemdir isolated from system packages:

```bash
pipx install ephemdir
```

Or install from PyPI with pip:

```bash
pip install ephemdir
```

### Python package

Add ephemdir to your project dependencies, or install it into the current
environment:

```bash
python -m pip install ephemdir
```

## Quick start

```python
from ephemdir import tempdir

# Lives until the next system restart (the default).
work = tempdir()
print(work)                       # -> /current/dir/brave-otter-a81f42c9d047315b
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
| `cleanup`           | `"auto"`      | Use `"next-sweep"` to keep until an explicit full sweep.          |
| `max_size`          | `None`        | Optional byte or human string size limit such as `"2GiB"`.        |
| `name_style`        | `"secure"`    | `secure` keeps a random suffix; `clean` requires a private parent. |
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

Use `ephemdir sweep --dry-run` to preview due directories without destructive
actions, and `ephemdir explain <name>` to see the reasons and blockers for one
tracked directory.

### Safety guarantees

ephemdir never auto-deletes a directory it cannot prove it created:

* Each directory contains a hidden **`.ephemdir` marker file** with a random
  id that is also stored in the registry (plus the inode on Unix). Nothing is
  auto-deleted unless the marker matches — if you delete a directory manually
  and something else later appears at the same path, ephemdir leaves it alone
  (shown as ⚠️ `replaced` in `ephemdir list`). Directories registered by
  ephemdir ≤ 0.3 have no marker, so they are never auto-removed either: they
  show as ⚪ `legacy` until you `ephemdir rm` or `ephemdir keep` them.
* Deletion is **journaled and claimed under locks**: the registry records the
  intent durably before the directory is renamed away and commits afterwards,
  every step re-verifies the entry and the marker, and each directory has its
  own OS-level deletion lock. A concurrent `keep` or `extend` always wins
  against a running sweep (`keep()` inside a `with tempdir()` block is
  honoured), two overlapping sweeps never delete the same tree twice, and a
  crash at any point is reconciled by the next sweep's recovery pass under the
  same deletion lock. A half-deleted tree stays at its private staging path and
  is retried; it is never renamed over a new object at the original path.
  Ambiguous cases are parked as `recovery` and can be handled with
  `ephemdir recover`. Nothing mid-deletion becomes untracked.
* On POSIX, recursive staging deletion is bound to the verified directory
  object: ephemdir opens the staging tree with `O_DIRECTORY | O_NOFOLLOW`,
  checks `fstat()` against the claimed inode/marker and removes contents via
  `dir_fd` operations. If the staging pathname is replaced after verification,
  the replacement is left alone and the entry moves to `recovery`.
* Linux cleanup refuses to cross mount boundaries by comparing mount ids from
  opened directory fds. It uses `statx(STATX_MNT_ID)` when available and falls
  back to `/proc/self/fdinfo` or `/proc/self/mountinfo` on older kernels. Mount
  capability is checked before claim, then rechecked before recursion.
* Directory creation and claim/delete require a trusted parent chain: existing
  components are opened without following symlinks and must be owned by you or
  root. The final parent may be owner-controlled, or a root-owned sticky shared
  directory like `/tmp`; foreign-owned sticky parents and group/world-writable
  parents without sticky bit are refused before cleanup side effects or rename.
* Platforms without safe fd-bound recursive deletion do not fall back to
  pathname-based `rmtree`. `tempdir()` refuses to create new managed directories,
  and cleanup capability is rechecked before claim so existing entries remain untouched.
* A **deletion guard** refuses to touch the filesystem root, your home
  directory or ephemdir's own data — even with `sweep --force` and even if the
  registry file was corrupted or edited by hand. A malformed registry entry
  invalidates the whole registry for that command; transactions copy the
  inspected bytes to `registry.json.corrupt-*`, leave the active registry path
  in place as a blocking object and abort instead of saving a filtered or empty
  state.
* Directories are created owner-only (`0700`), like `tempfile.mkdtemp`.
* Optional `lsof` and macOS `sysctl` probes ignore inherited `PATH`, resolve
  helpers only from trusted system directories and run with a minimal environment.
  If `keep_while_in_use` is enabled and `lsof` cannot run, the sweep keeps the
  directory and warns instead of deleting.
* Generated names end with a **64-bit random token**, so another local user
  cannot pre-create the whole name space in a shared sticky directory like
  `/tmp` and block creation.
* The scheduled service runs **`python -I -m ephemdir`** from `/` with a
  scrubbed environment, and `install-service` verifies both that isolated
  mode resolves `ephemdir` to the installed package and that every directory
  on the way to the systemd/launchd unit files is owned and writable only by
  you (service files are then written `dir_fd`-relative to the verified
  directory). The installed job also pins the effective absolute
  `EPHEMDIR_DATA_DIR` and `EPHEMDIR_CONFIG_DIR`, so future scheduled sweeps use
  the same registry/config target as the shell that installed the service.
* The config file is honoured only when it is yours: a `config.toml` owned by
  another user or writable by group/others is ignored with a warning.

When an interrupted deletion is genuinely ambiguous, inspect it with
`ephemdir list --json`. After resolving the filesystem situation, retry it with
`ephemdir recover <name>`. `ephemdir recover <name> --forget` removes only the
registry entry and never deletes either the original or staging path.

To clean up reliably over time (and right after a reboot), install a scheduled
sweep service for your platform — one command, no manual unit files:

```bash
ephemdir install-service --interval 600   # launchd / systemd
ephemdir uninstall-service
```

`install-service` validates the persistent runtime before writing anything:
every component of the interpreter and package paths must be owned by you
(or root) and must not be writable by other users. This deliberately rejects
**any** group/world-writable component — including sticky directories like
`/tmp` — so a virtualenv created under `/tmp` cannot host the scheduled
service even though it is fine for one-off interactive use. Install the venv
under your home directory instead. It also checks the interpreter-startup
hooks Python runs before ephemdir is imported (`.pth` files in site-packages,
`sitecustomize`, `pyvenv.cfg`, and the `tomli` package on Python 3.10).

> **Trust boundary for `install-service`.** The scheduled job runs your Python
> interpreter unattended and later, as your user. ephemdir verifies its own
> package, the interpreter, and the startup hooks above, but it cannot
> exhaustively vet every module a `.pth` file or a runtime dependency
> might import — that is the same surface as any Python command you run.
> **Only install the service from a Python environment whose `site-packages`
> are not writable by other local users** (a normal per-user `pip`/`venv`
> install satisfies this). On a single-user machine there is nothing extra to
> do; on a shared multi-user host, ensure the environment is owned by you and
> not group/world-writable before scheduling sweeps.

Prefer to wire it up yourself? The equivalents are:

* **Linux (cron):** `*/10 * * * * ephemdir sweep`
* **macOS (launchd):** a `LaunchAgent` running `ephemdir sweep`; template in
  [`packaging/`](packaging/).

You can also keep a foreground watcher running:

```bash
ephemdir watch --interval 600
```

## Configuration

Set per-user defaults in a `config.toml` file in your config directory
(`~/.config/ephemdir/` on Linux or `~/Library/Application Support/ephemdir/`
on macOS). Every key is optional:

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
ephemdir recover brave-otter     # retry an interrupted deletion
ephemdir recover brave-otter --forget  # forget it without deleting files
ephemdir watch                   # sweep periodically in the foreground
ephemdir install-service         # schedule sweeps via the OS scheduler
ephemdir uninstall-service       # remove the scheduled service
```

Every command that takes a directory accepts a full path, the exact name or a
unique prefix (`bra` or `brave-otter` for `brave-otter-a81f42c9d047315b`).
Add `-v` for more output or `-q` to
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
ephemdir (the entry is dropped automatically) · `⚠️` replaced by another
directory (never touched) · `⚪` legacy entry from ephemdir ≤ 0.3 (resolve
with `rm`/`keep`) · `🚧` interrupted deletion requiring `recover` · `❓`
temporarily inaccessible but still tracked. Terminals without emoji support
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

## Reproducible source builds

`pyproject.toml` only declares the minimum safe build backend
(`setuptools>=78.1.1`, which includes the fix for CVE-2025-47273), so a plain
isolated build resolves whatever backend version is current. To build exactly
what release CI builds, use the hash-pinned toolchain:

```bash
python -m pip install --require-hashes -r requirements-build.txt
python -m build --no-isolation
```

Release artifacts are produced this way in `.github/workflows/publish.yml`;
the workflow records SHA-256 hashes of `dist/` right after the build and
verifies them again after the install smoke test.

## License

MIT © vindfjur
