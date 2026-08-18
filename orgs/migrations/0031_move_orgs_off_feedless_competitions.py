"""Move leagues off competitions that can never deliver a fixture.

Super League and Super Netball are 2027 roadmap entries with no scraper, no
teams and nothing scheduled to give them any. Six leagues had picked one, and
one of them tips nothing else — an empty dashboard for a whole season with
nothing on screen suggesting why.

The signup form no longer offers them (orgs.forms.fed_competitions), but that
only protects leagues created from now on. This is the existing ones.

WHAT IT DOES, PER ORG
  * drops every competition whose series no feed covers;
  * re-points any competition attached from the WRONG SEASON at the same
    competition in the org's own season, now that 0011 has created them —
    a 2027 league was pointing at the 2026 AFL competition, which
    Competition.for_series cannot resolve and which therefore left its rounds
    unattachable;
  * if that leaves the org with nothing at all, gives it NRL. Super League is
    rugby league, so the men's and women's NRL is the closest thing GoodTip
    actually carries, and a league with no competition cannot function.

Deliberately NOT a silent equivalence. Nobody who picked Super League is
getting Super League; they are getting the competition this product can serve.
That is a change worth an email, which is a human job — this migration only
makes sure the leagues are in a working state when it is sent.
"""

from django.db import migrations


FED_SERIES = {"AFL", "AFLW", "NRL", "NRLW", "State of Origin"}


def move(apps, schema_editor):
    Organisation = apps.get_model("orgs", "Organisation")
    Competition = apps.get_model("catalog", "Competition")

    def is_fed(comp):
        return any(s.name in FED_SERIES for s in comp.series.all())

    for org in Organisation.objects.select_related("season").prefetch_related(
        "competitions__series"
    ):
        if org.season_id is None:
            continue
        current = list(org.competitions.all())
        if not current:
            continue

        wanted = []
        for comp in current:
            if not is_fed(comp):
                continue                      # no feed — drop it
            if comp.season_id == org.season_id:
                wanted.append(comp)
                continue
            # Right competition, wrong season. Swap to the org's own.
            match = Competition.objects.filter(
                slug=comp.slug, season_id=org.season_id
            ).order_by("id").first()
            if match:
                wanted.append(match)

        if not wanted:
            fallback = Competition.objects.filter(
                slug="nrl", season_id=org.season_id
            ).order_by("id").first()
            if fallback:
                wanted = [fallback]

        if wanted:
            # De-duplicate: two wrong-season competitions can map to one right
            # one, and set() on a list with repeats is fine but the intent
            # should be explicit.
            org.competitions.set({c.id for c in wanted})


def unmove(apps, schema_editor):
    # Not reversible. The competitions removed here cannot serve a fixture, so
    # restoring them would put the leagues back into the broken state this
    # exists to fix, and the original selection is not recoverable anyway.
    pass


class Migration(migrations.Migration):

    dependencies = [
        ("orgs", "0030_roundrecap_leaderboard_roundrecap_talking_points"),
        # Needs the per-season competitions to exist before it can point
        # anything at them.
        ("catalog", "0011_competitions_for_every_season"),
    ]

    operations = [
        migrations.RunPython(move, unmove),
    ]
