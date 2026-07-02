from django.db import migrations


# The 2027 roadmap rows so every level of the hierarchy shows the brief's
# examples (Ambrose brief, slide 7 / Fixtures Calendar): Super League (Feb 2027)
# and Super Netball (May 2027).
NEW_SERIES = [
    # slug, name, sport slug, category
    ("super-league", "Super League", "rugby-league", "mens"),
    ("super-netball", "Super Netball", "netball", "womens"),
]

NEW_COMPETITIONS = [
    # name, slug, sport slug, [series slugs]
    ("Super League", "super-league", "rugby-league", ["super-league"]),
    ("Super Netball", "super-netball", "netball", ["super-netball"]),
]


def seed_roadmap(apps, schema_editor):
    Sport = apps.get_model("catalog", "Sport")
    Series = apps.get_model("catalog", "Series")
    Season = apps.get_model("catalog", "Season")
    Competition = apps.get_model("catalog", "Competition")

    season, _ = Season.objects.get_or_create(year=2027, defaults={"label": "2027"})
    sports = {s.slug: s for s in Sport.objects.all()}

    for slug, name, sport_slug, category in NEW_SERIES:
        sport = sports.get(sport_slug)
        if sport is None:
            continue
        Series.objects.get_or_create(
            slug=slug,
            defaults={"name": name, "sport": sport, "category": category,
                      "is_womens": category == "womens", "representation_type": "full"},
        )

    series = {s.slug: s for s in Series.objects.all()}
    for name, slug, sport_slug, series_slugs in NEW_COMPETITIONS:
        sport = sports.get(sport_slug)
        if sport is None:
            continue
        comp, _ = Competition.objects.get_or_create(
            slug=slug, season=season, defaults={"name": name, "sport": sport},
        )
        comp.series.set([series[s] for s in series_slugs if s in series])


def unseed_roadmap(apps, schema_editor):
    apps.get_model("catalog", "Competition").objects.filter(
        slug__in=["super-league", "super-netball"], season__year=2027
    ).delete()
    apps.get_model("catalog", "Series").objects.filter(
        slug__in=["super-league", "super-netball"]
    ).delete()


class Migration(migrations.Migration):

    dependencies = [
        ("catalog", "0004_competition_sport_codes"),
    ]

    operations = [
        migrations.RunPython(seed_roadmap, unseed_roadmap),
    ]
