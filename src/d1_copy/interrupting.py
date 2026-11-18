"""A copy's interrupt: a SIGTERM, SIGHUP or SIGINT, however Python delivers it.

The private working directory (workdir.py) holds full copies of a database, and
its `finally` removes them only if the signal that ends a run arrives as an
exception rather than as the default action. `Interrupting` is that exception's
source: it raises it, raises it again when Python discards it, keeps any signal
(the first included) from cutting the removal short, and hands it back as the
run's ending at the boundary. The working directory's own order - hold, disarm,
remove, restore, spend - stays with the working directory.
"""

from __future__ import annotations

import signal
import sys
from collections.abc import Callable
from types import FrameType

# How soon an interrupt whose raise Python discarded is delivered again; see
# `Interrupting`.
INTERRUPT_REDELIVERY_SECONDS = 0.1


class Interrupting:
    """Turns the first signal it catches into KeyboardInterrupt, and sees it land.

    Turned into an exception so the `finally` that removes the working directory
    runs; the default action would end the process around it.

    Python runs a signal handler at a later point in the main thread
    (https://docs.python.org/3/library/signal.html#execution-of-python-signal-handlers),
    and an exception raised where Python has no way to handle it - a `__del__`
    among those places - is handed to `sys.unraisablehook` rather than raised
    (https://docs.python.org/3/library/sys.html#sys.unraisablehook), which
    would let a cancelled run carry on. So `redeliver` stands as that hook: when
    it is handed one of `handle`'s own raises, it fires SIGALRM, which comes back
    through `handle` for the signal that came first. Only then, so a raise that
    landed is never followed by another.
    """

    def __init__(self, previous_hook: Callable[[sys.UnraisableHookArgs], object]):
        self.previous_hook = previous_hook
        self._first: list[int] = []
        self._raised: list[KeyboardInterrupt] = []
        self._redelivery_armed = False
        self._holding = False

    def hold(self) -> None:
        """From now on a signal is recorded, never raised.

        For the working directory's removal: an interrupt raised inside it
        would stop it part-way and leave a copy of the database on disk. A
        signal that arrives while held still ends the run, through `spend`.
        """
        self._holding = True

    def handle(self, signum: int, _frame: FrameType | None) -> None:
        if self._holding:
            if not self._first:
                self._first.append(signum)
                self._raised.append(KeyboardInterrupt(f"signal {signum}"))
            return
        if self._first:
            # Decided already, so only the redelivery `redeliver` armed raises
            # again. A second SIGTERM, SIGHUP or SIGINT would otherwise raise
            # inside the cleanup the first interrupt started, the working
            # directory's removal among it.
            if signum != signal.SIGALRM or not self._redelivery_armed:
                return
            self._redelivery_armed = False
        else:
            self._first.append(signum)
        interruption = KeyboardInterrupt(f"signal {self._first[0]}")
        self._raised.append(interruption)
        raise interruption

    def redeliver(self, unraisable: sys.UnraisableHookArgs) -> None:
        if any(unraisable.exc_value is raised for raised in self._raised):
            self._redelivery_armed = True
            signal.setitimer(signal.ITIMER_REAL, INTERRUPT_REDELIVERY_SECONDS)
        self.previous_hook(unraisable)

    def spend(self, ended_by: BaseException | None) -> None:
        """Raise the interrupt a signal asked for, in place of however the run ended.

        The redelivery above is prompt, not certain: the run can finish, or fail
        some other way, before it lands, and the working directory's cleanup
        disarms it. A normal finish included, the interrupt is what the run
        ended with - unless what ended it is the interrupt's own raise.
        """
        if self._raised and not any(ended_by is raised for raised in self._raised):
            raise self._raised[0] from ended_by
