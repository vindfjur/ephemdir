# Changelog

All notable changes to this project are documented here. The format is based on
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/) and this project
adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

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
- Playful two-word directory names via `coolname`.
- Cross-platform support for Linux, macOS and Windows (registry location and
  restart detection).
- `ephemdir` command-line interface: `new`, `sweep`, `list`, `watch`.
