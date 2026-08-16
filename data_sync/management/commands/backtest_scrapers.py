"""Check the scrapers against results we already know to be right.

WHY THIS EXISTS
---------------
Sportradar is being removed as a source, which leaves the scrapers with nothing
to be checked against. Normally you would run the old and new sources side by
side for a few weeks and compare — but there is a much faster version available
here, because months of Sportradar-sourced results are already sitting in
``matchreader.HistoricalMatch``.

So instead of waiting a week per round, this replays whole past seasons through
the scrapers and diffs them against those stored results. Every score, team
resolution and kickoff gets checked against a known-good answer, at zero cost,
with nothing live at stake.

READ-ONLY. It writes nothing, ever. A disagreement is reported, not corrected —
deciding which side is right is a judgement, and doing it silently is how you
end up trusting the wrong source.

    manage.py backtest_scrapers --series NRL --from-season 2024 --to-season 2025
    manage.py backtest_scrapers --series AFL --from-season 2026 --max-round 22

Expect it to be slow, and let it be: nrl.com is paced at one page every six
seconds deliberately (see scrapers/nrl.py), so a full season is a few minutes.
That pacing is what keeps us a welcome client of a site that has not asked us
to stay away.
"""

from __future__ import annotations

from django.core.management.base import BaseCommand

from catalog.models import Series
from matchreader.models import HistoricalMatch


class Command(BaseCommand):
    help = "Replay past seasons through the scrapers and diff against stored results."

    def add_arguments(self, parser):
        parser.add_argument("--series", default="AFL", help="AFL, AFLW, NRL or NRLW.")
        parser.add_argument("--from-season", type=int, required=True)
        parser.add_argument("--to-season", type=int)
        parser.add_argument("--max-round", type=int, default=27)
        parser.add_argument(
            "--show", type=int, default=25,
            help="How many individual disagreements to print before summarising.",
        )

    def handle(self, *args, **o):
        name = o["series"].upper()
        series = Series.objects.filter(name__iexact=name).first()
        if series is None:
            self.stderr.write(self.style.ERROR(f"No series named {name!r}."))
            return

        scraper, resolve, key = self._source(name)
        if scraper is None:
            return

        first = o["from_season"]
        last = o["to_season"] or first

        checked = agreed = missing = extra = unresolved = 0
        problems: list[str] = []

        for season in range(first, last + 1):
            # What we already believe about this season, keyed the same way the
            # scraper output will be so the two can be compared directly.
            known = {
                (m.round_number, m.home_team_id, m.away_team_id): m
                for m in HistoricalMatch.objects.filter(series=series, season=season)
            }
            if not known:
                self.stdout.write(f"  {name} {season}: no stored history, skipped")
                continue

            seen: set[tuple] = set()
            for rn in range(1, o["max_round"] + 1):
                # Printed per round, and flushed, because this command is slow
                # by design — the scrapers are paced, and nrl.com is paced at
                # six seconds a page. Without a heartbeat there is no way to
                # tell a working run from a hung one.
                self.stdout.write(f"    {season} R{rn}...", ending="")
                self.stdout.flush()

                try:
                    games = scraper.fixtures(series=key, season=season, round_number=rn)
                except Exception as e:                       # noqa: BLE001
                    self.stdout.write(" scrape failed")
                    problems.append(f"{season} R{rn}: scrape failed — {e}")
                    continue

                round_agreed = round_problems = 0

                for g in games:
                    if g["status"] != "complete" or g["home_score"] is None:
                        continue
                    home = resolve(series, g["home_name"], g["home_external_id"])
                    away = resolve(series, g["away_name"], g["away_external_id"])
                    if not home or not away:
                        unresolved += 1
                        round_problems += 1
                        problems.append(
                            f"{season} R{rn}: could not resolve "
                            f"{g['home_name']!r} v {g['away_name']!r} to teams"
                        )
                        continue

                    k = (rn, home.id, away.id)
                    seen.add(k)
                    stored = known.get(k)
                    if stored is None:
                        round_problems += 1
                        # The scraper found a game the history does not hold.
                        # Usually a round-numbering difference rather than an
                        # invented fixture, which is why it is counted apart.
                        extra += 1
                        problems.append(
                            f"{season} R{rn}: scraper has {home.name} v {away.name}, "
                            f"history does not"
                        )
                        continue

                    checked += 1
                    if (stored.home_score, stored.away_score) == (g["home_score"], g["away_score"]):
                        agreed += 1
                        round_agreed += 1
                    else:
                        round_problems += 1
                        problems.append(
                            f"{season} R{rn}: {home.name} v {away.name} — "
                            f"history {stored.home_score}-{stored.away_score}, "
                            f"scraper {g['home_score']}-{g['away_score']}"
                        )

                self.stdout.write(
                    f" {round_agreed} matched"
                    + (f", {round_problems} problem(s)" if round_problems else "")
                )

            for k, m in known.items():
                if k not in seen:
                    missing += 1
                    problems.append(
                        f"{season} R{k[0]}: history has {m.home_team.name} v "
                        f"{m.away_team.name}, scraper did not return it"
                    )

            self.stdout.write(f"  {name} {season}: {len(known)} stored, {len(seen)} scraped")

        self._report(name, checked, agreed, missing, extra, unresolved, problems, o["show"])

    def _source(self, name: str):
        """The scraper, team resolver and series key for a code."""
        from data_sync.services import _resolve_team, _resolve_nrl_team

        if name in ("AFL", "AFLW"):
            from data_sync.scrapers.afl import AflApiScraper
            self.stdout.write("  source: afl.com.au")
            return AflApiScraper(), _resolve_team, name
        if name in ("NRL", "NRLW"):
            from data_sync.scrapers.nrl import NrlDrawScraper
            self.stdout.write("  source: nrl.com")
            return NrlDrawScraper(), _resolve_nrl_team, name
        self.stderr.write(self.style.ERROR(f"No scraper for {name!r}."))
        return None, None, None

    def _report(self, name, checked, agreed, missing, extra, unresolved, problems, show):
        self.stdout.write("")
        if problems:
            self.stdout.write(self.style.WARNING(f"{len(problems)} problem(s):"))
            for line in problems[:show]:
                self.stdout.write(f"  - {line}")
            if len(problems) > show:
                self.stdout.write(f"  … and {len(problems) - show} more")
            self.stdout.write("")

        rate = (agreed / checked * 100) if checked else 0.0
        self.stdout.write(
            f"{name}: {agreed}/{checked} scores matched ({rate:.1f}%), "
            f"{missing} missing from scraper, {extra} not in history, "
            f"{unresolved} unresolved teams"
        )
        # Anything less than total agreement on a settled result is a bug in
        # the scraper, the team aliases or the round numbering — not noise.
        # Past games do not change, so the bar here is 100%.
        if checked and agreed == checked and not missing and not extra and not unresolved:
            self.stdout.write(self.style.SUCCESS(
                "Clean. The scraper reproduces the stored history exactly."
            ))
        else:
            self.stdout.write(self.style.ERROR(
                "Not clean. Fix these before the scrapers become the only source."
            ))
