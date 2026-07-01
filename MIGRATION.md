# Registry migration guide

ephemdir stores everything it tracks in a single registry file
(`registry.json`, under the platform data directory or `EPHEMDIR_DATA_DIR`). The
file carries a versioned envelope so the format can evolve without losing data.

You normally never touch this file or think about migrations — they happen
automatically and safely. This document exists for transparency and recovery.

## How upgrades happen

- **Lazy.** When ephemdir reads a registry written by an older version, it uses
  it as-is. The file is only rewritten in the new format the next time a command
  **modifies** it (`new`, `keep`, `extend`, `rm`, `sweep`, recovery). Read-only
  commands (`list`, `tree`, `path`, `last`, `explain`, `doctor`) never rewrite
  it.
- **Backed up first.** Before the first write in the new format, ephemdir copies
  the old file to an owner-only backup beside the registry, e.g.
  `registry.json.v2.bak` (or `registry.json.v1.bak` for the original flat
  format). If that name already exists it uses a timestamped variant like
  `registry.json.v2.<timestamp>-<random>.bak`; an existing backup is **never**
  overwritten.
- **Atomic.** The new file is written to a temporary file, flushed and fsynced,
  then atomically renamed into place under the registry lock. If anything fails,
  the original file is left intact and no partial state is published.
- **Idempotent.** Re-running on an already-current registry does nothing and
  takes no further backup.

`ephemdir doctor` reports the on-disk schema version and whether an upgrade is
pending on the next change.

## Schema history

| Schema | Introduced | Change |
|--------|------------|--------|
| flat (v1) | 0.1 | Bare JSON object of entries, no envelope. Still readable; upgraded to the current schema and backed up as `registry.json.v1.bak`. |
| v2 | 0.4 | Versioned envelope (`schema_version`, `writer`, `entries`). |
| v3 | 0.7 | Adds optional per-directory `tags` and `description` fields. A v2 entry is already valid v3 (absent = default), so the upgrade is the envelope bump plus the backup. |

A registry written by a **newer** ephemdir than you are running is refused
rather than rewritten, so downgrading does not corrupt it.

## If something goes wrong

- A corrupt or foreign-owned registry is never silently emptied. ephemdir
  refuses the command, leaves the file in place, and (for corrupt content)
  copies the bytes aside as `registry.json.corrupt-<…>` for inspection.
- To roll back an upgrade, stop ephemdir, restore the relevant
  `registry.json.v*.bak` over `registry.json`, and run an **older** ephemdir.
- The registry file format is internal. To read your tracked directories from a
  script, use `ephemdir list --json` (or the `registered()` API), not the file.
