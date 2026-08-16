"""Re-run the finals classification with the lone-short-round rule.

0003 walked backwards from the last round marking anything under half a full
slate as finals, and flagged NRL 2026's newest round — one completed game of a
round still being played — as a finals week. Left alone that would have dropped
the current round out of the ladder every week of the season.

``finals_rounds`` now requires at least two consecutive trailing short rounds,
which every real finals series has and an in-progress round never does. This
reclassifies from scratch rather than patching the one season, so the stored
flags always match the current rule.
"""

from django.db import migrations

from matchreader.stages import finals_rounds


def reclassify(apps, schema_editor):
    HistoricalMatch = apps.get_model("matchreader", "HistoricalMatch")

    sizes_by_combo: dict[tuple, dict[int, int]] = {}
    for series_id, season, rn in HistoricalMatch.objects.values_list(
        "series_id", "season", "round_number"
    ):
        sizes = sizes_by_combo.setdefault((series_id, season), {})
        sizes[rn] = sizes.get(rn, 0) + 1

    for (series_id, season), sizes in sizes_by_combo.items():
        finals = finals_rounds(sizes)
        base = HistoricalMatch.objects.filter(series_id=series_id, season=season)
        # Both directions, so a round that stops being finals is put back.
        base.filter(round_number__in=finals).update(stage="finals")
        base.exclude(round_number__in=finals).update(stage="regular")


def noop(apps, schema_editor):
    """Derived flags; recomputed by this same function on the way forward."""


class Migration(migrations.Migration):

    dependencies = [
        ("matchreader", "0003_dedupe_by_date_and_set_stage"),
    ]

    operations = [
        migrations.RunPython(reclassify, noop),
    ]
