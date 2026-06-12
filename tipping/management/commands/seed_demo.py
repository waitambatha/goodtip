import random
from datetime import timedelta

from django.core.management.base import BaseCommand
from django.db import transaction
from django.utils import timezone

from accounts.models import User
from catalog.models import Charity, Competition, Season, Sport
from orgs.models import OrgMember, Organisation
from tipping.models import Match, Round, Team, Tip
from tipping.services import record_match_result


DEMO_USERS = [
    ("Sarah Chen", "sarah@goodtip.example"),
    ("Marcus Thompson", "marcus@goodtip.example"),
    ("Aaliyah Patel", "aaliyah@goodtip.example"),
    ("Jack O'Brien", "jack@goodtip.example"),
    ("Priya Singh", "priya@goodtip.example"),
    ("Tom Williams", "tom@goodtip.example"),
    ("Mia Robinson", "mia@goodtip.example"),
    ("Ben Foster", "ben@goodtip.example"),
    ("Lucy Tran", "lucy@goodtip.example"),
    ("Diego Martinez", "diego@goodtip.example"),
    ("Olivia King", "olivia@goodtip.example"),
]

R1_FIXTURES = [
    ("carlton", "collingwood", "MCG"),
    ("geelong-cats", "hawthorn", "GMHBA Stadium"),
    ("sydney-swans", "brisbane-lions", "SCG"),
    ("west-coast-eagles", "fremantle", "Optus Stadium"),
    ("essendon", "melbourne", "MCG"),
    ("port-adelaide", "adelaide-crows", "Adelaide Oval"),
]
R1_RESULTS = [(80, 70), (90, 75), (110, 95), (88, 105), (75, 90), (105, 85)]

R2_FIXTURES = [
    ("richmond", "carlton", "MCG"),
    ("collingwood", "sydney-swans", "MCG"),
    ("st-kilda", "western-bulldogs", "Marvel Stadium"),
    ("gold-coast-suns", "gws-giants", "People First Stadium"),
    ("geelong-cats", "brisbane-lions", "GMHBA Stadium"),
    ("north-melbourne", "port-adelaide", "Marvel Stadium"),
]
R2_RESULTS = [(85, 95), (100, 90), (70, 95), (100, 85), (88, 110), (65, 90)]

R3_FIXTURES = [
    ("carlton", "essendon", "MCG"),
    ("collingwood", "melbourne", "MCG"),
    ("sydney-swans", "gws-giants", "SCG"),
    ("fremantle", "west-coast-eagles", "Optus Stadium"),
    ("brisbane-lions", "hawthorn", "Gabba"),
    ("richmond", "geelong-cats", "MCG"),
]

R4_FIXTURES = [
    ("melbourne", "western-bulldogs", "MCG"),
    ("sydney-swans", "carlton", "SCG"),
    ("brisbane-lions", "collingwood", "Gabba"),
    ("adelaide-crows", "port-adelaide", "Adelaide Oval"),
    ("fremantle", "hawthorn", "Optus Stadium"),
    ("geelong-cats", "essendon", "GMHBA Stadium"),
]


class Command(BaseCommand):
    help = "Seed showcase demo data — wipes any existing rounds/matches/tips on the demo orgs first."

    @transaction.atomic
    def handle(self, *args, **options):
        random.seed(42)

        # 1. Users — Hop plus 11 demo tippers
        try:
            hop = User.objects.get(email="hop@goodtip.com.au")
        except User.DoesNotExist:
            hop = User.objects.create_superuser(
                email="hop@goodtip.com.au", password="hopgoodtip2026", display_name="Hop"
            )

        users = [hop]
        for name, email in DEMO_USERS:
            user = User.objects.filter(email=email).first()
            if not user:
                user = User.objects.create_user(email=email, password="demo1234", display_name=name)
            users.append(user)

        # 2. Orgs (idempotent)
        season, _ = Season.objects.get_or_create(year=2026, defaults={"label": "2026"})
        afl = Sport.objects.get(name="AFL")
        nrl = Sport.objects.get(name="NRL")

        beyond_blue, _ = Charity.objects.get_or_create(
            name="Beyond Blue",
            defaults={"slug": "beyond-blue", "website": "https://www.beyondblue.org.au", "is_approved": True},
        )
        ruok, _ = Charity.objects.get_or_create(
            name="R U OK?",
            defaults={"slug": "r-u-ok", "website": "https://www.ruok.org.au", "is_approved": True},
        )

        org1, _ = Organisation.objects.update_or_create(
            name="Test Friends Comp",
            defaults={"season": season, "charity": beyond_blue},
        )
        org1.sports.set([afl])
        org2, _ = Organisation.objects.update_or_create(
            name="Office Footy Crew",
            defaults={"season": season, "charity": ruok},
        )
        org2.sports.set([afl, nrl])

        # 3. Memberships — the host runs and owns each league.
        host_defaults = {"role": OrgMember.ROLE_BOTH, "is_league_owner": True}
        member_defaults = {"role": OrgMember.ROLE_PARTICIPANT}
        for u in users:
            OrgMember.objects.get_or_create(
                user=u, org=org1, defaults=host_defaults if u == hop else member_defaults
            )
        for u in users[:7]:
            OrgMember.objects.get_or_create(
                user=u, org=org2, defaults=host_defaults if u == hop else member_defaults
            )

        # 4. Wipe existing rounds/matches/tips on the demo orgs (clean re-seed)
        Tip.objects.filter(org__in=[org1, org2]).delete()
        Match.objects.filter(round__org__in=[org1, org2]).delete()
        Round.objects.filter(org__in=[org1, org2]).delete()

        # 5. Build rounds in different states for org1
        now = timezone.now()

        r1 = self._make_round(org1, 1, "AFL", now - timedelta(days=14), "complete")
        self._make_matches(r1, R1_FIXTURES, now - timedelta(days=14, hours=-2), "demo-r1")
        r2 = self._make_round(org1, 2, "AFL", now - timedelta(days=7), "complete")
        self._make_matches(r2, R2_FIXTURES, now - timedelta(days=7, hours=-2), "demo-r2")
        r3 = self._make_round(org1, 3, "AFL", now + timedelta(days=2), "open")
        self._make_matches(r3, R3_FIXTURES, now + timedelta(days=2, hours=2), "demo-r3")
        r4 = self._make_round(org1, 4, "AFL", now + timedelta(days=5), "upcoming")
        self._make_matches(r4, R4_FIXTURES, now + timedelta(days=5, hours=2), "demo-r4")

        # 6. Tips
        self._generate_tips(r1, users, org1, R1_RESULTS, accuracy=0.75)
        self._generate_tips(r2, users, org1, R2_RESULTS, accuracy=0.72)
        # R3 (open): 8 of 12 users have started tipping
        self._generate_tips(r3, users[:8], org1, None, accuracy=None, partial=True)
        # R4: no tips yet (upcoming)

        # 7. Record results — grades all tips on R1 + R2
        self._record_results(r1, R1_RESULTS)
        self._record_results(r2, R2_RESULTS)

        # 8. Org 2 — one complete round, smaller crew
        r5 = self._make_round(org2, 1, "AFL", now - timedelta(days=3), "complete")
        self._make_matches(r5, R1_FIXTURES[:3], now - timedelta(days=3, hours=-2), "demo2-r1")
        self._generate_tips(r5, users[:7], org2, R1_RESULTS[:3], accuracy=0.70)
        self._record_results(r5, R1_RESULTS[:3])

        totals = {
            "users": User.objects.count(),
            "orgs": Organisation.objects.count(),
            "rounds": Round.objects.count(),
            "matches": Match.objects.count(),
            "tips": Tip.objects.count(),
        }
        self.stdout.write(self.style.SUCCESS(
            f"Demo seeded → users={totals['users']} orgs={totals['orgs']} "
            f"rounds={totals['rounds']} matches={totals['matches']} tips={totals['tips']}"
        ))

    def _make_round(self, org, num, comp, lockout, status):
        competition = Competition.objects.get(name=comp)
        return Round.objects.create(
            org=org, round_number=num, competition=competition,
            lockout_at=lockout, status=status,
        )

    def _make_matches(self, round_obj, fixtures, base_kickoff, id_prefix):
        for i, (home_slug, away_slug, venue) in enumerate(fixtures):
            home = Team.objects.get(slug=home_slug, competition=round_obj.competition)
            away = Team.objects.get(slug=away_slug, competition=round_obj.competition)
            Match.objects.create(
                round=round_obj, home_team=home, away_team=away,
                kickoff_at=base_kickoff + timedelta(hours=i * 3),
                venue=venue,
                external_id=f"{id_prefix}-{i}",
            )

    def _generate_tips(self, round_obj, users, org, winners, accuracy, partial=False):
        matches = list(round_obj.matches.order_by("external_id"))
        for u in users:
            for i, m in enumerate(matches):
                if partial and random.random() < 0.25:
                    continue
                if winners is not None:
                    hs, as_ = winners[i]
                    winner_side = "home" if hs > as_ else "away"
                    if random.random() < accuracy:
                        selection = winner_side
                    else:
                        selection = "away" if winner_side == "home" else "home"
                else:
                    selection = random.choice(["home", "away"])
                Tip.objects.create(user=u, match=m, org=org, selection=selection)

    def _record_results(self, round_obj, results):
        for i, m in enumerate(round_obj.matches.order_by("external_id")):
            hs, as_ = results[i]
            record_match_result(m, hs, as_)
