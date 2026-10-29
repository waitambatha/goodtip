"""A Competition for every new league, in every season from 2026 on — what an
organisation signs up to (client, 27 Oct 2026). One league each, named as the
league: "English Premier League (2026)" bundles the Premier League alone.

Also corrects where the fixtures come from: football-data.org would not serve
this project without a paid key, so football reads ESPN's public feed like
the rest; and the few with no free source at all say so.
"""
from django.db import migrations

ESPN = "ESPN public feeds"
NO_FEED = {
    "WNBL": "No free feed found yet",
    "PWHL": "No free feed found yet",
    "Super Rugby Women's": "No free feed found yet",
    "Test Cricket": "No free feed found yet",
    "Grand Slams — Men": "Field events — tipping format to come",
    "Grand Slams — Women": "Field events — tipping format to come",
    "Men's Majors": "Field events — tipping format to come",
    "Women's Majors": "Field events — tipping format to come",
    "Formula 1": "Jolpica F1 API — tipping format to come",
    "Supercars": "Field events — tipping format to come",
}
NEW_SPORTS = ["football", "basketball", "cricket", "rugby-union", "tennis", "golf",
              "motorsport", "american-football", "ice-hockey", "baseball"]


def forwards(apps, schema_editor):
    db = schema_editor.connection.alias
    Series = apps.get_model("catalog", "Series")
    Season = apps.get_model("catalog", "Season")
    Competition = apps.get_model("catalog", "Competition")

    for s in Series.objects.using(db).filter(sport__slug__in=NEW_SPORTS):
        s.feed_source = NO_FEED.get(s.name, ESPN)
        s.save(using=db, update_fields=["feed_source"])

    seasons = list(Season.objects.using(db).filter(year__gte=2026))
    for s in Series.objects.using(db).filter(sport__slug__in=NEW_SPORTS).select_related("sport"):
        for season in seasons:
            comp, _ = Competition.objects.using(db).get_or_create(
                slug=s.slug, season=season, defaults={"name": s.name, "sport": s.sport})
            comp.series.add(s)


def backwards(apps, schema_editor):
    db = schema_editor.connection.alias
    Competition = apps.get_model("catalog", "Competition")
    Competition.objects.using(db).filter(sport__slug__in=NEW_SPORTS, organisations__isnull=True).delete()


class Migration(migrations.Migration):
    dependencies = [("catalog", "0024_seed_more_sports")]
    operations = [migrations.RunPython(forwards, backwards)]
