import unittest
import logging
import asyncio
import sys
from time import time, sleep

from artiq.experiment import *
from artiq.master.scheduler import Scheduler


class EmptyExperiment(EnvExperiment):
    def build(self):
        pass

    def run(self):
        pass


class BackgroundExperiment(EnvExperiment):
    def build(self):
        self.setattr_device("scheduler")

    def run(self):
        try:
            while True:
                self.scheduler.pause()
                sleep(0.2)
        except TerminationRequested:
            self.set_dataset("termination_ok", True,
                             broadcast=True, archive=False)


class CheckPauseBackgroundExperiment(EnvExperiment):
    def build(self):
        self.setattr_device("scheduler")

    def run(self):
        while True:
            while not self.scheduler.check_pause():
                sleep(0.2)
            self.scheduler.pause()


def _get_expid(name):
    return {
        "log_level": logging.WARNING,
        "file": sys.modules[__name__].__file__,
        "class_name": name,
        "arguments": dict()
    }


def _get_basic_steps(rid, expid, priority=0, flush=False):
    return [
        {"action": "setitem", "key": rid, "value":
            {"pipeline": "main", "status": "pending", "priority": priority,
             "expid": expid, "due_date": None, "flush": flush,
             "repo_msg": None},
            "path": []},
        {"action": "setitem", "key": "status", "value": "preparing",
            "path": [rid]},
        {"action": "setitem", "key": "status", "value": "prepare_done",
            "path": [rid]},
        {"action": "setitem", "key": "status", "value": "running",
            "path": [rid]},
        {"action": "setitem", "key": "status", "value": "run_done",
            "path": [rid]},
        {"action": "setitem", "key": "status", "value": "analyzing",
            "path": [rid]},
        {"action": "setitem", "key": "status", "value": "deleting",
            "path": [rid]},
        {"action": "delitem", "key": rid, "path": []}
    ]


class _RIDCounter:
    def __init__(self, next_rid):
        self._next_rid = next_rid

    def get(self):
        rid = self._next_rid
        self._next_rid += 1
        return rid


class SchedulerCase(unittest.TestCase):
    def setUp(self):
        self.loop = asyncio.new_event_loop()
        asyncio.set_event_loop(self.loop)

    def test_steps(self):
        loop = self.loop
        scheduler = Scheduler(_RIDCounter(0), dict(), None, None)
        expid = _get_expid("EmptyExperiment")

        expect = _get_basic_steps(1, expid)
        done = asyncio.Event()
        expect_idx = 0
        def notify(mod):
            nonlocal expect_idx
            self.assertEqual(mod, expect[expect_idx])
            expect_idx += 1
            if expect_idx >= len(expect):
                done.set()
        scheduler.notifier.publish = notify

        scheduler.start()

        # Verify that a timed experiment far in the future does not
        # get run, even if it has high priority.
        late = time() + 100000
        expect.insert(0,
            {"action": "setitem", "key": 0, "value":
                {"pipeline": "main", "status": "pending", "priority": 99,
                 "expid": expid, "due_date": late, "flush": False,
                 "repo_msg": None},
             "path": []})
        scheduler.submit("main", expid, 99, late, False)

        # This one (RID 1) gets run instead.
        scheduler.submit("main", expid, 0, None, False)

        loop.run_until_complete(done.wait())
        scheduler.notifier.publish = None
        loop.run_until_complete(scheduler.stop())

    def test_pending_priority(self):
        """Check due dates take precedence over priorities when waiting to
        prepare."""
        loop = self.loop
        handlers = {}
        scheduler = Scheduler(_RIDCounter(0), handlers, None, None)
        handlers["scheduler_check_pause"] = scheduler.check_pause

        expid_empty = _get_expid("EmptyExperiment")

        expid_bg = _get_expid("CheckPauseBackgroundExperiment")
        # Suppress the SystemExit backtrace when worker process is killed.
        expid_bg["log_level"] = logging.CRITICAL

        high_priority = 3
        middle_priority = 2
        low_priority = 1
        late = time() + 100000
        early = time() + 1

        expect_rid2 = _get_basic_steps(2, expid_empty, middle_priority)
        expect_rid2[0]["value"].update(due_date=early)
        rid0_paused = asyncio.Event()
        rid1_running = asyncio.Event()
        rid2_running = asyncio.Event()
        done = asyncio.Event()
        expect_rid2_idx = 0

        def notify(mod):
            nonlocal expect_rid2_idx
            if mod == {"path": [0],
                       "value": "paused",
                       "key": "status",
                       "action": "setitem"}:
                rid0_paused.set()
            if mod == {"path": [1],
                       "value": "running",
                       "key": "status",
                       "action": "setitem"}:
                rid1_running.set()
            if mod == {"path": [2],
                       "value": "running",
                       "key": "status",
                       "action": "setitem"}:
                rid2_running.set()
            if mod["path"] == [2] or (mod["path"] == [] and mod["key"] == 2):
                self.assertEqual(mod, expect_rid2[expect_rid2_idx])
                expect_rid2_idx += 1
            if expect_rid2_idx >= len(expect_rid2):
                done.set()
        scheduler.notifier.publish = notify

        async def expect_paused_running():
            rid1_running_future = asyncio.ensure_future(rid1_running.wait())
            rid2_running_future = asyncio.ensure_future(rid2_running.wait())
            # expect RID 0 paused -> RID 2 running
            await rid0_paused.wait()
            done, pending = await asyncio.wait(
                [rid1_running_future, rid2_running_future],
                return_when=asyncio.FIRST_COMPLETED
            )
            assert rid2_running_future in done and \
                   rid1_running_future in pending
            for task in pending:
                task.cancel()

        scheduler.start()

        scheduler.submit("main", expid_bg, low_priority)
        scheduler.submit("main", expid_empty, high_priority, late)
        scheduler.submit("main", expid_empty, middle_priority, early)

        timeout = 5
        try:
            loop.run_until_complete(
                asyncio.wait_for(expect_paused_running(), timeout)
            )
        except asyncio.TimeoutError:
            raise AssertionError(
                f"expect_paused_running() did not complete within {timeout}s"
            )
        loop.run_until_complete(done.wait())
        scheduler.notifier.publish = None
        loop.run_until_complete(scheduler.stop())

    def test_pause(self):
        loop = self.loop

        termination_ok = False
        def check_termination(mod):
            nonlocal termination_ok
            self.assertEqual(
                mod,
                {"action": "setitem", "key": "termination_ok",
                 "value": (False, True), "path": []})
            termination_ok = True
        handlers = {
            "update_dataset": check_termination
        }
        scheduler = Scheduler(_RIDCounter(0), handlers, None, None)

        expid_bg = _get_expid("BackgroundExperiment")
        expid = _get_expid("EmptyExperiment")

        expect = _get_basic_steps(1, expid)
        background_running = asyncio.Event()
        empty_ready = asyncio.Event()
        empty_completed = asyncio.Event()
        background_completed = asyncio.Event()
        expect_idx = 0
        def notify(mod):
            nonlocal expect_idx
            if mod == {"path": [0],
                       "value": "running",
                       "key": "status",
                       "action": "setitem"}:
                background_running.set()
            if mod == {"path": [0],
                       "value": "deleting",
                       "key": "status",
                       "action": "setitem"}:
                background_completed.set()
            if mod == {"path": [1],
                       "value": "prepare_done",
                       "key": "status",
                       "action": "setitem"}:
                empty_ready.set()
            if mod["path"] == [1] or (mod["path"] == [] and mod["key"] == 1):
                self.assertEqual(mod, expect[expect_idx])
                expect_idx += 1
                if expect_idx >= len(expect):
                    empty_completed.set()
        scheduler.notifier.publish = notify

        scheduler.start()
        scheduler.submit("main", expid_bg, -99, None, False)
        loop.run_until_complete(background_running.wait())
        self.assertFalse(scheduler.check_pause(0))
        scheduler.submit("main", expid, 0, None, False)
        self.assertFalse(scheduler.check_pause(0))
        loop.run_until_complete(empty_ready.wait())
        self.assertTrue(scheduler.check_pause(0))
        loop.run_until_complete(empty_completed.wait())
        self.assertFalse(scheduler.check_pause(0))

        self.assertFalse(termination_ok)
        scheduler.request_termination(0)
        self.assertTrue(scheduler.check_pause(0))
        loop.run_until_complete(background_completed.wait())
        self.assertTrue(termination_ok)

        loop.run_until_complete(scheduler.stop())

    def test_close_with_active_runs(self):
        """Check scheduler exits with experiments still running"""
        loop = self.loop

        scheduler = Scheduler(_RIDCounter(0), {}, None, None)

        expid_bg = _get_expid("BackgroundExperiment")
        # Suppress the SystemExit backtrace when worker process is killed.
        expid_bg["log_level"] = logging.CRITICAL
        expid = _get_expid("EmptyExperiment")

        background_running = asyncio.Event()
        empty_ready = asyncio.Event()
        background_completed = asyncio.Event()
        def notify(mod):
            if mod == {"path": [0],
                       "value": "running",
                       "key": "status",
                       "action": "setitem"}:
                background_running.set()
            if mod == {"path": [0],
                       "value": "deleting",
                       "key": "status",
                       "action": "setitem"}:
                background_completed.set()
            if mod == {"path": [1],
                       "value": "prepare_done",
                       "key": "status",
                       "action": "setitem"}:
                empty_ready.set()
        scheduler.notifier.publish = notify

        scheduler.start()
        scheduler.submit("main", expid_bg, -99, None, False)
        loop.run_until_complete(background_running.wait())

        scheduler.submit("main", expid, 0, None, False)
        loop.run_until_complete(empty_ready.wait())

        # At this point, (at least) BackgroundExperiment is still running; make
        # sure we can stop the scheduler without hanging.
        loop.run_until_complete(scheduler.stop())

    def test_flush(self):
        loop = self.loop
        scheduler = Scheduler(_RIDCounter(0), dict(), None, None)
        expid = _get_expid("EmptyExperiment")

        expect = _get_basic_steps(1, expid, 1, True)
        expect.insert(1, {"key": "status",
                          "path": [1],
                          "value": "flushing",
                          "action": "setitem"})
        first_preparing = asyncio.Event()
        done = asyncio.Event()
        expect_idx = 0
        def notify(mod):
            nonlocal expect_idx
            if mod == {"path": [0],
                       "value": "preparing",
                       "key": "status",
                       "action": "setitem"}:
                first_preparing.set()
            if mod["path"] == [1] or (mod["path"] == [] and mod["key"] == 1):
                self.assertEqual(mod, expect[expect_idx])
                expect_idx += 1
                if expect_idx >= len(expect):
                    done.set()
        scheduler.notifier.publish = notify

        scheduler.start()
        scheduler.submit("main", expid, 0, None, False)
        loop.run_until_complete(first_preparing.wait())
        scheduler.submit("main", expid, 1, None, True)
        loop.run_until_complete(done.wait())
        loop.run_until_complete(scheduler.stop())

    def tearDown(self):
        self.loop.close()
