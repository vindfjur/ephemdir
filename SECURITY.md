# Security Policy

## Supported Versions

The supported release line is `0.4.x`. Supported runtimes are Python 3.10+ on
Linux and macOS. Windows is not supported until a handle-bound recursive
deletion backend is available.

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
violations. When a platform cannot provide the required safe primitive,
`tempdir()` fails before creation and cleanup fails closed before claim: the
original pathname and active entry stay untouched rather than being moved or
deleted by pathname. Registry reads accept only owner-private regular files
up to 1 MiB and use non-blocking no-follow opens, preventing FIFOs and other
special files from stalling commands or the scheduled sweeper. A registry that
is group/world-*writable* is treated as potentially tampered: it is refused
outright (not parsed, swept, emptied or quarantined) and left in place for
manual inspection, while a merely world-*readable* legacy registry is tightened
to `0600` on load.

## `install-service` trust boundary

`install-service` schedules your Python interpreter to run `ephemdir sweep`
unattended, later, as your user. Before writing any unit/plist it verifies that
the interpreter, the entire `ephemdir` package tree (rejecting symlinked
package subdirectories) and the interpreter-startup hooks (`.pth` files,
`sitecustomize`, `pyvenv.cfg`, and `tomli` on Python 3.10) are owned by you or
root and not writable by other users.

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
