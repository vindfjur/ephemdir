# Changelog

All notable changes to this project are documented here. The format is based on
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/) and this project
adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

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
