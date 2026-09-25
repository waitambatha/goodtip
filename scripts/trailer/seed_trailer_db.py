"""Fill goodtip_trailer with an invented club, so the public trailer can be filmed.

Run through scripts/trailer/reset.sh, which builds the database first and points
DATABASE_URL at it. Refuses to run against anything else: this script creates
users, wipes nothing, and copies fixtures from a real league, so aimed at the
wrong database it would do real damage.

Everything a viewer will read is invented -- names, club, wall posts, chat. The
only thing taken from real data is the SPORTS: the 2026 AFL/NRL draw and its
final scores, which are public and are what makes the ladder and results look
true. Those are copied from one existing league's rounds and matches (no tips,
no members) into the invented club.
"""
import os
import random
import sys
from datetime import timedelta

import django

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))
os.environ.setdefault("DJANGO_SETTINGS_MODULE", "goodtip.settings")

if not os.environ.get("DATABASE_URL", "").rstrip("/").endswith("/goodtip_trailer"):
    sys.exit("Refusing to run: DATABASE_URL is not the goodtip_trailer database.")

django.setup()

from django.conf import settings  # noqa: E402
from django.db import transaction  # noqa: E402
from django.utils import timezone  # noqa: E402

from accounts.models import User  # noqa: E402
from accounts.onboarding import PAGE_TOURS  # noqa: E402
from admin_panel.models import NewsPost  # noqa: E402
from catalog.models import Charity, Competition, OrganisationType, Season, State  # noqa: E402
from orgs.models import (  # noqa: E402
    CharityVote, CharityVoteBallot, CharityVoteOption, Group, GroupMember,
    Message, OrgMember, Organisation, WallPost, WallReaction, WallReply,
)
from orgs.services import group_room, org_room  # noqa: E402
from tipping.models import Match, Round, Tip  # noqa: E402
from tipping.services import record_match_result  # noqa: E402

PASSWORD = "Trailer#2026"
ORG_NAME = "Riverbend Rovers Footy Club"
# The league whose DRAW is copied (sports data only). Any org that tips AFL and
# NRL will do; this is read from the source database and never written to.
SOURCE_ORG_ID = int(os.environ.get("TRAILER_SOURCE_ORG", "63"))

random.seed(2026)

# (email-local, display name, role, skill = how often they pick the winner)
CAST = [
    ("sam", "Sam Whitfield", OrgMember.ROLE_BOTH, 0.70),       # the star of the clips
    ("jess", "Jess Nguyen", OrgMember.ROLE_CAPTAIN, 0.66),
    ("tom", "Tom Reddy", OrgMember.ROLE_PARTICIPANT, 0.61),
    ("priya", "Priya Nair", OrgMember.ROLE_PARTICIPANT, 0.68),
    ("marcus", "Marcus Bell", OrgMember.ROLE_MANAGER, 0.58),
    ("aroha", "Aroha Tane", OrgMember.ROLE_PARTICIPANT, 0.64),
    ("dan", "Dan Okafor", OrgMember.ROLE_PARTICIPANT, 0.55),
    ("mia", "Mia Kowalski", OrgMember.ROLE_PARTICIPANT, 0.63),
    ("liam", "Liam Fitzgerald", OrgMember.ROLE_PARTICIPANT, 0.59),
    ("zoe", "Zoe Marchetti", OrgMember.ROLE_PARTICIPANT, 0.67),
    ("noah", "Noah Sutherland", OrgMember.ROLE_PARTICIPANT, 0.52),
    ("ellie", "Ellie Papadopoulos", OrgMember.ROLE_PARTICIPANT, 0.65),
    ("ravi", "Ravi Menon", OrgMember.ROLE_PARTICIPANT, 0.60),
    ("hannah", "Hannah Doyle", OrgMember.ROLE_PARTICIPANT, 0.57),
    ("kaleb", "Kaleb Turner", OrgMember.ROLE_PARTICIPANT, 0.62),
    ("isla", "Isla McBride", OrgMember.ROLE_PARTICIPANT, 0.56),
]

GROUPS = {
    "Under 18s": ["jess", "ellie", "kaleb", "isla", "noah", "sam"],
    "Committee": ["sam", "marcus", "priya", "tom"],
    "Social Crew": ["zoe", "mia", "liam", "hannah", "dan", "ravi", "aroha", "sam"],
}


def make_users():
    users = {}
    seen = list(PAGE_TOURS.values()) + ["dashboard_empty", "dashboard"]
    for local, name, _role, _skill in CAST:
        u, _ = User.objects.get_or_create(
            email=f"{local}@riverbend.example",
            defaults={"username": f"{local}@riverbend.example"},
        )
        u.display_name = name
        u.is_active = True
        # Sam is the only one who signs in by password in the clips, and the
        # code step is part of what is shown. Everyone else is filmed via a
        # session cookie, so the setting is moot for them.
        u.two_factor_enabled = local == "sam"
        u.email_verified_at = timezone.now()
        u.onboarding_seen_at = timezone.now()
        u.onboarding_pages_seen = sorted(set(seen))
        u.set_password(PASSWORD)
        u.save()
        users[local] = u
    return users


def make_org(users):
    season = Season.objects.get(year=2026)
    org, _ = Organisation.objects.get_or_create(
        name=ORG_NAME, season=season,
        defaults={
            "organisation_type": OrganisationType.objects.get(name="Community"),
            "state": State.objects.get(code="VIC"),
            "charity": Charity.objects.get(name="Beyond Blue"),
            "is_public_listed": True,
            "team_size": 120,
            "groups_enabled": True,
            "approval_status": Organisation.APPROVAL_APPROVED,
            "created_by": users["sam"],
            "terms_accepted_at": timezone.now(),
            "terms_accepted_by": users["sam"],
            "terms_version": "trailer",
        },
    )
    afl = Competition.objects.get(series__name="AFL", season=season)
    nrl = Competition.objects.get(series__name="NRL", season=season)
    org.competitions.set([afl, nrl])
    for local, _n, role, _s in CAST:
        OrgMember.objects.update_or_create(
            user=users[local], org=org,
            defaults={"role": role, "is_league_owner": local == "sam",
                      "invited_by": users["sam"]},
        )
    return org


def copy_draw(org):
    """Copy the AFL/NRL rounds and matches from the source league into `org`.

    The whole draw is slid forward by a whole number of weeks so that AFL round
    24 -- a full nine-game round -- is the one about to be played. Today's real
    fixtures are finals, which are one or two games a round and would leave the
    tipping screen nearly empty. Whole weeks keep every game on its own weekday
    and kick-off time; nothing on screen says which week of the season it is.

    Scores are held back and applied afterwards through record_match_result, so
    the points on every tip come from the real grading code rather than being
    written by hand. Only games that end up in the past are scored.
    """
    src = "source"
    now = timezone.now()
    anchor = min(
        Match.objects.using(src)
        .filter(round__org_id=SOURCE_ORG_ID, round__series__name="AFL", round__round_number=24)
        .values_list("kickoff_at", flat=True))
    weeks = -(-((now + timedelta(hours=20)) - anchor).total_seconds() // (7 * 86400))
    shift = timedelta(weeks=int(weeks))

    scores = {}
    for rnd in Round.objects.using(src).filter(org_id=SOURCE_ORG_ID):
        matches = list(Match.objects.using(src).filter(round=rnd))
        new_round = Round(**{
            f.attname: getattr(rnd, f.attname)
            for f in Round._meta.concrete_fields if f.name != "id"
        })
        new_round.org = org
        new_round.lockout_at = rnd.lockout_at + shift
        new_round.results_email_sent_at = now          # nothing here may mail
        new_round.status = "complete" if new_round.lockout_at <= now else "upcoming"
        new_round.save()
        for m in matches:
            data = {f.attname: getattr(m, f.attname)
                    for f in Match._meta.concrete_fields if f.name != "id"}
            hs, as_, had = data["home_score"], data["away_score"], data["result"]
            data.update(round_id=new_round.id, home_score=None, away_score=None,
                        result=None, kickoff_at=m.kickoff_at + shift)
            played = data["kickoff_at"] <= now
            if not played:
                data.update(status=Match.STATUS_SCHEDULED, period="", clock="",
                            progress=0, live_updated_at=None)
            nm = Match.objects.create(**data)
            if played and had is not None and hs is not None and as_ is not None:
                scores[nm.id] = (hs, as_)
    return scores


def make_tips(org, users, scores):
    """Tips for every finished match, in each member's own skill; the open
    rounds are tipped by everyone but Sam, so the tipping clip has work to do."""
    skill = {local: s for local, _n, _r, s in CAST}
    finished = list(Match.objects.filter(round__org=org, id__in=scores))
    tips = []
    for m in finished:
        hs, as_ = scores[m.id]
        winner = "home" if hs > as_ else "away" if as_ > hs else None
        for local, user in users.items():
            if random.random() < 0.06:            # everybody misses one now and then
                continue
            if winner is None:
                pick = random.choice(("home", "away"))
            else:
                loser = "away" if winner == "home" else "home"
                pick = winner if random.random() < skill[local] else loser
            tips.append(Tip(user=user, match=m, org=org, selection=pick))
    Tip.objects.bulk_create(tips, batch_size=2000)
    for m in finished:                             # grade through the real code
        record_match_result(m, *scores[m.id])

    now = timezone.now()
    upcoming = Match.objects.filter(round__org=org, kickoff_at__gt=now, result__isnull=True)
    future = []
    for m in upcoming:
        for local, user in users.items():
            if local == "sam" or random.random() < 0.35:
                continue
            future.append(Tip(user=user, match=m, org=org,
                              selection=random.choice(("home", "away"))))
    Tip.objects.bulk_create(future, batch_size=2000)
    return len(tips) + len(future)


def make_groups(org, users):
    for name, members in GROUPS.items():
        g, _ = Group.objects.get_or_create(
            org=org, name=name,
            defaults={"created_by": users["sam"],
                      "approval_status": Group.APPROVAL_APPROVED},
        )
        for local in members:
            GroupMember.objects.get_or_create(
                group=g, user=users[local], defaults={"is_admin": local == "sam"})


WALL = [
    ("jess", "Round 27 and the Rovers Under 18s are still undefeated in the tipping comp. Somebody has to stop us. 🏆", True, ("fire", "clap", "clap"), None),
    ("tom", "Called the Cats upset last week and nobody believed me. Screenshots or it didn't happen.", True, ("laugh", "eyes", "fire"), "Bold tip. Respect."),
    ("priya", "Reminder: tips lock at first bullet each round. Don't be the one who forgets. Again. Looking at you, Dan.", False, ("laugh", "laugh"), None),
    ("sam", "Vote on our charity for the season is open now. Every tip this year counts towards something that matters. Have your say. ❤️", True, ("heart", "heart", "clap", "fire"), "Voted. Easy choice."),
    ("aroha", "Family tipping night at mine on Friday. Bring snacks, bring a pen, bring your best excuses.", False, ("heart", "fire"), None),
    ("zoe", "Fourth week in a row inside the top three. I'm not saying I'm good, I'm saying the ladder is.", True, ("fire", "eyes", "laugh"), "Enjoy it while it lasts 😅"),
    ("marcus", "Thanks to everyone who's joined this year. 16 of us and it's the best the club has been in ages.", True, ("clap", "heart", "clap"), None),
]


def make_wall(org, users):
    t = timezone.now()
    for i, (author, body, public, reacts, reply) in enumerate(WALL):
        post = WallPost.objects.create(
            org=org, author=users[author], body=body, is_public=public)
        WallPost.objects.filter(pk=post.pk).update(
            created_at=t - timedelta(hours=3 + i * 5))
        others = [u for k, u in users.items() if k != author]
        random.shuffle(others)
        for emoji, u in zip(reacts, others):
            WallReaction.objects.get_or_create(post=post, user=u, emoji=emoji)
        if reply:
            WallReply.objects.create(
                post=post, author=others[-1], body=reply, is_approved=True)


def make_chat(org, users):
    room = org_room(org)
    lines = [
        ("marcus", "Team sheet for Saturday is up. Kick-off's 1:20, so be there by noon."),
        ("jess", "Can someone bring oranges this week?"),
        ("tom", "On it 🍊"),
        ("priya", "Who's tipping the Lions this round? I'm torn."),
        ("aroha", "Lions all day. Trust me."),
        ("sam", "Careful, last time you said that I lost a round 😂"),
        ("zoe", "MatchReader has them at 71% though. I'm going with that."),
        ("dan", "Bold of you all to assume I remembered to tip at all"),
        ("sam", "Locks in two hours, Dan."),
    ]
    now = timezone.now()
    for i, (who, body) in enumerate(lines):
        m = Message.objects.create(thread=room, author=users[who], body=body)
        Message.objects.filter(pk=m.pk).update(
            created_at=now - timedelta(minutes=(len(lines) - i) * 9))
    grp = group_room(Group.objects.get(org=org, name="Committee"))
    for i, (who, body) in enumerate([
        ("marcus", "Charity vote closes Friday. Can we nudge the quiet ones?"),
        ("priya", "I'll post on the Wall tonight."),
        ("sam", "Perfect. Thank you both."),
    ]):
        m = Message.objects.create(thread=grp, author=users[who], body=body)
        Message.objects.filter(pk=m.pk).update(
            created_at=now - timedelta(minutes=(3 - i) * 14))


def make_vote(org, users):
    now = timezone.now()
    vote = CharityVote.objects.create(
        org=org, status=CharityVote.STATUS_OPEN,
        scheduled_close_at=now + timedelta(days=2, hours=3),
        admin_message="Pick the cause your tips will back this season.",
    )
    options = {}
    for name in ("Beyond Blue", "Lifeline", "headspace", "ReachOut"):
        options[name] = CharityVoteOption.objects.create(
            vote=vote, charity=Charity.objects.get(name=name))
    leans = ["Beyond Blue"] * 4 + ["Lifeline"] * 3 + ["headspace"] * 2 + ["ReachOut"]
    voters = [k for k in users if k != "sam"]
    random.shuffle(voters)
    for local, pick in zip(voters, leans):
        CharityVoteBallot.objects.create(vote=vote, user=users[local], option=options[pick])


# Invented stories for the public News page. The pictures are the site's own
# stock scenes, copied to media/news/ (which git ignores) so the cards have art.
NEWS = [
    ("Round 20 tips: who to back when the ladder is this tight", ["AFL"], "scenes/mcg-match.jpg",
     "Five games, six points between second and seventh. Here is how our members are leaning."),
    ("Finals fever: how a footy club raised $4,200 from one tipping comp", ["NRL", "BLOG"], "scenes/nrl-players-fans.jpg",
     "A suburban club, sixty tippers, one charity vote. What happened next surprised everyone."),
    ("AFLW: the young guns to watch this month", ["AFLW"], "scenes/afl-player-training.jpg",
     "Three first-year players who are changing the way their sides move the ball."),
    ("NRLW round preview: the tips that could decide your comp", ["NRLW"], "scenes/nrl-ground-dusk.jpg",
     "A big weekend for the women's game, with a tipping angle on every match."),
    ("Six ways to get your workplace tipping this season", ["BLOG"], "scenes/stadium-lights-grass.jpg",
     "Setting up an office comp takes a minute. Keeping it fun takes a little more."),
    ("Where the money goes: this season's charity partners", ["NEWS"], "scenes/aussie-crowd-flag.jpg",
     "Beyond Blue, Lifeline, headspace and ReachOut. What each one does with your tips."),
]


def make_news():
    import shutil
    if NewsPost.objects.exists():
        return
    dest = settings.MEDIA_ROOT / "news"
    dest.mkdir(parents=True, exist_ok=True)
    t = timezone.now()
    for i, (title, tags, pic, excerpt) in enumerate(NEWS):
        name = f"trailer-{i + 1}.jpg"
        shutil.copyfile(settings.BASE_DIR / "static" / "img" / pic, dest / name)
        NewsPost.objects.create(
            title=title, tags=tags, tag=tags[0], excerpt=excerpt,
            body=f"<p>{excerpt}</p>", image=f"news/{name}", image_alt=title,
            published_at=t - timedelta(days=i * 2 + 1),
        )
    print(f"{len(NEWS)} stories")


@transaction.atomic
def main():
    if "--news-only" in sys.argv:
        make_news()
        return
    settings.DATABASES["source"] = {
        **settings.DATABASES["default"],
        "NAME": os.environ.get("SOURCE_DB_NAME", "goodtip"),
    }
    users = make_users()
    org = make_org(users)
    if not Round.objects.filter(org=org).exists():
        scores = copy_draw(org)
        n = make_tips(org, users, scores)
        print(f"draw copied, {len(scores)} results graded, {n} tips")
    make_groups(org, users)
    make_wall(org, users)
    make_chat(org, users)
    make_vote(org, users)
    make_news()
    print(f"org id={org.id}: {org.members.count()} members, "
          f"{Round.objects.filter(org=org).count()} rounds")
    print(f"login: sam@riverbend.example / {PASSWORD}")


if __name__ == "__main__":
    main()
