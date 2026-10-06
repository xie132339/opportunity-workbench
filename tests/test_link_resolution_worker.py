import unittest
from unittest.mock import Mock

from services.link_resolution_worker import run_periodically


class StopAfterWait:
    def __init__(self):
        self.stopped = False
        self.waits = []

    def is_set(self):
        return self.stopped

    def wait(self, seconds):
        self.waits.append(seconds)
        self.stopped = True
        return True


class LinkResolutionWorkerTests(unittest.TestCase):
    def test_scheduler_waits_only_for_the_remaining_minute(self):
        stop = StopAfterWait()
        cycle = Mock(return_value={"resolved": 10})
        report = Mock()
        clock = Mock(side_effect=[20, 35])

        run_periodically(stop, cycle=cycle, clock=clock, report=report)

        cycle.assert_called_once_with()
        report.assert_called_once_with({"public_links": {"resolved": 10}})
        self.assertEqual(stop.waits, [45])

    def test_slow_cycle_does_not_add_an_extra_full_minute_delay(self):
        stop = StopAfterWait()
        clock = Mock(side_effect=[20, 95])

        run_periodically(stop, cycle=Mock(return_value={}), clock=clock, report=Mock())

        self.assertEqual(stop.waits, [0])

    def test_cycle_errors_are_reported_and_scheduler_continues(self):
        stop = StopAfterWait()
        report = Mock()

        run_periodically(
            stop,
            cycle=Mock(side_effect=RuntimeError("temporary database lock")),
            clock=Mock(side_effect=[1, 2]),
            report=report,
        )

        self.assertEqual(report.call_args.args[0]["public_links_error"], "temporary database lock")
        self.assertEqual(stop.waits, [59])
