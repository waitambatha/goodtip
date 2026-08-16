"""Optional traffic-driven fallback for the feed syncs. OFF by default.

The real scheduler is a systemd timer calling ``manage.py run_due_syncs`` every
two minutes — see deploy/systemd/. That runs on the server's clock, so the data
is current whether or not anybody is on the site, which is the only correct
behaviour: a visitor arriving at 3am must find last night's results already in,
not trigger the fetch that gets them.

What is left here is a net for the case where the timer is not installed at all.
It is disabled unless AUTOSYNC_ENABLED=True precisely because it is the wrong
mechanism to rely on — it cannot sync a site with no visitors, and its work runs
on a web worker thread, which dies mid-sync whenever the service restarts.

``claim()`` is the shared piece and is used by the timer path too: a single
conditional UPDATE against SyncSchedule, so overlapping runs from any source can
never double-hit a feed.
"""
from __future__ import annotations

import logging
import threading
import time
from datetime import timedelta

from django.core.management import call_command
from django.db import connections, transaction
from django.utils import timezone

logger = logging.getLogger("data_sync")

# How often each kind should run, in seconds. Mirrors the cron cadences.
#
# These are the FAST kinds: they work from a window around today, finish in
# seconds to a couple of minutes, and share the two-minute scheduler tick.
INTERVALS = {
    "live": 120,       # in-play score and clock
    "results": 900,    # final scores; this is what grades tips
    "fixtures": 3600,  # discovery: new rounds and games, around today
    "ladder": 1800,    # standings only move when a game finishes
}

# The full-season sweep, on its own cadence and its own systemd unit.
#
# Every kind above works from a window around today, which keeps this week
# correct and can never repair a round that fell outside the window when it
# mattered — the live database held AFL 2026 rounds 1-5 and 21-25 with a
# permanent hole in between for exactly that reason. This one asks each feed
# for every round it publishes, so a hole closes itself within six hours
# instead of never.
#
# It is kept OUT of INTERVALS deliberately. run_due_syncs runs its kinds
# sequentially in one oneshot process, so a slow sweep sharing that unit would
# hold up the two-minute live poller behind it for as long as it ran. Separate
# unit, separate timeout, separate failure in the journal.
SLOW_INTERVALS = {
    "backfill": 21600,
}

# Everything claimable, fast and slow. claim() reads this; the schedulers read
# the two halves separately.
ALL_INTERVALS = {**INTERVALS, **SLOW_INTERVALS}

# Process-local throttle: the cheapest check is the one that never reaches the
# database. Nothing can be due sooner than the shortest interval, so there is no
# point asking before then.
_MIN_INTERVAL = min(INTERVALS.values())
_last_looked = 0.0
_look_lock = threading.Lock()
_running: set[str] = set()


def claim(kind: str) -> bool:
    """Atomically take ownership of the next run of `kind`.

    A single UPDATE … WHERE next_run_at <= now is the whole lock. Two workers
    racing produce one row updated and one not, with no window between reading
    and writing for both to pass — which a get-then-save would have.
    """
    from .models import SyncSchedule

    now = timezone.now()
    interval = ALL_INTERVALS[kind]
    with transaction.atomic():
        claimed = (
            SyncSchedule.objects.filter(kind=kind, next_run_at__lte=now)
            .update(next_run_at=now + timedelta(seconds=interval),
                    last_started_at=now)
        )
    return bool(claimed)


def stamp(kind: str) -> None:
    """Mark a kind as just-run, moving it to its next due time.

    For work that runs from its OWN systemd timer rather than through the
    run_due_syncs dispatcher — the full-season backfill — and so never goes
    near ``claim``. Without this its SyncSchedule row would sit at a due time
    that had long passed, and the admin panel's "next due" column would report
    a sweep as permanently overdue while it was in fact running on schedule.
    """
    from .models import SyncSchedule

    now = timezone.now()
    SyncSchedule.objects.filter(kind=kind).update(
        next_run_at=now + timedelta(seconds=ALL_INTERVALS[kind]),
        last_started_at=now,
    )


def _run(kind: str) -> None:
    """Run one sync on a worker thread, then let go of the connection.

    Django opens a connection per thread and will not close it for us; a
    long-lived process spawning these would leak one per run until the database
    refuses new ones.
    """
    try:
        if kind == "fixtures":
            call_command("sync_matches", fixtures=True, verbosity=0)
        else:
            call_command("sync_matches", **{kind: True}, verbosity=0)
    except Exception:
        # A feed outage must never surface as a 500 on someone's dashboard.
        # SyncRun already records the failure; this is the last resort.
        logger.exception("autosync %s failed", kind)
    finally:
        _running.discard(kind)


def _claim_and_run() -> None:
    """Decide what is due and run it. Entirely off the request thread."""
    try:
        for kind in INTERVALS:
            if kind in _running:
                continue
            try:
                if not claim(kind):
                    continue
            except Exception:
                # Table missing (pre-migrate), database blip — autosync is an
                # optimisation and must never break anything.
                logger.debug("autosync claim failed for %s", kind, exc_info=True)
                return
            _running.add(kind)
            _run(kind)
    finally:
        connections.close_all()


def maybe_run() -> None:
    """Called once per request. Returns in microseconds.

    The claim queries are deliberately NOT done here. This app's database is
    remote — roughly 370ms per round trip — so three claims cost over a second,
    and although the response has already been sent by this point, the request
    thread is still occupied and cannot pick up the next request. Under load
    that turns a background nicety into a throughput ceiling. So the request
    thread does nothing but check a float and hand off.
    """
    global _last_looked

    now = time.monotonic()
    with _look_lock:
        if now - _last_looked < _MIN_INTERVAL:
            return
        _last_looked = now

    threading.Thread(target=_claim_and_run, daemon=True, name="autosync").start()


class AutoSyncMiddleware:
    """Hangs autosync off ordinary traffic.

    Deliberately runs AFTER the response is produced, so even the claim query
    is off the path a visitor waits on.
    """

    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        response = self.get_response(request)
        from django.conf import settings

        if getattr(settings, "AUTOSYNC_ENABLED", False):
            try:
                maybe_run()
            except Exception:
                logger.debug("autosync skipped", exc_info=True)
        return response
