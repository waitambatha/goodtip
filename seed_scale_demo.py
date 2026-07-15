#!/usr/bin/env python
"""Scale demo seed for ian@terminology.digital (Ian Hopkinson).

Paints the dashboard the way it will look mid-season:
- fills Test League 1's Round 1 out to a full 9-game AFL fixture and adds
  Rounds 2-4 behind it,
- creates 15 extra comps the user belongs to, each with an open Round 1,
  staggered lockouts, and a mix of tip states (all in / partial / none),
so every card state the dashboard can render is on screen at once.

Idempotent: get_or_create everywhere; safe to re-run.
"""

import os
import random
from datetime import timedelta

import django
from django.utils import timezone

os.environ.setdefault("DJANGO_SETTINGS_MODULE", "goodtip.settings")
django.setup()

from django.contrib.auth import get_user_model
from catalog.models import Charity, Competition, GroupType, Season, Series
from orgs.models import Organisation, OrgMember
from tipping.models import Match, Round, Team, Tip

User = get_user_model()
random.seed(2026)  # deterministic so re-runs don't reshuffle fixtures

USER_EMAIL = "ian@terminology.digital"

LEAGUE_NAMES = [
    "Boardroom Tipsters", "Warehouse Legends", "North Shore Nine",
    "Friday Arvo Footy", "The Lunchroom League", "Coastal Crew Tips",
    "Back Paddock Footy", "Data Team Derby", "The Sunday Sherrins",
    "Depot Dream Team", "Harbour City Tipping", "The Long Lunch League",
    "Spreadsheet Warriors", "Smoko Selections", "The Golden Points",
]

VENUES = ["MCG", "Marvel Stadium", "Adelaide Oval", "The Gabba", "Optus Stadium", "SCG", "GMHBA Stadium"]

ROLES = [
    (OrgMember.ROLE_BOTH, True),          # Owner + Manager + Captain
    (OrgMember.ROLE_MANAGER, False),
    (OrgMember.ROLE_CAPTAIN, False),
    (OrgMember.ROLE_PARTICIPANT, False),
    (OrgMember.ROLE_BOTH, False),
]

TEAM_NAMES = [
    "Adelaide Crows", "Brisbane Lions", "Carlton", "Collingwood",
    "Essendon", "Fremantle", "Geelong Cats", "Gold Coast Suns",
    "Greater Western Sydney", "Hawthorn", "Melbourne", "North Melbourne",
    "Port Adelaide", "Richmond", "St Kilda", "Sydney Swans",
    "West Coast Eagles", "Western Bulldogs",
]


def ensure_teams(series):
    for i, name in enumerate(TEAM_NAMES):
        Team.objects.get_or_create(
            name=name, slug=name.lower().replace(" ", "-"), series=series,
            defaults={"external_id": f"team_{i + 1}"},
        )
    return list(Team.objects.filter(series=series))


def make_fixture(round_obj, teams, first_kickoff):
    """A full 9-game slate: every team plays once."""
    shuffled = teams[:]
    random.shuffle(shuffled)
    matches = []
    for g in range(len(shuffled) // 2):
        home, away = shuffled[g * 2], shuffled[g * 2 + 1]
        match, _ = Match.objects.get_or_create(
            round=round_obj, home_team=home, away_team=away,
            defaults={
                "kickoff_at": first_kickoff + timedelta(hours=g * 5),
                "venue": VENUES[g % len(VENUES)],
            },
        )
        matches.append(match)
    return matches


def tip_matches(user, org, matches, how_many):
    for match in matches[:how_many]:
        Tip.objects.get_or_create(
            match=match, user=user, org=org,
            defaults={"selection": random.choice(["home", "away"])},
        )


def main():
    now = timezone.now()
    user = User.objects.get(email=USER_EMAIL)
    season = Season.objects.get(year=2026)
    series = Series.objects.get(slug="afl")
    competition, created = Competition.objects.get_or_create(
        sport=series.sport, season=season, slug="afl-2026", defaults={"name": "AFL 2026"},
    )
    if created:
        competition.series.add(series)
    teams = ensure_teams(series)
    charities = list(Charity.objects.filter(is_approved=True)) or [
        Charity.objects.get_or_create(
            name="Red Cross Australia",
            defaults={"slug": "red-cross-australia", "is_approved": True},
        )[0]
    ]
    group_types = list(GroupType.objects.all())
    print(f"User {user.display_name}, {len(teams)} teams, {len(charities)} charities")

    # ---- Test League 1: full Round 1 fixture + three more rounds ----
    tl1 = Organisation.objects.filter(name="Test League 1", season=season).first()
    if tl1:
        if not tl1.competitions.filter(pk=competition.pk).exists():
            tl1.competitions.add(competition)
        r1 = Round.objects.filter(org=tl1, series=series, round_number=1).first()
        if r1:
            existing = r1.matches.count()
            used = set()
            for m in r1.matches.select_related("home_team", "away_team"):
                used.update([m.home_team_id, m.away_team_id])
            free = [t for t in teams if t.pk not in used]
            random.shuffle(free)
            for g in range(len(free) // 2):
                Match.objects.get_or_create(
                    round=r1, home_team=free[g * 2], away_team=free[g * 2 + 1],
                    defaults={
                        "kickoff_at": r1.lockout_at + timedelta(hours=2 + g * 5),
                        "venue": VENUES[g % len(VENUES)],
                    },
                )
            print(f"Test League 1 R1: {existing} -> {r1.matches.count()} matches")
        for rn in (2, 3, 4):
            rnd, created = Round.objects.get_or_create(
                org=tl1, round_number=rn, series=series,
                defaults={
                    "competition": competition,
                    "lockout_at": now + timedelta(days=7 * (rn - 1), hours=20),
                    "status": "upcoming",
                },
            )
            if created:
                make_fixture(rnd, teams, rnd.lockout_at + timedelta(hours=2))
                print(f"Test League 1: created Round {rn} (9 games)")

    # ---- 15 extra comps with an open round each ----
    for i, name in enumerate(LEAGUE_NAMES):
        org, created = Organisation.objects.get_or_create(
            name=name, season=season,
            defaults={
                "charity": charities[i % len(charities)],
                "group_type": group_types[i % len(group_types)] if group_types else None,
            },
        )
        if created:
            org.competitions.add(competition)
        role, owner = ROLES[i % len(ROLES)]
        OrgMember.objects.get_or_create(
            user=user, org=org, defaults={"role": role, "is_league_owner": owner},
        )
        # lockouts fan out: first few lock within hours ("locks soon"), the
        # rest across the coming week
        lockout = now + timedelta(hours=6 + i * 14)
        rnd, r_created = Round.objects.get_or_create(
            org=org, round_number=1, series=series,
            defaults={"competition": competition, "lockout_at": lockout, "status": "open"},
        )
        matches = (
            make_fixture(rnd, teams, lockout + timedelta(hours=3))
            if r_created else list(rnd.matches.all())
        )
        # tip states cycle: all in / partial / none
        state = i % 3
        tipped = len(matches) if state == 0 else (random.randint(2, 6) if state == 1 else 0)
        tip_matches(user, org, matches, tipped)
        print(f"{'Created' if created else 'Kept   '} {name}: {len(matches)} games, "
              f"{tipped}/{len(matches)} tipped, locks {lockout:%a %H:%M}")

    total = OrgMember.objects.filter(user=user).count()
    print(f"\nDone. {user.display_name} is now in {total} comps.")


if __name__ == "__main__":
    main()
