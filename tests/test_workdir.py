"""A run's working files are full copies of a database. These cases are the
ways a run can end - a terminating signal among them - and each must leave
nothing behind and must not pass for a run that finished."""

from __future__ import annotations

import os
import pathlib
import shutil
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

    def test_signals_during_the_removal_neither_stop_it_nor_are_lost(
        self,
    ) -> None:
        """A run that finished and is already removing its files is interrupted
        there - by a terminating signal, or by Ctrl-C - once or repeatedly.
        Raised inside the removal, the interrupt would stop it part-way and
        leave a copy of the database on disk."""
        remove = shutil.rmtree
        for signum in (signal.SIGTERM, signal.SIGINT):
            with self.subTest(signal=signum.name):
                signalled: list[bool] = []

                def interrupted_removal(
                    path: str,
                    *,
                    dir_fd: int,
                    ignore_errors: bool,
                    signum: int = signum,
                    signalled: list[bool] = signalled,
                ) -> None:
                    if not signalled:
                        signalled.append(True)
                        for _ in range(2):
                            os.kill(os.getpid(), signum)
                            # The handler runs at the next bytecode boundary;
                            # this gives it one before the removal goes on.
                            time.sleep(0.2)
                    remove(path, dir_fd=dir_fd, ignore_errors=ignore_errors)

                before = signal.getsignal(signum)
                with (
                    mock.patch("d1_copy.workdir.shutil.rmtree", interrupted_removal),
                    self.assertRaises(KeyboardInterrupt),
                    workdir.private_workdir() as directory,
                ):
                    (directory / "source.sql").write_text("rows", encoding="utf-8")
                self.assertEqual(signalled, [True])
                self.assertEqual(self._runs(), [])
                self.assertEqual(signal.getsignal(signum), before)

    def test_the_directory_is_private_to_the_user(self) -> None:
        with workdir.private_workdir():
            self.assertEqual(self.parent.stat().st_mode & 0o777, 0o700)

    def test_a_second_run_is_refused_while_one_holds_the_directory(self) -> None:
        """Without it the second run's sweep would delete the first one's files."""
        with workdir.private_workdir() as directory:
            with self.assertRaises(workdir.WorkdirInUse), workdir.private_workdir():
                pass
            self.assertTrue(directory.exists())


class UnsafeParentTest(unittest.TestCase):
    """The parent sits in a temporary directory other accounts can write to, so
    whatever is already at its path may be someone else's. Each case must be
    refused before the tool writes or deletes anything."""

    @override
    def setUp(self) -> None:
        scratch = tempfile.TemporaryDirectory()
        self.addCleanup(scratch.cleanup)
        self.root = pathlib.Path(scratch.name)
        self.parent = self.root / "work"
        patch = mock.patch.object(workdir, "WORK_PARENT", self.parent)
        patch.start()
        self.addCleanup(patch.stop)

    def _refused(self) -> None:
        with self.assertRaises(workdir.WorkdirUnsafe), workdir.private_workdir():
            self.fail("a run started in an unsafe working directory")

    def test_a_planted_symlink_is_refused_and_its_target_left_alone(self) -> None:
        """Followed, the sweep would delete the `run-*` entries of whatever
        directory of ours the link names."""
        victim = self.root / "mine"
        (victim / "run-keep").mkdir(parents=True)
        (victim / "run-keep" / "data").write_text("kept", encoding="utf-8")
        self.parent.symlink_to(victim)
        mode = victim.stat().st_mode
        self._refused()
        self.assertEqual((victim / "run-keep" / "data").read_text(), "kept")
        self.assertFalse((victim / "lock").exists())
        self.assertEqual(victim.stat().st_mode, mode)

    def test_a_directory_another_user_owns_is_refused(self) -> None:
        """Our dumps would land somewhere its owner can read and rearrange."""
        self.parent.mkdir(mode=0o700)
        (self.parent / "run-theirs").mkdir()
        with mock.patch("os.getuid", return_value=os.getuid() + 1):
            self._refused()
        self.assertTrue((self.parent / "run-theirs").exists())
        self.assertFalse((self.parent / "lock").exists())

    def test_a_directory_others_can_open_is_refused(self) -> None:
        self.parent.mkdir()
        self.parent.chmod(0o755)
        self._refused()
        self.assertFalse((self.parent / "lock").exists())

    def test_a_directory_this_user_cannot_open_is_refused(self) -> None:
        """Another user's private directory cannot even be opened, so its owner
        is never read; that is a refusal like the others, not a crash."""
        self.parent.mkdir(mode=0o700)
        self.parent.chmod(0)
        self.addCleanup(self.parent.chmod, 0o700)
        self._refused()

    def test_a_file_at_the_path_is_refused(self) -> None:
        self.parent.write_text("not a directory", encoding="utf-8")
        self._refused()


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

    def test_a_signal_while_held_is_recorded_and_ends_the_run(self) -> None:
        self.interrupting.hold()
        # Does not raise.
        self.interrupting.handle(signal.SIGTERM, None)
        with self.assertRaises(KeyboardInterrupt):
            self.interrupting.spend(None)

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
