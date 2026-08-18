"""Move empty leagues off a season nobody is playing yet.

Nine organisations sat on season 2027 with no rounds, no tips and, in most
cases, one member. None of them chose it: the season dropdown is ordered by
-year, so the furthest-future season on file was the first option, and with no
empty label it became the silent default. Every league created in that window
landed in a season the feeds have no draw for — nrl.com and afl.com.au both
404 for 2027 — so the league synced nothing and showed an empty dashboard with
nothing on screen suggesting why.

The form itself was fixed on 5 Aug (orgs.forms: default to the season actually
in play). This is the leagues created before that landed, which the fix cannot
reach.

SAFE BY CONSTRUCTION. Only orgs with NO rounds and NO tips move. An org that
has played anything has a season that means something and is left alone, so
this cannot rewrite history for anyone who has actually used the product.

Competitions are re-pointed to the same code in the new season, so a league
that picked NRL keeps NRL — the men's and women's and Origin, as that bundle
has always contained.
"""

from django.db import migrations


def move(apps, schema_editor):
    Organisation = apps.get_model("orgs", "Organisation")
    Competition = apps.get_model("catalog", "Competition")
    Round = apps.get_model("tipping", "Round")
    Tip = apps.get_model("tipping", "Tip")
    Season = apps.get_model("catalog", "Season")

    # The season actually being played: the one fixtures exist for. Derived
    # rather than hardcoded, so this does the right thing whenever it runs.
    seasons_with_rounds = set(
        Round.objects.values_list("org__season__year", flat=True).distinct()
    )
    seasons_with_rounds.discard(None)
    if not seasons_with_rounds:
        return
    target = Season.objects.filter(year=max(seasons_with_rounds)).first()
    if target is None:
        return

    for org in Organisation.objects.exclude(season=target).prefetch_related("competitions"):
        if org.season_id is None:
            continue
        # Anything played stays where it is. This is for leagues that never
        # got off the ground because the season had no fixtures to give them.
        if Round.objects.filter(org=org).exists() or Tip.objects.filter(org=org).exists():
            continue

        slugs = [c.slug for c in org.competitions.all()]
        org.season = target
        org.save(update_fields=["season"])
        if slugs:
            moved = Competition.objects.filter(slug__in=slugs, season=target)
            org.competitions.set(moved)


def unmove(apps, schema_editor):
    # Not reversible: the original season is not recorded anywhere, and
    # restoring it would put these leagues back in a season with no fixtures.
    pass


class Migration(migrations.Migration):

    dependencies = [
        ("orgs", "0031_move_orgs_off_feedless_competitions"),
        ("tipping", "0011_tip_is_auto"),
    ]

    operations = [
        migrations.RunPython(move, unmove),
    ]
