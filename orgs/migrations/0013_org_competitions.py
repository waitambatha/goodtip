from django.db import migrations, models


def sports_to_competitions(apps, schema_editor):
    """Repoint each league from the sports it picked to the matching Competition
    for its season (e.g. sport 'Rugby League' + season 2026 -> 'NRL (2026)')."""
    Organisation = apps.get_model("orgs", "Organisation")
    Competition = apps.get_model("catalog", "Competition")
    for org in Organisation.objects.all():
        comps = []
        for sport in org.sports.all():
            comp = Competition.objects.filter(sport=sport, season=org.season).first()
            if comp is not None:
                comps.append(comp)
        if comps:
            org.competitions.set(comps)


def competitions_to_sports(apps, schema_editor):
    Organisation = apps.get_model("orgs", "Organisation")
    for org in Organisation.objects.all():
        org.sports.set([c.sport for c in org.competitions.all()])


class Migration(migrations.Migration):

    dependencies = [
        ("catalog", "0004_competition_sport_codes"),
        ("orgs", "0012_seed_charity_selections"),
    ]

    operations = [
        migrations.AddField(
            model_name="organisation",
            name="competitions",
            field=models.ManyToManyField(blank=True, related_name="organisations", to="catalog.competition"),
        ),
        migrations.RunPython(sports_to_competitions, competitions_to_sports),
        migrations.RemoveField(model_name="organisation", name="sports"),
    ]
