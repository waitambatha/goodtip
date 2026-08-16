"""The duplicates the natural key could not catch, and the finals marker.

0002 collapsed rows that agreed on (series, season, round, teams). Six pairs
survived it, because the two sources disagreed about the ROUND:

    AFL 2024 finals   afl.com.au: round 25    Sportradar: round 1

Sportradar restarts its round numbering for the finals series. Left alone,
those games would have been counted as regular-season round 1 results and
credited to the ladder — the precise failure the stage field exists to stop.

Two teams do not meet twice on the same day, so (series, season, teams, date)
identifies the fixture whatever either source calls the round.
"""

from django.db import migrations


def dedupe_by_date(apps, schema_editor):
    """Second pass: same two clubs, same day, therefore the same game."""
    HistoricalMatch = apps.get_model("matchreader", "HistoricalMatch")

    seen: dict[tuple, tuple[int, str]] = {}
    doomed: list[int] = []
    for pk, series_id, season, home_id, away_id, kickoff, ext in (
        HistoricalMatch.objects.order_by("id").values_list(
            "id", "series_id", "season", "home_team_id", "away_team_id",
            "kickoff_at", "external_id",
        )
    ):
        key = (series_id, season, home_id, away_id, kickoff.date())
        if key not in seen:
            seen[key] = (pk, ext)
            continue
        kept_pk, kept_ext = seen[key]
        # Keep the scraper's row: it is the source that survives, and it is
        # also the one whose round numbering is right.
        if kept_ext.startswith("sr:") and not ext.startswith("sr:"):
            doomed.append(kept_pk)
            seen[key] = (pk, ext)
        else:
            doomed.append(pk)

    if doomed:
        HistoricalMatch.objects.filter(id__in=doomed).delete()


def set_stage(apps, schema_editor):
    """Mark the trailing short rounds of each season as finals.

    Derived from the shape of the season rather than a hardcoded round count,
    because that count changes: the AFL ran 22 rounds, then 23, then added an
    Opening Round, and the NRL has moved between 24 and 27.

    A finals round is small — four games, then two, then one — where a regular
    round is a full slate. So: take the largest round in the season, and walk
    backwards from the last round marking anything under half that size, until
    a full-sized round stops the walk. The walk is what makes it safe; a
    bye-thinned round mid-season is never reached.
    """
    HistoricalMatch = apps.get_model("matchreader", "HistoricalMatch")

    # One query for the whole table rather than two per season. The database
    # this runs against is remote and every round trip costs ~370ms, which
    # turns forty small queries into an alarmingly long-looking migration.
    sizes_by_combo: dict[tuple, dict[int, int]] = {}
    for series_id, season, rn in HistoricalMatch.objects.values_list(
        "series_id", "season", "round_number"
    ):
        sizes = sizes_by_combo.setdefault((series_id, season), {})
        sizes[rn] = sizes.get(rn, 0) + 1

    for (series_id, season), sizes in sizes_by_combo.items():
        if not sizes:
            continue

        threshold = max(sizes.values()) / 2
        finals: list[int] = []
        for rn in sorted(sizes, reverse=True):
            if sizes[rn] < threshold:
                finals.append(rn)
            else:
                break

        if finals:
            HistoricalMatch.objects.filter(
                series_id=series_id, season=season, round_number__in=finals
            ).update(stage="finals")


def noop(apps, schema_editor):
    """Schema reverses; deleted rows and derived flags do not. Re-run the
    backfill to rebuild them."""


class Migration(migrations.Migration):

    dependencies = [
        ("matchreader", "0002_alter_historicalmatch_unique_together_and_more"),
    ]

    operations = [
        migrations.RunPython(dedupe_by_date, noop),
        migrations.RunPython(set_stage, noop),
    ]
