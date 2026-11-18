"""The private directory a run keeps its dumps and working databases in.

Dumps and the databases built from them are full copies of a database. They live
under one parent, readable by this user alone, and are removed on every exit
this process can observe, including a terminating signal (interrupting.py). A
run that was killed outright leaves its directory behind; the next run removes
it before it starts.
"""

from __future__ import annotations

import contextlib
import fcntl
import pathlib
import shutil
import signal
import sys
import tempfile
from collections.abc import Iterator

from .interrupting import Interrupting

# One parent for every run's working directory, so a later run can find and
# remove what a killed one could not.
WORK_PARENT = pathlib.Path(tempfile.gettempdir()) / "d1-copy"


class WorkdirInUse(RuntimeError):
    """Another run holds the working directory. Nothing was written."""


@contextlib.contextmanager
def private_workdir() -> Iterator[pathlib.Path]:
    parent = WORK_PARENT
    parent.mkdir(mode=0o700, exist_ok=True)
    parent.chmod(0o700)
    # Held for the whole run. It is what makes the sweep below safe: without it
    # a second run would delete the first one's dumps from under it. It is also
    # the only serialization this tool has of its own, and a second copy into a
    # target already being filled is not a state anything downstream could interpret.
    with (parent / "lock").open("w") as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError as exc:
            raise WorkdirInUse(
                f"another run holds the working directory {parent}"
            ) from exc
        for leftover in parent.glob("run-*"):
            # A run that was SIGKILLed had no chance to clean up. Its directory
            # holds a database's data and is this tool's own, so the next run
            # removes it.
            shutil.rmtree(leftover, ignore_errors=True)
        workdir = pathlib.Path(tempfile.mkdtemp(prefix="run-", dir=parent))
        interrupting = Interrupting(sys.unraisablehook)
        previous = {
            signum: signal.signal(signum, interrupting.handle)
            for signum in (signal.SIGTERM, signal.SIGHUP, signal.SIGALRM)
        }
        sys.unraisablehook = interrupting.redeliver
        ended_by: BaseException | None = None
        try:
            yield workdir
        except BaseException as error:
            ended_by = error
            raise
        finally:
            # Disarmed first, so a redelivery cannot cut this cleanup short.
            signal.setitimer(signal.ITIMER_REAL, 0)
            sys.unraisablehook = interrupting.previous_hook
            shutil.rmtree(workdir, ignore_errors=True)
            for signum, handler in previous.items():
                signal.signal(signum, handler)
            interrupting.spend(ended_by)
