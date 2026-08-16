"""Deduplicate the history, then make the duplication impossible.

AFL 2024 held every fixture twice — once under Sportradar ids
("sr:sport_event:45559680") and once under afl.com.au ids ("CD_M20240140101")
— because the only uniqueness rule was (series, external_id), which cannot see
that two id schemes describe the same game. 422 rows for a 207-game season,
and MatchReader was fitted on the doubled history.

Order matters here: the duplicates have to go BEFORE the natural-key constraint
is applied, or the AlterUniqueTogether fails on existing data.
"""

from django.db import migrations, models


def dedupe(apps, schema_editor):
    """Collapse each real fixture to one row.

    Which copy survives: the one whose external_id is NOT a Sportradar id.
    Sportradar is being removed as a source, so its ids will never be looked up
    again, while a scraper id stays meaningful to a re-run of the backfill. If
    every copy is Sportradar's (a season only it ever fetched), the earliest
    row wins and keeps its id — the fixture is still correct, it just carries an
    id nothing will match on, which the backfill repairs on its next pass.
    """
    HistoricalMatch = apps.get_model("matchreader", "HistoricalMatch")

    seen: dict[tuple, int] = {}
    doomed: list[int] = []
    rows = HistoricalMatch.objects.order_by("id").values_list(
        "id", "series_id", "season", "round_number", "home_team_id",
        "away_team_id", "external_id",
    )
    for pk, series_id, season, rn, home_id, away_id, ext in rows:
        key = (series_id, season, rn, home_id, away_id)
        if key not in seen:
            seen[key] = pk
            continue
        # A duplicate. Keep whichever copy has the more durable id.
        kept_pk = seen[key]
        kept_ext = HistoricalMatch.objects.get(pk=kept_pk).external_id
        if kept_ext.startswith("sr:") and not ext.startswith("sr:"):
            doomed.append(kept_pk)
            seen[key] = pk
        else:
            doomed.append(pk)

    if doomed:
        HistoricalMatch.objects.filter(id__in=doomed).delete()


def noop(apps, schema_editor):
    """Deletions are not restorable, and re-running the backfill is how you
    would recover the rows anyway. Reversible in the sense that the schema goes
    back; the duplicates do not, which is the point."""


class Migration(migrations.Migration):

    dependencies = [
        ('catalog', '0010_departmenttype'),
        ('matchreader', '0001_initial'),
        ('tipping', '0009_ladderentry_byes_ladderadjustment'),
    ]

    operations = [
        migrations.AddField(
            model_name='historicalmatch',
            name='stage',
            field=models.CharField(choices=[('regular', 'Regular season'), ('finals', 'Finals')], default='regular', max_length=10),
        ),
        migrations.RunPython(dedupe, noop),
        migrations.AlterUniqueTogether(
            name='historicalmatch',
            unique_together={('series', 'external_id'), ('series', 'season', 'round_number', 'home_team', 'away_team')},
        ),
    ]
