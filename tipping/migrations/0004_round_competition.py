from django.db import migrations, models
import django.db.models.deletion


def backfill_round_competition(apps, schema_editor):
    """Key each existing round to the competition that includes its series for the
    league's season (e.g. an NRLW round -> 'NRL (2026)')."""
    Round = apps.get_model("tipping", "Round")
    Competition = apps.get_model("catalog", "Competition")
    for rnd in Round.objects.select_related("series", "org"):
        comp = Competition.objects.filter(series=rnd.series, season=rnd.org.season).first()
        if comp is not None:
            rnd.competition = comp
            rnd.save(update_fields=["competition"])


class Migration(migrations.Migration):

    dependencies = [
        ("catalog", "0004_competition_sport_codes"),
        ("tipping", "0003_series_and_scoring"),
    ]

    operations = [
        # Free the stale competition-named FK index left on round/team after
        # 0003 renamed those fields to `series` (Postgres keeps old index names).
        # Idempotent — skips anything already renamed. See catalog/0004.
        migrations.RunSQL(
            sql="""
            DO $$
            BEGIN
              IF EXISTS (SELECT 1 FROM pg_class WHERE relname='tipping_round_competition_id_81e5398f' AND relkind='i') THEN
                ALTER INDEX tipping_round_competition_id_81e5398f RENAME TO tipping_round_series_id_idx;
              END IF;
              IF EXISTS (SELECT 1 FROM pg_class WHERE relname='tipping_team_competition_id_938d6166' AND relkind='i') THEN
                ALTER INDEX tipping_team_competition_id_938d6166 RENAME TO tipping_team_series_id_idx;
              END IF;
            END $$;
            """,
            reverse_sql=migrations.RunSQL.noop,
        ),
        migrations.AddField(
            model_name="round",
            name="competition",
            field=models.ForeignKey(
                blank=True, null=True,
                on_delete=django.db.models.deletion.PROTECT,
                related_name="rounds", to="catalog.competition",
            ),
        ),
        migrations.RunPython(backfill_round_competition, migrations.RunPython.noop),
    ]
