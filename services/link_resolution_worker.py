"""Run public product-link checks independently of slow source scans."""
import time

from link_resolution import run_cycle


def run_periodically(stop_event, interval_seconds=60, cycle=None, clock=None, report=None):
    """Resolve a bounded batch each minute without waiting for the source scan loop."""
    cycle = cycle or run_cycle
    clock = clock or time.monotonic
    report = report or (lambda result: print(result, flush=True))
    while not stop_event.is_set():
        started = clock()
        try:
            report({"public_links": cycle()})
        except Exception as exc:
            report({"public_links_error": str(exc)[:240]})
        remaining = max(0, interval_seconds - (clock() - started))
        if stop_event.wait(remaining):
            return
