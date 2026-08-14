# ephemdir public contract

> **Status: pre-1.0 freeze candidate.** This document pins the surfaces ephemdir
> intends to keep stable. While the project is `0.x`, these may still change
> between minor releases (changes are called out in `CHANGELOG.md`). At `1.0.0`
> this contract is frozen: anything listed here changes only with a major
> version bump. If something is not listed here, treat it as internal and
> unstable (the leading-underscore modules and names are always internal).

## Versioning

ephemdir follows [Semantic Versioning](https://semver.org). Pre-1.0, minor
releases may make breaking changes (documented in the changelog). From 1.0.0,
breaking changes to anything in this document require a major bump.

## Python API

The public API is exactly the names exported from the top-level package
(`ephemdir.__all__`):

```
tempdir, sweep, registered, keep, extend, remove, resolve, prune, recover,
explain, plan_sweep, parse_size,
CleanupDecision, CleanupPolicy, SweepMode, EphemeralDirectory,
EphemdirError, RegistryError, RegistryUnavailableError, CorruptRegistryError,
UnsafeRegistryError, UnsupportedPlatformError, SafetyError, TargetError,
__version__, __author__
```

Anything imported from a submodule (especially `ephemdir._*`) is internal and
may change at any time.

`extend` raises `OSError` when the path no longer holds the directory ephemdir
created (it was deleted and replaced): that entry can never be cleaned up
automatically, so a fresh deadline would be a promise nothing can keep. The
recorded lifetime is left untouched.

## CLI commands

Stable subcommands: `new`, `list`, `tree`, `path`, `last`, `keep`, `extend`,
`rm`, `sweep`, `explain`, `stats`, `prune`, `recover`, `watch`, `doctor`,
`shell-init`, `completion`, `menu`, `service`, `install-service`,
`uninstall-service`. `service` groups `install`, `uninstall` and the
read-only `status` (`install-service`/`uninstall-service` are aliases);
`completion` groups `show`, `install`, `uninstall` and the read-only
`status`.

`list`, `tree`, `sweep` and `keep` accept `--tag` (repeatable; matches
directories carrying every given tag). For `sweep` the tag filter only narrows
what is removed; for `keep` it untracks every matching directory.

Global flags: `--version`, `-v/--verbose`, `-q/--quiet`,
`--color {auto,always,never}`. Long options are never abbreviated
(`allow_abbrev=False`).

## Cleanup timing

Lifetimes are deadlines, not timers. A directory becomes **due** when its
lifetime expires (or, for restart-scoped entries, when the boot session
changes) and is removed by the first sweep that runs afterwards and can
verify ownership. Nothing runs while the machine is asleep or off, so the
wall-clock delay between expiry and removal is bounded only by when a sweep
next happens — the scheduler interval plus any sleep time. Deletion is never
earlier than the deadline; it may be arbitrarily later. Callers that need a
guarantee must run `sweep` themselves.

## Exit codes

| Code | Meaning |
|------|---------|
| `0`  | success |
| `2`  | usage or input-validation error (bad arguments, invalid lifetime/size, conflicting flags) |
| `1`  | any other failure (target not found, registry locked/unsafe, OS error, etc.) |

Only these three codes are emitted. Scripts may rely on `0` = success and `2` =
"you called it wrong"; everything else is a non-zero runtime failure. Do **not**
try to distinguish causes of a runtime failure (e.g. "not found" vs a
safety refusal) by exit code — they all share `1`. For machine diagnosis, read
the `ephemdir: <command>: <reason>` line on stderr; a structured error channel,
if added, will arrive as an additive, compatible change.

## Output streams

- **stdout** carries data only: created paths, `list`/`tree`/`explain` output,
  and every `--json` document.
- **stderr** carries all diagnostics: warnings, progress and errors.
- Errors are formatted uniformly as `ephemdir: <command>: <reason>` and never
  print a traceback for an expected failure.

## Colour

`--color auto` (the default) enables ANSI colour only when stdout is an
interactive terminal, and disables it when output is piped or when the
`NO_COLOR` environment variable is set. `--color always` and `--color never`
are explicit and override both the TTY check and `NO_COLOR`. Colour is never
added to `--json` output, and hostile control characters in names/paths are
escaped (`\xNN`) before any styling.

## JSON contract

`--json` is available on the read-only commands `list`, `tree`, `path`, `last`,
`explain`, `doctor`, `stats`, `completion status` and `service status`. JSON is
strict (no `NaN`/`Infinity`), printed to stdout,
indented. Durations are integer **seconds** (`remaining_seconds`), sizes are
integer **bytes** (`size_bytes`), and timestamps (`created_at`, `expires_at`)
are Unix epoch **seconds and may be fractional**. Unknown keys may be **added**
in a backward-compatible way; existing keys and their meaning are stable.

- `list --json` → array of objects with: `path`, `name`, `status`,
  `lifecycle_state`, `exists`, `original_state`, `staging_path`,
  `staging_state`, `created_at`, `expires_at`, `remaining_seconds`,
  `remove_on_restart`, `keep_while_in_use`, `tags`, `description`. Within one
  row `exists` and `original_state` always agree (`true`/`"present"`,
  `false`/`"missing"`, `null`/`"unknown"`): both come from a single probe, so
  a sweep running concurrently cannot produce a self-contradicting row. They
  describe the **original path only**, never the staging copy — a row in
  `deleting` or `recovery` whose directory has already been renamed reports
  `false`/`"missing"` here and locates the tree under `staging_path`.
- `tree --json` → array of: `path`, `name`, `parent`, `status`, `size_bytes`,
  `size_complete`, `tags`. `size_bytes` is `null` for directories ephemdir does
  not own or that are absent; a `false` `size_complete` means the value is a
  lower bound (scan budget reached).
- `explain --json` → object with: `path`, `name`, `status`, `due`,
  `destructive_allowed`, `remaining_seconds`, `size_bytes`, `max_size_bytes`,
  `reasons`, `blocked_by`, `decision` (array of `{check, ok, detail}`),
  `remove_on_restart`, `keep_while_in_use`, `tags`, `description`. The two
  boolean triggers are also carried by `list --json`; a `null`
  `remaining_seconds` does not mean the directory is permanent. The verdict and
  the metadata beside it are read from **one registry snapshot**, so a
  concurrent `extend`/`keep`/sweep cannot produce a payload whose deadline
  disagrees with its own `reasons`.
- `path --json`, `last --json` → object with `path`, `name`.
- `doctor --json` → array of `{name, ok, message, hint}`.
- `stats --json` → object with `created`, `swept`, `kept`, `removed`,
  `currently_tracked` (all integers).
- `completion status --json` → array of `{shell, path, status}`. `status` is one
  of `not-installed`, `installed`, `outdated`, `modified`, `unrecorded`,
  `foreign`, `irregular`, `unreadable` or `unsafe`.
- `service status --json` → object with `platform`, `manager`, `unit`, `files`,
  `installed`, `active`, `environment`, `command`, `working_directory`,
  `command_valid`, `working_directory_valid`, `registry_status` and
  `registry_present`. `files` maps managed paths to the same verification
  states used by completion status; `active` and validation fields may be
  `null` when the host cannot answer safely. `registry_status` is `present`,
  `missing`, `unsafe`, `unreadable` or `null`.

## Names and tags

Generated directory names use the configured `name_style`. Tags (when set) match
`^[a-z0-9][a-z0-9._-]{0,31}$` (lowercase, alphanumeric start, ≤ 32 characters),
with at most 16 tags per directory. A directory description is ≤ 256 bytes and
must not contain control characters.

## On-disk registry

The registry uses a versioned envelope; the current on-disk schema is **v3**. An
older schema is read and upgraded on the next write after an owner-only backup is
taken; a newer schema is refused, not rewritten. See [MIGRATION.md](MIGRATION.md)
for details and recovery. The registry file format itself is internal: do not
parse it directly — use `ephemdir list --json` / `registered()` instead.
