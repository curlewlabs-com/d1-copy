"""A run's working files are full copies of a database. These cases are the
ways a run can end - a terminating signal among them - and each must leave
nothing behind and must not pass for a run that finished."""

from __future__ import annotations

import os
import pathlib
import signal
import tempfile
import time
import unittest
from typing import Any, override
from unittest import mock

from d1_copy import workdir
from d1_copy.interrupting import Interrupting


class WorkdirTest(unittest.TestCase):
    @override
    def setUp(self) -> None:
        scratch = tempfile.TemporaryDirectory()
        self.addCleanup(scratch.cleanup)
        self.parent = pathlib.Path(scratch.name) / "work"
        patch = mock.patch.object(workdir, "WORK_PARENT", self.parent)
        patch.start()
        self.addCleanup(patch.stop)

    def _runs(self) -> list[str]:
        return sorted(p.name for p in self.parent.glob("run-*"))

    def test_a_terminating_signal_removes_the_files_and_ends_the_run(self) -> None:
        before = signal.getsignal(signal.SIGTERM)
        with (
            self.assertRaises(KeyboardInterrupt),
            workdir.private_workdir() as directory,
        ):
            (directory / "source.sql").write_text("rows", encoding="utf-8")
            os.kill(os.getpid(), signal.SIGTERM)
            # The handler runs at the next bytecode boundary; this gives it one.
            time.sleep(1)
            self.fail("the run went on past its signal")
        self.assertEqual(self._runs(), [])
        self.assertEqual(signal.getsignal(signal.SIGTERM), before)

    def test_the_directory_is_private_to_the_user(self) -> None:
        with workdir.private_workdir():
            self.assertEqual(self.parent.stat().st_mode & 0o777, 0o700)

    def test_a_second_run_is_refused_while_one_holds_the_directory(self) -> None:
        """Without it the second run's sweep would delete the first one's files."""
        with workdir.private_workdir() as directory:
            with self.assertRaises(workdir.WorkdirInUse), workdir.private_workdir():
                pass
            self.assertTrue(directory.exists())


class InterruptingTest(unittest.TestCase):
    @override
    def setUp(self) -> None:
        self.hook_calls: list[Any] = []
        self.interrupting = Interrupting(self.hook_calls.append)

    def _first(self) -> KeyboardInterrupt:
        with self.assertRaises(KeyboardInterrupt) as raised:
            self.interrupting.handle(signal.SIGTERM, None)
        return raised.exception

    def test_a_later_signal_does_not_cut_the_cleanup_short(self) -> None:
        """The first signal decided the run's ending; a second one raising inside
        the cleanup would stop the working files' removal part-way."""
        self._first()
        # Neither raises.
        self.interrupting.handle(signal.SIGHUP, None)
        self.interrupting.handle(signal.SIGTERM, None)

    def test_an_interrupt_python_discarded_is_delivered_again(self) -> None:
        """A raise inside a __del__ goes to sys.unraisablehook instead of
        unwinding the run, which would let a cancelled run carry on."""
        discarded = self._first()
        previous = signal.signal(signal.SIGALRM, self.interrupting.handle)
        self.addCleanup(signal.signal, signal.SIGALRM, previous)
        self.addCleanup(signal.setitimer, signal.ITIMER_REAL, 0)
        unraisable = mock.Mock(exc_value=discarded)
        with self.assertRaises(KeyboardInterrupt):
            self.interrupting.redeliver(unraisable)
            time.sleep(1)
            self.fail("the discarded interrupt was not delivered again")
        self.assertEqual(self.hook_calls, [unraisable])

    def test_an_unrelated_unraisable_error_arms_nothing(self) -> None:
        unraisable = mock.Mock(exc_value=ValueError())
        with mock.patch("signal.setitimer") as setitimer:
            self.interrupting.redeliver(unraisable)
        setitimer.assert_not_called()
        self.assertEqual(self.hook_calls, [unraisable])

    def test_a_run_that_finished_after_its_signal_still_ends_interrupted(
        self,
    ) -> None:
        """The redelivery is prompt, not certain: the run can complete before it
        lands, and that completion must not read as success."""
        interrupt = self._first()
        with self.assertRaises(KeyboardInterrupt) as ended:
            self.interrupting.spend(None)
        self.assertIs(ended.exception, interrupt)

    def test_a_run_ended_by_the_interrupt_itself_is_not_raised_twice(self) -> None:
        interrupt = self._first()
        self.interrupting.spend(interrupt)

    def test_a_run_with_no_signal_ends_as_it_ended(self) -> None:
        self.interrupting.spend(None)
        self.interrupting.spend(ValueError())


if __name__ == "__main__":
    unittest.main()
