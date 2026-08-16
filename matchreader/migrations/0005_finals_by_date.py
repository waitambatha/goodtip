"""Catch finals that a bad round number hid from the shape heuristic.

Found by ``manage.py backtest_scrapers --series AFL --from-season 2025``, which
replayed the season through the scraper and reported three fixtures in history
that afl.com.au never returned. Two were Opening Round games, which are real
regular-season results. The third was this:

    2025-09-06  Greater Western Sydney v Hawthorn  88-107  round_number=1

A September final, stored by Sportradar as round 1, sitting among the March
fixtures and counting towards the regular-season ladder.

``finals_rounds`` could never have caught it: that rule reads round numbers,
and this row claims to be round 1. ``finals_by_date`` reads kickoffs instead —
anything played after the last regular-season game is a final, whatever round
it says it is.
"""

from django.db import migrations

from matchreader.stages import finals_by_date, finals_rounds


def mark(apps, schema_editor):
    HistoricalMatch = apps.get_model("matchreader", "HistoricalMatch")

    by_combo: dict[tuple, list] = {}
    sizes_by_combo: dict[tuple, dict[int, int]] = {}
    for pk, series_id, season, rn, kickoff in HistoricalMatch.objects.values_list(
        "id", "series_id", "season", "round_number", "kickoff_at"
    ):
        by_combo.setdefault((series_id, season), []).append((pk, rn, kickoff))
        sizes = sizes_by_combo.setdefault((series_id, season), {})
        sizes[rn] = sizes.get(rn, 0) + 1

    doomed: list[int] = []
    for combo, rows in by_combo.items():
        finals = finals_rounds(sizes_by_combo[combo])
        doomed.extend(finals_by_date(rows, finals))

    if doomed:
        HistoricalMatch.objects.filter(id__in=doomed).update(stage="finals")


def noop(apps, schema_editor):
    """Derived flag; recomputed forwards."""


class Migration(migrations.Migration):

    dependencies = [
        ("matchreader", "0004_reclassify_stages"),
    ]

    operations = [
        migrations.RunPython(mark, noop),
    ]
