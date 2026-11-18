"""The private directory a run keeps its dumps and working databases in.

Dumps and the databases built from them are full copies of a database. They live
under one parent that this user alone owns and can open (_open_parent), and are
removed on every exit this process can observe, including a terminating signal
(interrupting.py). A run that was killed outright leaves its directory behind;
the next run removes it before it starts.
"""

from __future__ import annotations

import contextlib
import errno
import fcntl
import os
import pathlib
import secrets
import shutil
import signal
import stat
import sys
import tempfile
from collections.abc import Generator

from .interrupting import Interrupting

# One parent for every run's working directory, so a later run can find and
# remove what a killed one could not.
WORK_PARENT = pathlib.Path(tempfile.gettempdir()) / "d1-copy"


class WorkdirInUse(RuntimeError):
    """Another run holds the working directory. Nothing was written."""


class WorkdirUnsafe(RuntimeError):
    """WORK_PARENT is not a directory this user alone owns. Nothing was written."""


def _open_parent() -> int:
    """A descriptor on WORK_PARENT, created here or found to be this user's own
    private directory.

    The lock file and the removal of leftovers act through this descriptor,
    never through the path. A shared temporary directory lets another account
    put something at that path first: a symlink to a directory of ours, whose
    `run-*` entries the sweep would then delete, or a directory of theirs, where
    our dumps would land. So a symlink is not followed, and a directory another
    user owns, or that anyone else can open, is refused rather than repaired.
    """
    with contextlib.suppress(FileExistsError):
        os.mkdir(WORK_PARENT, 0o700)
    try:
        parent = os.open(WORK_PARENT, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    except OSError as exc:
        if exc.errno in (errno.ELOOP, errno.ENOTDIR):
            raise WorkdirUnsafe(
                f"{WORK_PARENT} is a symlink or not a directory; remove it"
            ) from exc
        if exc.errno == errno.EACCES:
            raise WorkdirUnsafe(
                f"{WORK_PARENT} cannot be opened by this user; remove it"
            ) from exc
        raise
    info = os.fstat(parent)
    if info.st_uid != os.getuid() or stat.S_IMODE(info.st_mode) & 0o077:
        os.close(parent)
        raise WorkdirUnsafe(
            f"{WORK_PARENT} is owned by another user or open to others; remove it"
        )
    return parent


@contextlib.contextmanager
def private_workdir() -> Generator[pathlib.Path]:
    parent = _open_parent()
    try:
        # Held for the whole run. It is what makes the sweep below safe: without
        # it a second run would delete the first one's dumps from under it. It is
        # also the only serialization this tool has of its own, and a second copy
        # into a target already being filled is not a state anything downstream
        # could interpret.
        lock = os.open(
            "lock", os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW, 0o600, dir_fd=parent
        )
        try:
            try:
                fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except OSError as exc:
                raise WorkdirInUse(
                    f"another run holds the working directory {WORK_PARENT}"
                ) from exc
            with _run_directory(parent) as workdir:
                yield workdir
        finally:
            os.close(lock)
    finally:
        os.close(parent)


@contextlib.contextmanager
def _run_directory(parent: int) -> Generator[pathlib.Path]:
    for leftover in os.listdir(parent):
        if leftover.startswith("run-"):
            # A run that was SIGKILLed had no chance to clean up. Its directory
            # holds a database's data and is this tool's own, so the next run
            # removes it.
            shutil.rmtree(leftover, dir_fd=parent, ignore_errors=True)
    name = f"run-{secrets.token_hex(8)}"
    os.mkdir(name, 0o700, dir_fd=parent)
    interrupting = Interrupting(sys.unraisablehook)
    previous = {
        signum: signal.signal(signum, interrupting.handle)
        for signum in (signal.SIGTERM, signal.SIGHUP, signal.SIGINT, signal.SIGALRM)
    }
    sys.unraisablehook = interrupting.redeliver
    ended_by: BaseException | None = None
    try:
        yield WORK_PARENT / name
    except BaseException as error:
        ended_by = error
        raise
    finally:
        # Held, then disarmed, first, so no signal - one arriving now for the
        # first time included - and no redelivery can cut this cleanup short.
        interrupting.hold()
        signal.setitimer(signal.ITIMER_REAL, 0)
        sys.unraisablehook = interrupting.previous_hook
        shutil.rmtree(name, dir_fd=parent, ignore_errors=True)
        for signum, handler in previous.items():
            signal.signal(signum, handler)
        interrupting.spend(ended_by)
