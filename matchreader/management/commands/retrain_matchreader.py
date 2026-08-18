"""Refit MatchReader for every series that needs it. Safe to run on a tick.

WHY THIS EXISTS
``train_matchreader`` fits ONE series and has to be told to. It had never been
scheduled anywhere — not in a timer, not in run_due_jobs, not in the deploy —
so the only models that existed were the ones somebody typed a command for on
13 August. Two consequences, both live at the time this was written:

  * NRL and AFLW had no active model at all, so those competitions showed no
    prediction on any fixture. Not a degraded read — nothing.
  * The two models that did exist were frozen at a fortnight of results and
    would drift further from the season every week it ran.

A tipping assistant that silently stops learning is worse than one that was
never offered, because the screen goes on presenting its verdict with the same
confidence.

WHEN IT ACTUALLY REFITS
Self-throttling, so it can sit in the ten-minute job loop and cost nothing:

  * a series with NO active model is fitted immediately — that is a hole, not
    a refresh, and waiting a week to fill it helps nobody;
  * otherwise only when BOTH the model is older than --min-days AND new
    completed games have been stored since it was fitted. Refitting on an
    unchanged history burns time to produce identical coefficients.

ACTIVATION IS EARNED, NOT AUTOMATIC
Delegated to train_matchreader's own rule: a new fit goes live only if it beats
always-picking-the-home-side. A model that cannot clear that bar is worse than
a rule anybody can apply for free, so it is stored for the record and left
inactive. That also makes this job safe to run unattended — the worst case is
a stored version nobody uses, never a live model that got worse.
"""

from __future__ import annotations

from datetime import timedelta

from django.core.management import call_command
from django.core.management.base import BaseCommand
from django.utils import timezone

from matchreader.models import HistoricalMatch, ModelVersion

#: Below this, a fit cannot be meaningfully validated — there is no test season
#: left once anything is held back. State of Origin sits here permanently.
MIN_GAMES = 60


class Command(BaseCommand):
    help = "Refit MatchReader for any series whose model is missing or stale."

    def add_arguments(self, parser):
        parser.add_argument(
            "--min-days", type=int, default=7,
            help="Don't refit a series whose model is newer than this. Default 7.",
        )
        parser.add_argument(
            "--force", action="store_true",
            help="Refit every series regardless of age or new results.",
        )

    def handle(self, *args, **opts):
        now = timezone.now()
        cutoff = now - timedelta(days=opts["min_days"])
        # Only series we actually hold results for. Asking for a fit on a
        # series with no history raises, and there is nothing to learn from.
        names = sorted(set(
            HistoricalMatch.objects.values_list("series__name", flat=True).distinct()
        ))

        refit, skipped = [], []
        for name in names:
            # A series with barely any history cannot produce a model that
            # beats the baseline, and never will until the results arrive.
            # Without this floor, State of Origin — three games a year, two
            # stored — is re-attempted on every single tick forever: a fit that
            # fails, a line of stderr, and nothing to show for it. The job has
            # to be a genuine no-op when there is nothing to do, or it is just
            # a slower way of doing nothing.
            history = HistoricalMatch.objects.filter(series__name=name)
            if history.count() < MIN_GAMES:
                skipped.append(f"{name} (only {history.count()} games stored)")
                continue
            latest = (
                ModelVersion.objects.filter(series__name=name)
                .order_by("-created_at").first()
            )
            active = ModelVersion.objects.filter(series__name=name, is_active=True).exists()

            if opts["force"] or not active:
                reason = "forced" if opts["force"] else "no active model"
            elif latest and latest.created_at > cutoff:
                skipped.append(f"{name} (fitted {latest.created_at:%d %b})")
                continue
            else:
                # Stale — but only worth refitting if the history moved. The
                # coefficients are a function of the results; unchanged results
                # give an identical model and a wasted row.
                since = latest.created_at if latest else None
                fresh = HistoricalMatch.objects.filter(series__name=name)
                if since:
                    fresh = fresh.filter(kickoff_at__gt=since)
                if not fresh.exists():
                    skipped.append(f"{name} (no new results)")
                    continue
                reason = f"{fresh.count()} new result(s)"

            self.stdout.write(f"{name}: refitting — {reason}")
            before = ModelVersion.objects.filter(series__name=name).count()
            try:
                call_command(
                    "train_matchreader", series=name, activate=True, verbosity=0,
                )
                # train_matchreader catches its own errors and reports them
                # rather than raising, so "call_command returned" does not mean
                # "a model was fitted". Counting versions is what actually
                # distinguishes the two — otherwise this reports a refit of a
                # series that produced nothing, which is the one thing a
                # monitoring line must never do.
                if ModelVersion.objects.filter(series__name=name).count() > before:
                    refit.append(name)
                else:
                    skipped.append(f"{name} (fit produced nothing)")
            except Exception as e:                      # noqa: BLE001
                # One series failing to fit must not stop the others. A model
                # is an enhancement; the app tips fine without it.
                self.stderr.write(f"  {name}: {e}")

        if refit:
            self.stdout.write(self.style.SUCCESS(f"Refitted: {', '.join(refit)}."))
        else:
            self.stdout.write(f"Nothing to refit. {'; '.join(skipped) or 'no history stored'}")
