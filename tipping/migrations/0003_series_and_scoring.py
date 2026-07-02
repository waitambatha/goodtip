from django.db import migrations, models


def backfill_points(apps, schema_editor):
    """Existing graded tips were all regular-round (1 pt) under the old flat model.

    Award 1 point to every already-correct tip so historical leaderboards are
    unchanged by the switch from Count() to Sum().
    """
    Tip = apps.get_model("tipping", "Tip")
    Tip.objects.filter(is_correct=True).update(points_awarded=1)


class Migration(migrations.Migration):

    dependencies = [
        ("catalog", "0003_series_origin"),
        ("tipping", "0002_use_catalog_lookups"),
    ]

    operations = [
        # Team.competition -> Team.series
        migrations.AlterUniqueTogether(name="team", unique_together=set()),
        migrations.RenameField(model_name="team", old_name="competition", new_name="series"),
        migrations.AlterField(
            model_name="team",
            name="series",
            field=models.ForeignKey(
                on_delete=models.deletion.PROTECT, related_name="teams", to="catalog.series"
            ),
        ),
        migrations.AlterModelOptions(name="team", options={"ordering": ["series", "name"]}),
        migrations.AlterUniqueTogether(name="team", unique_together={("slug", "series")}),
        # Round.competition -> Round.series
        migrations.AlterUniqueTogether(name="round", unique_together=set()),
        migrations.RenameField(model_name="round", old_name="competition", new_name="series"),
        migrations.AlterField(
            model_name="round",
            name="series",
            field=models.ForeignKey(
                on_delete=models.deletion.PROTECT, related_name="rounds", to="catalog.series"
            ),
        ),
        migrations.AlterUniqueTogether(name="round", unique_together={("org", "round_number", "series")}),
        # Round.stage — drives weighted scoring (regular=1, finals=2, origin=4)
        migrations.AddField(
            model_name="round",
            name="stage",
            field=models.CharField(
                choices=[
                    ("regular", "Regular round"),
                    ("finals", "Finals"),
                    ("origin", "State of Origin"),
                ],
                default="regular",
                max_length=10,
            ),
        ),
        # Tip.points_awarded — stored weighted score per graded tip
        migrations.AddField(
            model_name="tip",
            name="points_awarded",
            field=models.PositiveIntegerField(default=0),
        ),
        migrations.RunPython(backfill_points, migrations.RunPython.noop),
    ]
