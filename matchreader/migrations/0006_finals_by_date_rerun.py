"""Re-run the date-based finals marking, now that it anchors correctly.

0005 asked whether a game sat more than a fortnight past the end of the regular
season. AFL 2025's finals began thirteen days after round 24, so it marked
nothing and the 6 September final stayed on the ladder.

``finals_by_date`` now anchors on the finals window itself — the earliest
kickoff among the rounds already identified as finals — instead of guessing a
gap. No regular-season game is played once that window opens.
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
        ("matchreader", "0005_finals_by_date"),
    ]

    operations = [
        migrations.RunPython(mark, noop),
    ]
