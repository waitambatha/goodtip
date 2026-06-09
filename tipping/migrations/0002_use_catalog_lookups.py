from django.db import migrations, models
import django.db.models.deletion


def forwards(apps, schema_editor):
    Competition = apps.get_model("catalog", "Competition")
    Team = apps.get_model("tipping", "Team")
    Round = apps.get_model("tipping", "Round")

    comp_by_name = {c.name: c for c in Competition.objects.all()}

    for team in Team.objects.all():
        team.competition_fk = comp_by_name[team.competition]
        team.save(update_fields=["competition_fk"])

    for rnd in Round.objects.all():
        rnd.competition_fk = comp_by_name[rnd.competition]
        rnd.save(update_fields=["competition_fk"])


def backwards(apps, schema_editor):
    Team = apps.get_model("tipping", "Team")
    Round = apps.get_model("tipping", "Round")

    for team in Team.objects.all():
        team.competition = team.competition_fk.name
        team.save(update_fields=["competition"])
    for rnd in Round.objects.all():
        rnd.competition = rnd.competition_fk.name
        rnd.save(update_fields=["competition"])


class Migration(migrations.Migration):

    dependencies = [
        ("tipping", "0001_initial"),
        ("catalog", "0001_initial"),
    ]

    operations = [
        # Drop unique constraints that reference the old competition CharField.
        migrations.AlterUniqueTogether(name="team", unique_together=set()),
        migrations.AlterUniqueTogether(name="round", unique_together=set()),
        # Add nullable FK columns alongside the old CharFields.
        migrations.AddField(
            model_name="team",
            name="competition_fk",
            field=models.ForeignKey(
                null=True,
                on_delete=django.db.models.deletion.PROTECT,
                related_name="teams",
                to="catalog.competition",
            ),
        ),
        migrations.AddField(
            model_name="round",
            name="competition_fk",
            field=models.ForeignKey(
                null=True,
                on_delete=django.db.models.deletion.PROTECT,
                related_name="rounds",
                to="catalog.competition",
            ),
        ),
        # Copy data across.
        migrations.RunPython(forwards, backwards),
        # Remove the old CharFields and promote the FK columns to the real names.
        migrations.RemoveField(model_name="team", name="competition"),
        migrations.RemoveField(model_name="round", name="competition"),
        migrations.RenameField(model_name="team", old_name="competition_fk", new_name="competition"),
        migrations.RenameField(model_name="round", old_name="competition_fk", new_name="competition"),
        migrations.AlterField(
            model_name="team",
            name="competition",
            field=models.ForeignKey(
                on_delete=django.db.models.deletion.PROTECT,
                related_name="teams",
                to="catalog.competition",
            ),
        ),
        migrations.AlterField(
            model_name="round",
            name="competition",
            field=models.ForeignKey(
                on_delete=django.db.models.deletion.PROTECT,
                related_name="rounds",
                to="catalog.competition",
            ),
        ),
        # Restore the unique constraints, now over the FK column.
        migrations.AlterUniqueTogether(name="team", unique_together={("slug", "competition")}),
        migrations.AlterUniqueTogether(
            name="round", unique_together={("org", "round_number", "competition")}
        ),
    ]
