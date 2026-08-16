"""Merge the second Greater Western Sydney into the first.

The Team table held nineteen AFL clubs. There are eighteen:

    id=9   greater-western-sydney   Greater Western Sydney   external_id=team_9
    id=27  gws-giants               GWS GIANTS               external_id=sr:competitor:60170

Sportradar's resolver created id=27. It could not match "GWS GIANTS" to the
existing club and made a new one, despite ``AFL_TEAM_ALIASES`` in
data_sync/services.py already mapping the slug ``gws-giants`` to
``greater-western-sydney`` — that map is consulted by the scraper resolver,
which the Sportradar resolver did not share.

The damage is that GWS's record is split in two, so every GWS fixture is stored
once against each record. The derived ladder wrote nineteen rows for an
eighteen-team competition, with GWS appearing twice on partial records.

Safe to merge: no Tip references any match involving id=27, and it holds no
LadderEntry rows.

This migration is written against ids matched by SLUG rather than primary key,
so it is a no-op on a database that never had the duplicate.
"""

from django.db import migrations

DUPLICATE_SLUG = "gws-giants"
CANONICAL_SLUG = "greater-western-sydney"


def merge(apps, schema_editor):
    Team = apps.get_model("tipping", "Team")
    Match = apps.get_model("tipping", "Match")
    Tip = apps.get_model("tipping", "Tip")
    HistoricalMatch = apps.get_model("matchreader", "HistoricalMatch")

    dupe = Team.objects.filter(slug=DUPLICATE_SLUG, series__name="AFL").first()
    keep = Team.objects.filter(slug=CANONICAL_SLUG, series__name="AFL").first()
    if dupe is None or keep is None or dupe.pk == keep.pk:
        return

    _merge_history(HistoricalMatch, dupe, keep)
    _merge_matches(Match, Tip, dupe, keep)
    dupe.delete()


def _merge_history(HistoricalMatch, dupe, keep):
    """Repoint history rows, dropping the ones that would collide.

    A collision means the same fixture is already stored against the canonical
    club — the duplicate row carries nothing the kept row does not, so it goes.
    """
    dupe_pks = set(
        HistoricalMatch.objects.filter(home_team=dupe).values_list("id", flat=True)
    ) | set(
        HistoricalMatch.objects.filter(away_team=dupe).values_list("id", flat=True)
    )

    occupied = {
        (season, rn, h, a)
        for season, rn, h, a in HistoricalMatch.objects.exclude(
            id__in=dupe_pks
        ).values_list("season", "round_number", "home_team_id", "away_team_id")
    }

    doomed = []
    for pk, season, rn, h, a in HistoricalMatch.objects.filter(
        id__in=dupe_pks
    ).values_list("id", "season", "round_number", "home_team_id", "away_team_id"):
        key = (
            season, rn,
            keep.pk if h == dupe.pk else h,
            keep.pk if a == dupe.pk else a,
        )
        if key in occupied:
            doomed.append(pk)
        else:
            occupied.add(key)

    if doomed:
        HistoricalMatch.objects.filter(id__in=doomed).delete()
    HistoricalMatch.objects.filter(home_team=dupe).update(home_team=keep)
    HistoricalMatch.objects.filter(away_team=dupe).update(away_team=keep)


def _merge_matches(Match, Tip, dupe, keep):
    """Same for the org-scoped fixtures.

    Match has no uniqueness constraint, so a collision here shows up as two
    identical fixtures in one round rather than an integrity error — which is
    worse, because it is silent. Deduplicated on (round, home, away).

    A duplicate is only dropped if nothing has been tipped on it. There are no
    such tips today, but a migration that can silently destroy a member's tip
    is not one to leave lying in the tree.
    """
    dupe_pks = set(
        Match.objects.filter(home_team=dupe).values_list("id", flat=True)
    ) | set(
        Match.objects.filter(away_team=dupe).values_list("id", flat=True)
    )

    occupied = {
        (r, h, a)
        for r, h, a in Match.objects.exclude(id__in=dupe_pks).values_list(
            "round_id", "home_team_id", "away_team_id"
        )
    }
    tipped = set(
        Tip.objects.filter(match_id__in=dupe_pks).values_list("match_id", flat=True)
    )

    doomed = []
    for pk, round_id, h, a in Match.objects.filter(id__in=dupe_pks).values_list(
        "id", "round_id", "home_team_id", "away_team_id"
    ):
        key = (
            round_id,
            keep.pk if h == dupe.pk else h,
            keep.pk if a == dupe.pk else a,
        )
        if key in occupied and pk not in tipped:
            doomed.append(pk)
        else:
            occupied.add(key)

    if doomed:
        Match.objects.filter(id__in=doomed).delete()
    Match.objects.filter(home_team=dupe).update(home_team=keep)
    Match.objects.filter(away_team=dupe).update(away_team=keep)


def noop(apps, schema_editor):
    """The duplicate club is not worth recreating, and could not be filled back
    with the rows that were merged into the survivor."""


class Migration(migrations.Migration):

    dependencies = [
        ("tipping", "0009_ladderentry_byes_ladderadjustment"),
        ("matchreader", "0004_reclassify_stages"),
    ]

    operations = [
        migrations.RunPython(merge, noop),
    ]
