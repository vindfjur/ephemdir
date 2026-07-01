# Security Policy

## Supported Versions

The supported release line is `0.7.x` (with `0.6.x` still receiving security
fixes). Supported runtimes are Python 3.10+ on Linux and macOS. Windows is not
supported until a handle-bound recursive deletion backend is available.

## Reporting a Vulnerability

Please report suspected vulnerabilities privately to the maintainer before
opening a public issue. Include:

- affected ephemdir version and operating system;
- the exact command/API call used;
- the directory layout, permissions and mount/symlink setup;
- whether the parent directory is private, sticky shared, or non-sticky shared;
- a minimal reproducer when possible.

For deletion-safety reports, preserve the registry file and any `.deleting`
staging directory for inspection. Do not run `recover --forget` until the issue
has been triaged.

## Security Model

ephemdir only deletes directories it can verify by marker and inode identity.
POSIX deletion is fd-relative and refuses symlink, parent-trust and mount-boundary
violations. The owned staging tree's contents are removed through opened
directory descriptors; only the final removal of the now-empty staging directory
is by pathname, because POSIX offers no fd-only `rmdir`. That last step carries a
small, accepted same-user race: a racing same-user process could swap an *empty*
replacement into the staging path between the identity check and the `rmdir`. The
window is bounded — `rmdir` refuses a non-empty directory, a symlink or a
mountpoint, so any replacement holding data survives, and only an empty directory
the racer itself created could be removed (not a privilege-boundary break). When a
platform cannot provide the required safe primitive,
`tempdir()` fails before creation and cleanup fails closed before claim: the
original pathname and active entry stay untouched rather than being moved or
deleted by pathname. A missing active path is not treated as proof that cleanup
succeeded: the entry remains tracked and blocked until ephemdir verifies a
deletion, or until the user explicitly forgets it with `prune`, `keep` or
`recover --forget` for recovery entries. Registry reads and writes are bounded to 1 MiB, use a versioned
envelope (schema v3, which adds optional per-directory `tags` and `description`)
on write, and use non-blocking no-follow opens, preventing FIFOs and other
special files from stalling commands or the scheduled sweeper. An older on-disk
schema is read unchanged and upgraded to the current one the next time a command
modifies the registry, after copying the old file to an owner-only backup beside
it (for example `registry.json.v2.bak`); an existing backup is never overwritten,
and a registry written by a *newer* ephemdir is refused rather than rewritten. A
registry that is group/world-*writable* is treated as potentially tampered: it is
refused outright (not parsed, swept, emptied or quarantined) and left in place
for manual inspection. A merely world-/group-*readable* registry is tightened to
`0600` by the next command that writes it; a read-only command (`list`, `tree`,
`path`, `last`, `explain`) deliberately does not mutate it and instead logs a
warning, and `doctor` reports it as a finding. Because the registry now stores
`tags`/`description`, keeping it owner-only also keeps those labels private. Any malformed individual registry entry makes the whole
registry invalid for that command; transactions copy the inspected bytes to a
unique `registry.json.corrupt-*` file, leave the active registry path in place
as a blocking object and abort instead of saving a filtered or empty state.

## `install-service` trust boundary

`install-service` schedules your Python interpreter to run `ephemdir sweep`
unattended, later, as your user. Before writing any unit/plist it verifies that
the interpreter, the entire `ephemdir` package tree (rejecting symlinked
package subdirectories) and the interpreter-startup hooks (`.pth` files,
`sitecustomize`, `pyvenv.cfg`, and `tomli` on Python 3.10) are owned by you or
root. A **world-writable** component, a **foreign-owned** component, a
symlinked package subdirectory, and any group/world-writable **executable**
file or startup hook are always rejected, under every policy.
It also pins the verified effective `EPHEMDIR_DATA_DIR` and
`EPHEMDIR_CONFIG_DIR` into the installed launchd/systemd definition so the
scheduled sweep does not drift to a different registry after logout/login or
shell environment changes.

What it does **not** do is recursively vet every module those hooks or runtime
dependencies may import: a `.pth` line may run an arbitrary `import`, and
validating the full transitive import closure is equivalent to trusting the
whole environment. This is the same surface as running any other Python program
from that interpreter. Therefore **the scheduled service must be installed only
from a Python environment whose `site-packages` cannot be modified by other
local users** — which a normal per-user `pip`/`venv` installation already
guarantees. On a single-user system this requires no action; on a shared host,
confirm the environment's ownership and permissions before scheduling sweeps.
One-off interactive `tempdir()`, `ephemdir sweep` and the rest of the CLI do not
rely on this and are unaffected.

### Runtime-trust policy: `strict` vs `balanced`

The one place the policy is configurable is how a **group-writable directory
ancestor** of the runtime is treated. This is governed by
`--runtime-policy strict|balanced` (or `EPHEMDIR_SERVICE_RUNTIME_POLICY`).

* **`strict`** rejects any group-writable component. It is the default on every
  platform except macOS and is the correct choice on a genuinely shared
  multi-user host.
* **`balanced`** (the default on macOS) allows a group-writable directory
  ancestor only as a narrow, property-checked Homebrew/usr-local carve-out, with
  a warning. **All** of the following must hold, or the component is rejected:
  the platform is macOS; the resolved path is under `/opt/homebrew` or
  `/usr/local`; the directory is owned by root or the installing user; it is not
  world-writable; and its **owning group is a local administrator group**
  (`admin`, gid 80). This reflects the single-user model: the `admin` group on a
  personal Mac is the owner, not an attacker. It exists because a stock Homebrew
  interpreter lives under `/opt/homebrew/Cellar` (mode `0775`, group `admin`);
  under `strict` that ancestor is refused, which silently prevents the scheduled
  sweep from ever being installed — and therefore prevents reboot/expiry cleanup
  from running automatically.

The owning-group check is the load-bearing restriction: POSIX write permission
on a directory lets any member of its group replace entries inside it, so a
group-writable directory on the import path whose group could contain a
*different* unprivileged user would be a code-execution vector for the scheduled
service. Restricting the carve-out to the local `admin` group (plus the prefix
allowlist) keeps the relaxation within the threat model. A group-writable
directory owned by an ordinary shared group is rejected even under `balanced`.

`balanced` relaxes **only** group-writable *directory ancestors* that pass that
carve-out. World-writable components, foreign-owned components, symlinked
package subdirectories, and group/world-writable *executable* files or startup
hooks remain hard failures under both policies. If you prefer `strict`
everywhere, install the service from a private uv-managed venv under your home
directory, whose components are owned only by you.

This threat model deliberately excludes root, the local administrator, and
other members of the owner's own `admin` group on a personal machine; it
defends against a different unprivileged local user, not against the owner.
