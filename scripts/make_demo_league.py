"""Create a playable demo league: one admin, a squad of members, real AFL fixtures.

Idempotent — re-running updates the same rows rather than duplicating them.
Run with:  ./venv/bin/python scripts/make_demo_league.py
"""
import os
import sys

import django

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.environ.setdefault("DJANGO_SETTINGS_MODULE", "goodtip.settings")
django.setup()

from django.contrib.auth import get_user_model  # noqa: E402

from catalog.models import Charity, Competition, GroupType, Season, State  # noqa: E402
from orgs.models import OrgMember, Organisation  # noqa: E402

User = get_user_model()

PASSWORD = "GoodTip#2026"
ADMIN_EMAIL = "admin@goodtip.test"
ORG_NAME = "Demo Sparks FC"

MEMBERS = [
    ("jess@goodtip.test", "Jess Whitlam", OrgMember.ROLE_CAPTAIN),
    ("tom@goodtip.test", "Tom Reddy", OrgMember.ROLE_PARTICIPANT),
    ("priya@goodtip.test", "Priya Nair", OrgMember.ROLE_PARTICIPANT),
    ("marcus@goodtip.test", "Marcus Bell", OrgMember.ROLE_MANAGER),
    ("aroha@goodtip.test", "Aroha Tane", OrgMember.ROLE_PARTICIPANT),
    ("dan@goodtip.test", "Dan Okonkwo", OrgMember.ROLE_PARTICIPANT),
]


def upsert_user(email, display_name, *, superuser=False):
    user, created = User.objects.get_or_create(
        email=email,
        defaults={"username": email, "display_name": display_name},
    )
    user.display_name = display_name
    user.username = email
    user.is_active = True
    user.is_staff = superuser
    user.is_superuser = superuser
    user.set_password(PASSWORD)
    user.save()
    return user, created


def main():
    season = Season.objects.get(year=2026)
    afl = Competition.objects.get(series__name="AFL", season=season)

    admin, _ = upsert_user(ADMIN_EMAIL, "Erick Mbatha", superuser=True)

    org, org_created = Organisation.objects.get_or_create(
        name=ORG_NAME,
        season=season,
        defaults={
            "group_type": GroupType.objects.get(name="Community"),
            "state": State.objects.get(code="VIC"),
            "charity": Charity.objects.get(name="Lifeline"),
            "is_public_listed": True,
            "team_size": 10,
        },
    )
    org.competitions.add(afl)

    OrgMember.objects.update_or_create(
        user=admin,
        org=org,
        defaults={"role": OrgMember.ROLE_BOTH, "is_league_owner": True},
    )

    for email, name, role in MEMBERS:
        member_user, _ = upsert_user(email, name)
        OrgMember.objects.update_or_create(
            user=member_user,
            org=org,
            defaults={"role": role, "is_league_owner": False, "invited_by": admin},
        )

    print(f"org: {org} (id={org.id}, created={org_created})")
    print(f"competitions: {[str(c) for c in org.competitions.all()]}")
    print(f"members: {org.members.count()}")
    for m in org.members.select_related("user"):
        flag = " [LEAGUE OWNER]" if m.is_league_owner else ""
        print(f"  - {m.user.email:26} {m.role:12}{flag}")
    print(f"\nlogin: {ADMIN_EMAIL} / {PASSWORD}")
    return org


if __name__ == "__main__":
    main()
