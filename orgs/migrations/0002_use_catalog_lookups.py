from django.db import migrations, models
import django.db.models.deletion


# Map the old Organisation.sport CharField value to the catalog Sport names.
SPORT_MAP = {
    "AFL": ["AFL"],
    "NRL": ["NRL"],
    "BOTH": ["AFL", "NRL"],
}


def forwards(apps, schema_editor):
    Organisation = apps.get_model("orgs", "Organisation")
    Sport = apps.get_model("catalog", "Sport")
    Season = apps.get_model("catalog", "Season")

    sport_by_name = {s.name: s for s in Sport.objects.all()}

    for org in Organisation.objects.all():
        season, _ = Season.objects.get_or_create(
            year=org.season, defaults={"label": str(org.season)}
        )
        org.season_fk = season
        org.save(update_fields=["season_fk"])

        names = SPORT_MAP.get(org.sport, [])
        org.sports.set([sport_by_name[n] for n in names if n in sport_by_name])


def backwards(apps, schema_editor):
    Organisation = apps.get_model("orgs", "Organisation")
    for org in Organisation.objects.all():
        names = sorted(s.name for s in org.sports.all())
        org.sport = "BOTH" if len(names) > 1 else (names[0] if names else "")
        org.season = org.season_fk.year if org.season_fk else 0
        org.save(update_fields=["sport", "season"])


class Migration(migrations.Migration):

    dependencies = [
        ("orgs", "0001_initial"),
        ("catalog", "0001_initial"),
    ]

    operations = [
        migrations.AddField(
            model_name="organisation",
            name="sports",
            field=models.ManyToManyField(blank=True, related_name="organisations", to="catalog.sport"),
        ),
        migrations.AddField(
            model_name="organisation",
            name="season_fk",
            field=models.ForeignKey(
                null=True,
                on_delete=django.db.models.deletion.PROTECT,
                related_name="organisations",
                to="catalog.season",
            ),
        ),
        migrations.RunPython(forwards, backwards),
        migrations.RemoveField(model_name="organisation", name="sport"),
        migrations.RemoveField(model_name="organisation", name="season"),
        migrations.RenameField(model_name="organisation", old_name="season_fk", new_name="season"),
        migrations.AlterField(
            model_name="organisation",
            name="season",
            field=models.ForeignKey(
                on_delete=django.db.models.deletion.PROTECT,
                related_name="organisations",
                to="catalog.season",
            ),
        ),
    ]
