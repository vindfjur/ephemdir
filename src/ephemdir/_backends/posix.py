"""POSIX deletion backend: the filesystem side of create/probe/claim/delete.

This module owns the syscall clusters of the managed-directory lifecycle
(APA-BE-002): owner-only creation with the ownership marker, the marker/inode
ownership probe, the claim preflight and fd-relative rename, and the fd-bound
recursive deletion of a claimed staging tree. Every primitive is no-follow
and fd-bound; there is no pathname-recursive fallback. Sequencing — registry
transactions, journal states, the decision policy — stays in
:mod:`ephemdir.core`, which drives these operations through
:class:`PosixBackend`.

Seam note: the cross-cutting probes that the frozen behavioural fixtures
monkeypatch on the core module (``core._mount_id_for_fd``,
``core._safe_delete_backend_error``, ``core._open_verified_staging``) are
deliberately resolved through :mod:`ephemdir.core` at call time via
:func:`_core`. Those names are the pinned test seams for fault injection into
the active path; binding them here at import time would silently detach the
fixtures from the code they exist to constrain.
"""

from __future__ import annotations

import errno
import os
import re
import stat
import sys
import uuid
from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path
from types import ModuleType

from ephemdir._mounts import MountBoundary, MountBoundaryError, verify_same_mount
from ephemdir._registry import Entry
from ephemdir._security import open_trusted_directory
from ephemdir._size import SizeResult, measure_tree

from .base import (
    BackendCapabilities,
    ClaimPreflight,
    CreatedDirectory,
    OwnershipProbe,
    ParentValidation,
    ScanBudget,
)

__all__ = ["MARKER_NAME", "PosixBackend", "StagingIdentityError"]

MARKER_NAME = ".ephemdir"
_CONTENTS_CLEARED_CALLBACK_KEY = "_ephemdir_contents_cleared_callback"
_MARKER_ID_PATTERN = re.compile(rb"[0-9a-f]{32}")
_MARKER_PAYLOAD_MAX_BYTES = 33  # 32 lowercase hex characters plus one LF.

class StagingIdentityError(OSError):
    """Raised when a staging pathname no longer names the verified directory."""


@dataclass
class _DeleteFrame:
    fd: int
    names: Iterator[str]
    parent_fd: int | None
    name: str | None
    inode: tuple[int, int]
    close: bool


def _core() -> ModuleType:
    """Return :mod:`ephemdir.core` for the patchable cross-cutting seams."""
    from ephemdir import core

    return core


def open_directory_nofollow(path: str | os.PathLike[str], *, dir_fd: int | None = None) -> int:
    """Open a directory itself, refusing symlinks when the platform supports it."""
    flags = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | getattr(os, "O_NOFOLLOW", 0)
    if dir_fd is None:
        return os.open(path, flags)
    return os.open(path, flags, dir_fd=dir_fd)


def parse_marker_payload(payload: bytes) -> str | None:
    """Validate and decode one bounded ownership-marker payload."""
    if payload.endswith(b"\n"):
        payload = payload[:-1]
    if _MARKER_ID_PATTERN.fullmatch(payload) is None:
        return None
    return payload.decode("ascii")


def write_marker(path: Path, marker_id: str | None = None, *, dir_fd: int | None = None) -> str:
    """Create the ownership marker inside ``path``; return its id.

    Passing an explicit ``marker_id`` recreates a known marker during safe
    recovery or test setup without changing the ownership identity. When
    ``dir_fd`` names the already-opened directory, the marker is created
    relative to that descriptor so a concurrent path swap cannot redirect it.
    """
    marker_id = marker_id or uuid.uuid4().hex
    try:
        encoded = marker_id.encode("ascii")
    except UnicodeEncodeError as error:
        raise ValueError("marker_id must be 32 lowercase hexadecimal characters") from error
    if _MARKER_ID_PATTERN.fullmatch(encoded) is None:
        raise ValueError("marker_id must be 32 lowercase hexadecimal characters")

    flags = (
        os.O_CREAT
        | os.O_EXCL
        | os.O_WRONLY
        | getattr(os, "O_NOFOLLOW", 0)
        | getattr(os, "O_CLOEXEC", 0)
    )
    if dir_fd is not None:
        fd = os.open(MARKER_NAME, flags, 0o600, dir_fd=dir_fd)
    else:
        fd = os.open(path / MARKER_NAME, flags, 0o600)
    try:
        payload = encoded + b"\n"
        written = 0
        while written < len(payload):
            count = os.write(fd, payload[written:])
            if count == 0:
                raise OSError(errno.EIO, "short write while creating ownership marker")
            written += count
    finally:
        os.close(fd)
    return marker_id


def read_marker_at(dir_fd: int) -> str | None:
    """Read a small regular marker relative to an open directory descriptor.

    The marker is attacker-controlled once untrusted code can write inside an
    ephemeral directory.  Fail closed unless no-follow and non-blocking opens
    are available, reject special files, and never read more than the exact
    marker envelope.
    """
    nofollow = getattr(os, "O_NOFOLLOW", 0)
    nonblock = getattr(os, "O_NONBLOCK", 0)
    if not nofollow or not nonblock:
        return None
    flags = os.O_RDONLY | nofollow | nonblock | getattr(os, "O_CLOEXEC", 0)
    try:
        fd = os.open(MARKER_NAME, flags, dir_fd=dir_fd)
    except (OSError, NotImplementedError, TypeError):
        return None
    try:
        marker_stat = os.fstat(fd)
        if not stat.S_ISREG(marker_stat.st_mode):
            return None
        if marker_stat.st_size > _MARKER_PAYLOAD_MAX_BYTES:
            return None
        payload = os.read(fd, _MARKER_PAYLOAD_MAX_BYTES + 1)
        if len(payload) > _MARKER_PAYLOAD_MAX_BYTES:
            return None
        return parse_marker_payload(payload)
    except OSError:
        return None
    finally:
        os.close(fd)


def read_marker(path: Path) -> str | None:
    """Read a marker through a no-follow directory descriptor."""
    try:
        dir_fd = _core()._open_directory_nofollow(path)
    except (OSError, NotImplementedError, TypeError):
        return None
    try:
        return read_marker_at(dir_fd)
    finally:
        os.close(dir_fd)


def inode_matches(path: Path, entry: Entry) -> bool | None:
    """Compare ``path`` against the entry's stored inode *number*.

    Only ``st_ino`` is compared, never ``st_dev``. A device number is not
    stable across reboots — macOS reassigns it for an APFS volume at every boot,
    and it is not guaranteed stable on Linux either — so comparing it would mark
    every tracked directory ``foreign`` after a restart and silently defeat
    restart/expiry cleanup (the directory is never deleted). The on-disk inode
    number is stable across reboots, and the random ownership marker is the
    primary proof of ownership; the inode is a secondary cross-check that still
    distinguishes a replacement created at the same path (which gets a new
    inode number).

    Returns ``True``/``False`` for a definite answer and ``None`` when the
    entry carries no inode information to compare against.
    """
    ino = entry.get("ino")
    if not isinstance(ino, int):
        return None
    try:
        path_stat = os.stat(path, follow_symlinks=False)
    except OSError:
        return False
    return path_stat.st_ino == ino


def fd_inode_matches(fd: int, entry: Entry) -> bool | None:
    """Compare an already-open directory fd against the entry's stored inode number.

    Like :func:`inode_matches`, only ``st_ino`` is compared so the check is
    stable across reboots.
    """
    ino = entry.get("ino")
    if not isinstance(ino, int):
        return None
    return os.fstat(fd).st_ino == ino


def is_real_directory(path: Path) -> bool:
    """Return whether ``path`` itself is a directory, not a symlink to one."""
    try:
        path_stat = os.stat(path, follow_symlinks=False)
    except OSError:
        return False
    return stat.S_ISDIR(path_stat.st_mode)


def ownership(path: Path, entry: Entry) -> str:
    """Classify whether ``path`` is still the directory we registered.

    Returns ``"ours"`` (marker and inode verified), ``"foreign"`` (something
    else occupies the path now) or ``"unverified"`` (cannot be proven either
    way — such directories are never deleted automatically, only via an
    explicit ``rm``).
    """
    core = _core()
    if not core._is_real_directory(path):
        return "foreign"
    marker_id = entry.get("marker_id")
    if not isinstance(marker_id, str):
        return "unverified"
    if core._read_marker(path) != marker_id:
        return "foreign"
    if core._inode_matches(path, entry) is False:
        return "foreign"
    return "ours"


def open_verified_staging(staging: Path, entry: Entry) -> int:
    """Open ``staging`` and prove the fd still names the claimed directory."""
    fd: int = _core()._open_directory_nofollow(staging)
    try:
        fd_stat = os.fstat(fd)
        if not stat.S_ISDIR(fd_stat.st_mode):
            raise StagingIdentityError(errno.ENOTDIR, "staging path is not a directory")

        inode_ok = fd_inode_matches(fd, entry)
        if inode_ok is False:
            raise StagingIdentityError(
                errno.ESTALE,
                f"{staging} no longer matches the claimed directory inode",
            )

        marker_id = entry.get("marker_id")
        marker = read_marker_at(fd)
        if isinstance(marker_id, str):
            # The recorded marker is the primary proof of ownership; a matching
            # inode alone is never sufficient because inode numbers are reused
            # the instant a tree is removed. So when a marker id is on record the
            # marker must still be present AND match — mirroring the claim-time
            # preflight and _staging_ownership, which already treat a missing
            # marker as unproven. Accepting inode-only here would let a same-user
            # race swap the staging path for a marker-less replacement that
            # happened to inherit the recorded inode.
            # The crash-safe root-only phase tolerates a missing (never a
            # wrong) marker, but only with the recorded inode proven live:
            # the backend boundary must not rely on callers having run the
            # recovery-side identity gate first (EPH-AUD-011).
            marker_missing_after_verified_delete = (
                entry.get("delete_phase") == "root-only"
                and marker is None
                and inode_ok is True
            )
            if marker != marker_id and not marker_missing_after_verified_delete:
                raise StagingIdentityError(
                    errno.ESTALE,
                    f"{staging} no longer carries ephemdir's ownership marker",
                )
        elif inode_ok is None:
            raise StagingIdentityError(
                errno.ESTALE,
                f"{staging} cannot be verified without marker or inode",
            )
        return fd
    except BaseException:
        os.close(fd)
        raise


def rmdir_verified_child(parent_fd: int, name: str, child_fd: int) -> None:
    """Remove an empty child directory only while its name still matches ``child_fd``."""
    child_stat = os.fstat(child_fd)
    try:
        live_stat = os.stat(name, dir_fd=parent_fd, follow_symlinks=False)
    except FileNotFoundError as exc:
        raise StagingIdentityError(
            errno.ESTALE,
            f"directory entry {name!r} disappeared before removal",
        ) from exc
    if (live_stat.st_dev, live_stat.st_ino) != (child_stat.st_dev, child_stat.st_ino):
        raise StagingIdentityError(
            errno.ESTALE,
            f"directory entry {name!r} was replaced before removal",
        )
    os.rmdir(name, dir_fd=parent_fd)


def check_child_mount_boundary(child_fd: int, *, root_dev: int, root_mount_id: int | None) -> None:
    """Fail closed when a child directory crosses a filesystem or mount boundary."""
    boundary = MountBoundary(
        root_dev=root_dev,
        root_mount_id=root_mount_id,
        mount_id_required=sys.platform.startswith("linux"),
    )
    try:
        verify_same_mount(child_fd, boundary, mount_id_func=_core()._mount_id_for_fd)
    except MountBoundaryError as error:
        if error.code == "mount-id-unavailable":
            raise OSError(
                errno.ENOTSUP,
                "cannot verify Linux mount boundary while deleting staging tree",
            ) from error
        raise OSError(
            errno.EXDEV,
            "refusing to cross mount boundary while deleting staging tree",
        ) from error


def scan_fd_names(dir_fd: int) -> Iterator[str]:
    """Yield directory entry names from an fd without materializing the listing."""
    with os.scandir(dir_fd) as entries:
        for entry in entries:
            yield entry.name


def delete_directory_contents_fd(
    dir_fd: int,
    *,
    root_dev: int,
    root_mount_id: int | None,
    preserve_root_name: str | None = None,
) -> None:
    """Remove directory contents relative to an fd using an explicit stack."""
    core = _core()
    root_stat = os.fstat(dir_fd)
    root_id = (root_stat.st_dev, root_stat.st_ino)
    seen = {root_id}
    stack = [
        _DeleteFrame(
            fd=dir_fd,
            names=scan_fd_names(dir_fd),
            parent_fd=None,
            name=None,
            inode=root_id,
            close=False,
        )
    ]
    try:
        while stack:
            frame = stack[-1]
            current_fd = frame.fd
            try:
                name = next(frame.names)
            except StopIteration:
                stack.pop()
                seen.discard(frame.inode)
                try:
                    close_method = getattr(frame.names, "close", None)
                    if close_method is not None:
                        close_method()
                    if frame.parent_fd is not None and frame.name is not None:
                        rmdir_verified_child(frame.parent_fd, frame.name, current_fd)
                finally:
                    if frame.close:
                        os.close(current_fd)
                continue

            if name in {".", ".."}:
                continue
            if current_fd == dir_fd and name == preserve_root_name:
                continue
            try:
                child_stat = os.stat(name, dir_fd=current_fd, follow_symlinks=False)
            except FileNotFoundError:
                continue

            if stat.S_ISDIR(child_stat.st_mode):
                child_fd = core._open_directory_nofollow(name, dir_fd=current_fd)
                pushed = False
                try:
                    opened_stat = os.fstat(child_fd)
                    if (opened_stat.st_dev, opened_stat.st_ino) != (
                        child_stat.st_dev,
                        child_stat.st_ino,
                    ):
                        raise StagingIdentityError(
                            errno.ESTALE,
                            f"directory entry {name!r} changed before traversal",
                        )
                    check_child_mount_boundary(
                        child_fd,
                        root_dev=root_dev,
                        root_mount_id=root_mount_id,
                    )
                    child_id = (opened_stat.st_dev, opened_stat.st_ino)
                    if child_id in seen:
                        raise OSError(
                            errno.ELOOP,
                            "refusing to recurse into a repeated directory inode",
                        )
                    seen.add(child_id)
                    stack.append(
                        _DeleteFrame(
                            fd=child_fd,
                            names=scan_fd_names(child_fd),
                            parent_fd=current_fd,
                            name=name,
                            inode=child_id,
                            close=True,
                        )
                    )
                    pushed = True
                finally:
                    if not pushed:
                        os.close(child_fd)
            else:
                try:
                    os.unlink(name, dir_fd=current_fd)
                except FileNotFoundError:
                    continue
    finally:
        while len(stack) > 1:
            frame = stack.pop()
            close_method = getattr(frame.names, "close", None)
            if close_method is not None:
                close_method()
            if frame.close:
                os.close(frame.fd)


def rmdir_verified_staging(staging: Path, staging_fd: int) -> None:
    """Remove the empty staging directory if the pathname still names ``staging_fd``.

    By this point the owned staging tree's contents have already been removed
    through the fd-bound path, so only an empty directory remains to unlink.
    There is no portable fd-only ``rmdir`` primitive, so the final removal is
    necessarily by pathname and carries an accepted same-user TOCTOU window:
    between the identity ``stat`` below and ``os.rmdir`` a racing same-user
    process could swap an *empty* replacement (with the reused inode) into the
    path. The risk envelope is deliberately bounded — ``rmdir`` refuses a
    non-empty directory, a symlink, or a mountpoint, so any replacement that
    actually holds data survives; only an empty directory the racer itself
    created could be removed, which is not a privilege-boundary break. This
    residual race is documented in SECURITY.md and pinned by
    ``test_empty_staging_rmdir_cannot_remove_nonempty_replacement``.
    """
    fd_stat = os.fstat(staging_fd)
    try:
        live_stat = os.stat(staging, follow_symlinks=False)
    except FileNotFoundError as exc:
        raise StagingIdentityError(
            errno.ESTALE,
            f"{staging} disappeared before final directory removal",
        ) from exc
    if (live_stat.st_dev, live_stat.st_ino) != (fd_stat.st_dev, fd_stat.st_ino):
        raise StagingIdentityError(
            errno.ESTALE,
            f"{staging} was replaced before final directory removal",
        )
    os.rmdir(staging)


def finalize_verified_staging_root(staging: Path, staging_fd: int, entry: Entry) -> None:
    """Remove only the verified marker and empty staging root.

    This is the crash-safe second phase of deletion. The registry records
    ``delete_phase=root-only`` *after* every payload entry has been removed but
    before the marker is unlinked. If a reboot lands between marker unlink and
    ``rmdir``, recovery may safely resume this phase because no managed payload
    remains; any newly planted non-empty replacement still survives.
    """
    names = list(scan_fd_names(staging_fd))
    marker_id = entry.get("marker_id")
    allowed = {MARKER_NAME} if isinstance(marker_id, str) else set()
    unexpected = [name for name in names if name not in allowed]
    if unexpected:
        raise StagingIdentityError(
            errno.ENOTEMPTY,
            f"{staging} gained entries after payload deletion",
        )
    if MARKER_NAME in names:
        if read_marker_at(staging_fd) != marker_id:
            raise StagingIdentityError(
                errno.ESTALE,
                f"{staging} ownership marker changed before final removal",
            )
        os.unlink(MARKER_NAME, dir_fd=staging_fd)
    rmdir_verified_staging(staging, staging_fd)


def safe_delete_backend_error() -> str | None:
    """Return why fd-bound recursive deletion is unavailable, if applicable."""
    if os.name != "posix":
        return "safe handle-bound recursive deletion is unavailable on this platform"
    required_dir_fd = (os.open, os.stat, os.unlink, os.rmdir, os.rename, os.mkdir)
    if any(function not in os.supports_dir_fd for function in required_dir_fd):
        return "required dir_fd filesystem operations are unavailable"
    if os.scandir not in os.supports_fd:
        return "scandir cannot enumerate an opened directory descriptor"
    if not getattr(os, "O_DIRECTORY", 0) or not getattr(os, "O_NOFOLLOW", 0):
        return "safe no-follow directory opens are unavailable"
    return None


def preflight_claim_source(
    path: Path,
    parent_fd: int,
    entry: Entry,
    *,
    allow_unverified: bool,
) -> ClaimPreflight:
    """Open and verify the source before any journal or pathname mutation."""
    core = _core()
    try:
        source_fd = core._open_directory_nofollow(path.name, dir_fd=parent_fd)
    except (FileNotFoundError, NotADirectoryError):
        return ClaimPreflight("foreign", None, None, None)
    except (NotImplementedError, TypeError):
        return ClaimPreflight("unsupported", None, None, None)
    keep_open = False
    try:
        source_stat = os.fstat(source_fd)
        live_stat = os.stat(path.name, dir_fd=parent_fd, follow_symlinks=False)
        if not stat.S_ISDIR(source_stat.st_mode):
            return ClaimPreflight("foreign", None, None, None)
        if (source_stat.st_dev, source_stat.st_ino) != (live_stat.st_dev, live_stat.st_ino):
            return ClaimPreflight("changed", None, None, None)

        inode_ok = fd_inode_matches(source_fd, entry)
        marker_id = entry.get("marker_id")
        marker = read_marker_at(source_fd)
        if isinstance(marker_id, str):
            if marker != marker_id or inode_ok is False:
                return ClaimPreflight("foreign", None, None, None)
            verified = True
        else:
            verified = inode_ok is True
        if not verified and not allow_unverified:
            return ClaimPreflight("unverified", None, None, None)

        root_mount_id = core._mount_id_for_fd(source_fd)
        if sys.platform.startswith("linux") and root_mount_id is None:
            return ClaimPreflight("unsupported", None, None, None)
        keep_open = True
        return ClaimPreflight("ready", source_fd, source_stat, root_mount_id)
    except (NotImplementedError, TypeError):
        return ClaimPreflight("unsupported", None, None, None)
    except OSError:
        return ClaimPreflight("locked", None, None, None)
    finally:
        if not keep_open:
            os.close(source_fd)


def rename_to_staging(path: Path, staging: Path, parent_fd: int, source_fd: int) -> None:
    """Rename the preflighted source to its staging sibling, fd-relative only.

    Raises :class:`StagingIdentityError` when the moved name no longer matches
    the preflighted source descriptor; propagates ``OSError`` otherwise. There
    is no pathname fallback.
    """
    os.rename(path.name, staging.name, src_dir_fd=parent_fd, dst_dir_fd=parent_fd)
    moved_stat = os.stat(staging.name, dir_fd=parent_fd, follow_symlinks=False)
    source_stat = os.fstat(source_fd)
    if (moved_stat.st_dev, moved_stat.st_ino) != (source_stat.st_dev, source_stat.st_ino):
        raise StagingIdentityError(
            errno.ESTALE, f"{staging} does not name the preflighted source directory"
        )
    try:
        os.fsync(parent_fd)
    except OSError:
        pass


def create_owned_directory(
    parent: Path,
    name: str,
    marker_id: str | None = None,
    *,
    parent_fd: int | None = None,
) -> CreatedDirectory:
    """One creation attempt: owner-only directory, marker, identity snapshot.

    A name collision raises ``FileExistsError`` before the marker is written;
    the naming loop in core catches it and retries with a different name.
    When ``parent_fd`` is given, every step is relative to that already
    verified descriptor so an ancestor swapped mid-operation cannot redirect
    the creation.
    """
    core = _core()
    candidate = parent / name
    if parent_fd is not None:
        os.mkdir(name, mode=0o700, dir_fd=parent_fd)
        dir_fd = core._open_directory_nofollow(name, dir_fd=parent_fd)
    else:
        candidate.mkdir(mode=0o700, parents=False, exist_ok=False)
        dir_fd = core._open_directory_nofollow(candidate)
    try:
        written_marker = write_marker(candidate, marker_id, dir_fd=dir_fd)
        path_stat = os.fstat(dir_fd)
    finally:
        os.close(dir_fd)
    return CreatedDirectory(
        path=candidate,
        marker_id=written_marker,
        dev=path_stat.st_dev,
        ino=path_stat.st_ino,
    )


def delete_claimed_tree(staging: Path, entry: Entry) -> None:
    """Delete a verified staging tree, binding recursion to an opened directory fd."""
    core = _core()
    backend_error = core._safe_delete_backend_error()
    if backend_error is not None:
        raise OSError(errno.ENOTSUP, backend_error)

    staging_fd = core._open_verified_staging(staging, entry)
    try:
        root_stat = os.fstat(staging_fd)
        stored_mount_id = entry.get("mount_id")
        root_mount_id = core._mount_id_for_fd(staging_fd)
        if sys.platform.startswith("linux") and root_mount_id is None:
            raise OSError(
                errno.ENOTSUP,
                "cannot verify Linux mount boundary while deleting staging tree",
            )
        if (
            isinstance(stored_mount_id, int)
            and root_mount_id is not None
            and stored_mount_id != root_mount_id
        ):
            raise StagingIdentityError(
                errno.ESTALE,
                "staging directory moved to a different mount after claim",
            )
        if entry.get("delete_phase") != "root-only":
            marker_id = entry.get("marker_id")
            delete_directory_contents_fd(
                staging_fd,
                root_dev=root_stat.st_dev,
                root_mount_id=root_mount_id,
                preserve_root_name=MARKER_NAME if isinstance(marker_id, str) else None,
            )
            callback = entry.get(_CONTENTS_CLEARED_CALLBACK_KEY)
            if callable(callback):
                callback()
        finalize_verified_staging_root(staging, staging_fd, entry)
    finally:
        os.close(staging_fd)


class PosixBackend:
    name = "posix"

    def capabilities(self) -> BackendCapabilities:
        safe = os.name == "posix"
        return BackendCapabilities(
            safe_delete=safe,
            reason=None if safe else "not running on POSIX",
        )

    def validate_parent(self, parent: Path) -> ParentValidation:
        """Validate an EXISTING parent directory for fd-relative operations.

        Uses the same no-follow, fd-bound chain verification as the live
        deletion path (never ``Path.stat()``/``is_dir()``, which follow
        symlinks), so this method is safe to become the active backend path
        (APA-BE-006). Two scope limits are part of the contract:

        - A missing parent is reported as not ok. Creating a missing parent
          tail (owner-only 0700 components) is the create flow's job via
          ``_security.open_or_create_trusted_directory``; it is NOT routed
          through this method.
        - No path canonicalisation beyond ``os.path.abspath``. Callers must
          pass canonical paths (core resolves e.g. macOS ``/var`` →
          ``/private/var`` via ``_canonical_private_dir_path``); a symlinked
          alias such as ``/var/...`` is refused here by design.
        """
        try:
            fd = open_trusted_directory(Path(os.path.abspath(parent)))
        except OSError as error:
            return ParentValidation(False, str(error))
        try:
            return ParentValidation(True)
        finally:
            os.close(fd)

    def create_owned_directory(
        self,
        parent: Path,
        name: str,
        marker_id: str | None = None,
        *,
        parent_fd: int | None = None,
    ) -> CreatedDirectory:
        return create_owned_directory(parent, name, marker_id, parent_fd=parent_fd)

    def probe_ownership(self, path: Path, entry: Entry) -> OwnershipProbe:
        return OwnershipProbe(ownership(path, entry))

    def preflight_claim_source(
        self,
        path: Path,
        parent_fd: int,
        entry: Entry,
        *,
        allow_unverified: bool,
    ) -> ClaimPreflight:
        return preflight_claim_source(path, parent_fd, entry, allow_unverified=allow_unverified)

    def rename_to_staging(
        self,
        path: Path,
        staging: Path,
        parent_fd: int,
        source_fd: int,
    ) -> None:
        rename_to_staging(path, staging, parent_fd, source_fd)

    def delete_claimed_tree(self, staging: Path, entry: Entry) -> None:
        delete_claimed_tree(staging, entry)

    def measure_tree(
        self,
        path: Path,
        entry: Entry,
        limit: int | None,
        budget: ScanBudget,
    ) -> SizeResult:
        return measure_tree(path, limit=limit, max_entries=budget.max_entries or 100_000)
