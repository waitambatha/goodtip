"""Every season gets the AFL and NRL competitions, not just the ones that
existed when 0004 ran.

0004 looped `Season.objects.all()` and created one Competition per (sport,
season). That was correct on the day, and became wrong the moment a season was
added AFTER it: 0005 introduced the 2027 season for the roadmap entries, so
2027 got Super League and Super Netball and no AFL or NRL at all.

The consequence was not obvious. Nine organisations sit on season 2027, and the
ones "tipping AFL" were pointed at the 2026 AFL competition — a season-mismatch
that Competition.for_series cannot resolve, because it looks up by (series,
season) and there was nothing to find. Rounds could not be attached to a
competition, and the dashboard filters rounds by the org's competitions, so
those leagues were structurally incapable of showing a fixture.

Seeding by iteration over seasons rather than by a hardcoded year, so the next
season added inherits its competitions without another migration.
"""

from django.db import migrations


# name, slug, sport slug, [series slugs] — the same shape 0004 used, so the
# two cannot describe the bundles differently.
COMPETITIONS = [
    ("AFL", "afl", "australian-rules", ["afl", "aflw"]),
    ("NRL", "nrl", "rugby-league", ["nrl", "nrlw", "state-of-origin"]),
]


def seed(apps, schema_editor):
    Sport = apps.get_model("catalog", "Sport")
    Series = apps.get_model("catalog", "Series")
    Season = apps.get_model("catalog", "Season")
    Competition = apps.get_model("catalog", "Competition")

    sports = {s.slug: s for s in Sport.objects.all()}
    series = {s.slug: s for s in Series.objects.all()}

    for season in Season.objects.all():
        for name, slug, sport_slug, series_slugs in COMPETITIONS:
            sport = sports.get(sport_slug)
            if sport is None:
                continue
            comp, _ = Competition.objects.get_or_create(
                slug=slug, season=season, defaults={"name": name, "sport": sport},
            )
            # set() rather than add(): a competition that somehow lost a series
            # is repaired, and re-running changes nothing.
            comp.series.set([series[s] for s in series_slugs if s in series])


def unseed(apps, schema_editor):
    # Deliberately a no-op. Removing competitions would orphan the rounds and
    # organisations attached to them, and this migration only ever ADDS rows
    # that 0004 would have created had the season existed.
    pass


class Migration(migrations.Migration):

    dependencies = [
        ("catalog", "0010_departmenttype"),
    ]

    operations = [
        migrations.RunPython(seed, unseed),
    ]
