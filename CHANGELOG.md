# Changelog

All notable changes to this project are documented here. The format is based on
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/) and this project
adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

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
