# Changelog

All notable changes to this project are documented here. The format is based on
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/) and this project
adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [0.5.0] - 2026-06-18

### Added
- Added `cleanup="next-sweep"` / `ephemdir new --until-sweep` for directories
  that are removed only by explicit full sweeps, watchers or schedulers.
- Added `max_size` cleanup thresholds with bounded, mount-aware POSIX size
  scanning.
- Added `name_style=auto|clean|secure`; clean names are allowed only in
  owner-private parent directories.
- Added `ephemdir sweep --dry-run`, `ephemdir explain`, `ephemdir doctor`,
  `ephemdir menu`, `ephemdir completion show` and completion script printing.
- Added a registry v2 envelope with legacy flat-registry migration on the next
  write.

### Changed
- v0.5.0 is a POSIX-only hardening release for Linux and macOS. Windows remains
  intentionally unsupported until a handle-bound recursive deletion backend is
  available.
- `install-service` now validates the persistent Python runtime, pins the
  effective data/config directories into launchd/systemd definitions and runs
  sweeps via `python -I -m ephemdir`.
- Generated names, parent validation, registry loading and scheduled sweep
  setup now fail closed when ownership, symlink, mount or file-type checks are
  ambiguous.

### Fixed
- Corrupt registries, future schemas and malformed individual entries now block
  operations without replacing the active registry with empty state.
- Foreign-platform or unsupported-backend entries are preserved instead of
  being pruned as stale.
- POSIX deletion and size scanning share fd-relative identity and mount-boundary
  checks, including crash/recovery handling for interrupted deletions.
- `--force` no longer bypasses in-use, ownership, identity, backend or platform
  safety gates.

### Tests
- External POSIX audit passed for v0.5.0 release.
- Local release gate covers ruff, mypy strict, pytest, source coverage at 90%,
  build/twine checks, clean wheel install and extracted-zip tests.

## [0.4.0] - 2026-06-14

### Added
- Ownership markers are written into managed directories and matched against the
  registry before automatic deletion. Marker identity and Unix inode snapshots
  prevent ephemdir from removing a directory it did not create.
- Deletion is journaled and recoverable. Interrupted claims keep their staging
  paths tracked, later sweeps can retry safe work, and `ephemdir recover`
  handles ambiguous recovery states explicitly.
- POSIX cleanup now uses fd-relative directory operations, symlink checks,
  parent trust checks and Linux mount-boundary checks before recursive removal.
- `ephemdir recover <name>` and `ephemdir recover <name> --forget` were added
  for manually resolving interrupted deletion states.
- Generated names now include a 64-bit random token after the word slug, making
  shared temporary-directory name exhaustion impractical.
- CI and release builds use hash-pinned dependency files, pinned GitHub Actions
  and a non-isolated build step for reproducible source distributions.
- `SECURITY.md`, `MANIFEST.in` and release-oriented packaging metadata are now
  included with the source project.

### Changed
- Supported runtimes are now Python 3.10+ on Linux and macOS. Unsupported
  platforms fail before creating managed directories until a safe recursive
  deletion backend is available.
- The scheduled service runs `python -I -m ephemdir` from `/` with a scrubbed
  environment, and installation validates interpreter, package and unit-file
  paths before writing service files.
- Runtime dependencies were reduced: Python 3.11+ uses only the standard
  library, while Python 3.10 uses `tomli` for TOML configuration parsing.
- Config files, registry files, service directories and helper executables are
  accepted only when ownership and permissions make them trustworthy.
- `keep()`, `extend()`, sweeps and context-manager cleanup now re-check registry
  and marker state so stale handles cannot affect a replacement directory.
- Trust checks now cover whole ancestor chains: a helper executable is rejected
  when any parent directory above it is foreign-owned or writable by others, and
  `install-service` validates every importable module in the package tree
  (`__main__.py`, `cli.py`, `core.py`, all `_*.py`, cached bytecode and native
  extensions) — not just one entry point — before writing any unit/plist. It
  also validates the interpreter-startup hooks `python -I` still executes:
  site-packages `.pth` files, `sitecustomize`, `pyvenv.cfg`, and the `tomli`
  package on Python 3.10. Every directory in the package tree is validated in
  its own right — including an empty or foreign-owned subdirectory such as a
  freshly created `__pycache__`, whose owner could otherwise drop an unchecked
  `.pyc` after installation — and a symlinked subdirectory that `os.walk`
  would not descend into is refused outright. The walk itself fails closed: a
  subdirectory that cannot be entered (e.g. a foreign `__pycache__` at mode
  `0000`) raises rather than being silently skipped, and each subdirectory is
  validated before descent. The remaining trust boundary —
  `install-service` cannot vet every
  module a `.pth` or runtime dependency may import, so it must be run from an
  environment whose `site-packages` are not writable by other users — is now
  documented in `README.md` and `SECURITY.md`.

### Fixed
- Recursive deletion no longer follows path replacements after verification.
- A replacement directory at a previously tracked path is left untouched and the
  stale entry is dropped or moved to recovery as appropriate.
- Partial deletes no longer overwrite a new object at the original path.
- Corrupt registries are quarantined under lock instead of being silently
  replaced, and malformed entries are rejected on load.
- Upgrading from ephemdir ≤ 0.3 no longer quarantines an old registry that is
  merely world/group *readable* (written with the older umask): it is tightened
  to `0600` in place on load, so previously tracked directories stay tracked
  instead of becoming silent orphans. A registry that is world/group
  *writable*, however, may have been tampered with by another local user (e.g.
  a forced expiry), so it is now refused outright — never parsed, swept, or
  overwritten — and left untouched for the owner to inspect.
- Registry and marker reads reject symlinks, FIFOs, oversized data and malformed
  payloads without blocking commands or scheduled sweeps.
- `keep_while_in_use` fails closed when an in-use probe cannot answer.
- Lifetimes, prefixes, intervals, registry numbers and stored paths now reject
  malformed or non-finite values consistently.
- CLI validation errors are reported as concise user-facing errors instead of
  tracebacks.

## [0.3.0] - 2026-06-11

### Added
- Manage tracked directories by name, unique prefix or path — both from Python
  (`keep()`, `extend()`, `remove()`, `resolve()`, `prune()`) and the CLI:
  - `ephemdir keep <name>` — stop tracking a directory you want to keep; it
    becomes permanent and is never auto-removed.
  - `ephemdir extend <name> 2h` — give a directory a fresh lifetime from now;
    `--forever` removes the time limit.
  - `ephemdir rm <name>` — delete a tracked directory right away.
  - `ephemdir path [<name>]` — print a tracked directory's path (defaults to
    the most recently created one).
  - `ephemdir prune` — forget entries whose directories were deleted manually.
- `ephemdir shell-init [bash|zsh|fish|powershell]` prints shell functions to
  `eval` in your rc file: `ecd <name>` jumps into a tracked directory and
  `enew` creates one and jumps into it.
- `ephemdir list` now shows a status icon, the directory name and the time
  left (`🟢 brave-otter  1h 23m left  /path`), with an automatic ASCII
  fallback for terminals without emoji support, a `--plain` flag and a
  `--json` mode for scripting.
- `EphemeralDirectory.extend()` to refresh a lifetime through the handle.
- `EPHEMDIR_DATA_DIR` / `EPHEMDIR_CONFIG_DIR` environment variables override
  the registry and config locations (sandboxes, tests, dotfile setups).

### Changed
- Directories deleted outside ephemdir are now handled gracefully everywhere:
  `list` marks them as gone (👻), sweeps log how many stale entries were
  dropped, and resolving one by name removes the stale entry with a clear
  error message.
- The registry now always stores absolute paths, so directories created via a
  relative `parent` are still found when a sweep runs from another working
  directory. `parent` also expands `~`.

## [0.2.0] - 2026-06-05

### Added
- `keep_while_in_use` option: a sweep defers deleting a directory while a
  process still has files open inside it (Linux/macOS via `lsof`).
- User config file (`config.toml` in the per-user config dir) to set defaults
  for `lifetime`, `remove_on_restart`, `keep_while_in_use`, `parent`, `prefix`
  and `words`.
- `ephemdir install-service` / `uninstall-service` to register a scheduled
  sweep with launchd (macOS), systemd user timers (Linux) or Task Scheduler
  (Windows).
- `python -m ephemdir` entry point.
- Tooling: `ruff` and `mypy` configuration and a CI lint job.

### Changed
- `tempdir()` and the `new` CLI command now resolve unset options from the user
  config file before falling back to built-in defaults.
- Deletion is now transactional (rename-then-remove), so a locked file can never
  leave a half-deleted directory; on Windows a locked file defers cleanup
  instead, giving `keep_while_in_use` meaningful cross-platform behaviour.

### Fixed
- In-use detection no longer trusts `lsof`'s exit code (unreliable with `+D`):
  open files are detected from the command output, so `keep_while_in_use`
  actually defers cleanup on Linux/macOS.

## [0.1.0] - 2026-06-05

### Added
- Initial release.
- `tempdir()` to create self-cleaning ephemeral directories with optional
  `lifetime` and `remove_on_restart` policies.
- `EphemeralDirectory` path-like handle with context-manager support and
  `remove()` / `keep()` helpers.
- Playful two-word directory names.
- Cross-platform support for Linux, macOS and Windows (registry location and
  restart detection).
- `ephemdir` command-line interface: `new`, `sweep`, `list`, `watch`.
