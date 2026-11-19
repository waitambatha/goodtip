"""Fill a LOCAL Wall with four weeks of demo conversation (client, 9 Nov 2026:
"seed data, with images, for the last 4 weeks, the wins and losses, have
replies and all, so I can see how it will look").

Built on the real graded results already in the database: six demo members
(…@demo.goodtip.test) tip each round, real recaps are composed from those tips
by the same code the recap job uses, and the rooms fill with talk about the
actual games — pictures, replies, replies to replies and reactions — plus a
few lines from you so both sides of the chat show.

Nothing is sent: no notifications, no email. Everything made is listed in
MEDIA_ROOT/wall_demo.json and `--clear` removes exactly that.

LOCAL ONLY. Refuses unless DEBUG is on and the database is on this machine.

    python manage.py seed_wall_demo --org 53
    python manage.py seed_wall_demo --org 53 --clear
"""
import json
import random
import shutil
from datetime import timedelta
from io import BytesIO
from pathlib import Path

from django.conf import settings
from django.core.files.base import ContentFile
from django.core.management.base import BaseCommand, CommandError
from django.db import transaction
from django.utils import timezone

DEMO_DOMAIN = "demo.goodtip.test"
PEOPLE = [
    ("Jess Carter", "jess"), ("Liam Nguyen", "liam"), ("Priya Shah", "priya"),
    ("Tom Walsh", "tom"), ("Mia Rossi", "mia"), ("Kofi Mensah", "kofi"),
]
PREFERRED_ROOMS = ["epl", "a-league-men", "nba", "aflw", "la-liga", "nfl", "bundesliga", "serie-a"]
FONT = "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf"
SCENES = Path(settings.BASE_DIR) / "static" / "img" / "scenes"


def _font(size):
    from PIL import ImageFont

    try:
        return ImageFont.truetype(FONT, size)
    except OSError:
        return ImageFont.load_default(size=size)


def _hex(c):
    c = c.lstrip("#")
    return tuple(int(c[i:i + 2], 16) for i in (0, 2, 4))


def scoreboard_png(series, match) -> bytes:
    """A full-time score graphic, in the competition's colour — the kind of
    screenshot people actually post."""
    from PIL import Image, ImageDraw

    w, h = 1080, 640
    base = _hex(series.colour)
    img = Image.new("RGB", (w, h), base)
    d = ImageDraw.Draw(img)
    for y in range(h):  # a soft vertical fade
        k = y / h
        d.line([(0, y), (w, y)], fill=tuple(int(c * (1 - .45 * k)) for c in base))
    d.rounded_rectangle([60, 60, w - 60, h - 60], 36, outline=(255, 255, 255), width=4)
    d.text((w / 2, 120), f"{series.name.upper()}  ·  FULL TIME", font=_font(34), fill=(255, 255, 255), anchor="mm")
    hs = "–" if match.home_score is None else str(match.home_score)
    as_ = "–" if match.away_score is None else str(match.away_score)
    d.text((w / 2, 320), f"{hs}  –  {as_}", font=_font(150), fill=(255, 255, 255), anchor="mm")
    for x, team, won in ((w * .27, match.home_team.name, match.result == "home"),
                         (w * .73, match.away_team.name, match.result == "away")):
        d.text((x, 470), team[:22], font=_font(40 if won else 36),
               fill=(201, 242, 74) if won else (235, 240, 236), anchor="mm")
    buf = BytesIO()
    img.save(buf, "PNG")
    return buf.getvalue()


def scene_jpg(name) -> bytes | None:
    p = SCENES / name
    return p.read_bytes() if p.exists() else None


class Command(BaseCommand):
    help = "Seed a local Wall with four weeks of demo posts, recaps, replies and pictures."

    def add_arguments(self, parser):
        parser.add_argument("--org", type=int, help="one organisation")
        parser.add_argument("--all", action="store_true",
                            help="every organisation with graded games (its own ledger and --clear)")
        parser.add_argument("--weeks", type=int, default=4)
        parser.add_argument("--rooms", type=int, default=0, help="how many competitions to fill (0 = all with games)")
        parser.add_argument("--clear", action="store_true", help="remove what a previous run made")

    # ------------------------------------------------------------------ guard
    def _guard(self):
        host = str(settings.DATABASES["default"].get("HOST", ""))
        if not settings.DEBUG or host not in ("localhost", "127.0.0.1", "::1", ""):
            raise CommandError("seed_wall_demo is for a local database only (needs DEBUG and a local DB host).")

    all_orgs = False

    @property
    def ledger_path(self):
        return Path(settings.MEDIA_ROOT) / ("wall_demo_all.json" if self.all_orgs else "wall_demo.json")

    @property
    def other_ledger(self):
        return Path(settings.MEDIA_ROOT) / ("wall_demo.json" if self.all_orgs else "wall_demo_all.json")

    def handle(self, *args, **opts):
        self._guard()
        self.all_orgs = opts["all"]
        if not opts["all"] and not opts["org"]:
            raise CommandError("Give --org <id> or --all.")
        if opts["clear"]:
            return self._clear()
        if self.ledger_path.exists():
            raise CommandError("Demo data is already there. Run with --clear first.")
        self._seed(opts)

    # ------------------------------------------------------------------ clear
    def _clear(self):
        from accounts.models import User
        from orgs.models import RoundRecap, WallPost

        if not self.ledger_path.exists():
            self.stdout.write("Nothing to clear.")
            return
        led = json.loads(self.ledger_path.read_text())
        with transaction.atomic():
            RoundRecap.objects.filter(pk__in=led["recaps"]).delete()
            for p in WallPost.objects.filter(pk__in=led["posts"]):
                if p.image:
                    p.image.delete(save=False)
            WallPost.objects.filter(pk__in=led["posts"]).delete()
            if not self.other_ledger.exists():
                User.objects.filter(pk__in=led["users"], email__endswith="@" + DEMO_DOMAIN).delete()
            else:
                from orgs.models import OrgMember
                OrgMember.objects.filter(user_id__in=led["users"], org_id__in=led.get("orgs", [])).delete()
        self.ledger_path.unlink()
        self.stdout.write(self.style.SUCCESS(
            f"Removed {len(led['posts'])} posts, {len(led['recaps'])} recaps and {len(led['users'])} demo members."))

    # ------------------------------------------------------------------- seed
    def _seed(self, opts):
        from accounts.models import User
        from orgs.models import OrgMember, Organisation, RoundRecap, WallPost, WallReaction, WallReply
        from orgs.recaps import (
            RECAP_ENGINE, build_recap_facts, compose_recap, fallback_line, leaderboard_snapshot,
            round_ready_for_recap,
        )
        from tipping.models import Round, Tip

        rng = random.Random(9112026)
        now = timezone.now()
        since = now - timedelta(weeks=opts["weeks"])
        led = {"users": [], "posts": [], "recaps": [], "orgs": []}
        if opts["all"]:
            busy = set(Round.objects.filter(lockout_at__gte=since, lockout_at__lte=now,
                                            matches__result__isnull=False).values_list("org_id", flat=True))
            if self.other_ledger.exists():           # the single-org seed already filled that one
                busy -= set(json.loads(self.other_ledger.read_text()).get("orgs", []) or [])
            orgs = list(Organisation.objects.filter(pk__in=busy).order_by("pk"))
        else:
            orgs = [Organisation.objects.get(pk=opts["org"])]
        totals = []
        for org in orgs:
            try:
                chosen, games = self._seed_org(org, opts, rng, now, since, led)
            except CommandError as e:
                self.stdout.write(f"  {org.name}: {e}")
                continue
            led["orgs"].append(org.pk)
            totals.append((org.name, chosen, len(games)))
            self.stdout.write(f"  {org.name}: {', '.join(chosen)} ({len(games)} new game recaps)")
        led["users"] = sorted(set(led["users"]))
        self.ledger_path.parent.mkdir(parents=True, exist_ok=True)
        self.ledger_path.write_text(json.dumps(led))
        self.stdout.write(self.style.SUCCESS(
            f"Seeded {len(totals)} organisation(s): {len(led['posts'])} posts and {len(led['recaps'])} round recaps. "
            f"Undo with --clear{' --all' if opts['all'] else ''}."))

    def _seed_org(self, org, opts, rng, now, since, led):
        from accounts.models import User
        from orgs.models import OrgMember, RoundRecap, WallPost, WallReaction, WallReply
        from orgs.recaps import (
            RECAP_ENGINE, build_recap_facts, compose_recap, fallback_line, leaderboard_snapshot,
            round_ready_for_recap,
        )
        from tipping.models import Round, Tip

        with transaction.atomic():
            # -- the demo members
            people = []
            for name, slug in PEOPLE:
                u, _ = User.objects.get_or_create(
                    email=f"{slug}@{DEMO_DOMAIN}", defaults={"display_name": name, "username": f"{slug}@{DEMO_DOMAIN}"},
                )
                u.set_unusable_password()
                u.save()
                OrgMember.objects.get_or_create(user=u, org=org, defaults={"role": OrgMember.ROLE_PARTICIPANT})
                people.append(u)
                led["users"].append(u.pk)
            # "Your" side of the chat is only ever the organisation owner when that is the person
            # running this seed's own account on the single-org seed; in --all, demo members only,
            # so nothing is ever written in a real member's name.
            me = None
            if not self.all_orgs:
                m = OrgMember.objects.filter(org=org, is_league_owner=True).select_related("user").first()
                me = m.user if m else None

            # -- which rooms: competitions with graded rounds in the window
            rounds = list(
                Round.objects.filter(org=org, lockout_at__gte=since, lockout_at__lte=now)
                .select_related("series").prefetch_related("matches__home_team", "matches__away_team")
                .order_by("lockout_at")
            )
            rounds = [r for r in rounds if round_ready_for_recap(r)]
            by_series = {}
            for r in rounds:
                by_series.setdefault(r.series.slug, []).append(r)
            chosen = [s for s in PREFERRED_ROOMS if s in by_series] + sorted(s for s in by_series if s not in PREFERRED_ROOMS)
            if opts["rooms"]:
                chosen = chosen[: opts["rooms"]]
            if not chosen:
                raise CommandError("No graded rounds in that window to talk about.")

            def post(series, author, body, when, image=None, tip=None):
                p = WallPost.objects.create(org=org, group=None, author=author, body=body,
                                            series=series, kind=WallPost.KIND_MEMBER, tip=tip)
                if image:
                    p.image.save(image[0], ContentFile(image[1]), save=True)
                WallPost.objects.filter(pk=p.pk).update(created_at=when)
                led["posts"].append(p.pk)
                return p

            def reply(p, author, body, when, to=None):
                r = WallReply.objects.create(post=p, author=author, body=body, reply_to=to)
                WallReply.objects.filter(pk=r.pk).update(created_at=when)
                return r

            def react(p, users):
                for u in users:
                    WallReaction.objects.get_or_create(
                        post=p, user=u, defaults={"emoji": rng.choice(["fire", "laugh", "heart", "clap", "eyes"])})

            scenes = sorted(x.name for x in SCENES.glob("*.jpg") if x.name.count(".") == 1)

            for slug in chosen:
                for rnd in by_series[slug][-4:]:
                    series = rnd.series
                    games = [m for m in rnd.matches.all() if m.result]
                    if not games:
                        continue
                    kick = min(m.kickoff_at for m in games)
                    end = max(m.kickoff_at for m in games) + timedelta(hours=3)

                    # -- tips: mostly sensible, sometimes wrong
                    for u in people:
                        for m in games:
                            right = rng.random() < .58
                            options = ["home", "away"] + (["draw"] if m.result == "draw" else [])
                            sel = m.result if right else rng.choice([o for o in options if o != m.result] or ["home"])
                            Tip.objects.update_or_create(
                                user=u, match=m, org=org, group=None,
                                defaults={"selection": sel, "is_auto": False, "is_correct": sel == m.result,
                                          "points_awarded": rnd.points_per_correct if sel == m.result else 0},
                            )

                    # -- the recap, by the recap job's own code
                    if not RoundRecap.objects.filter(org=org, group=None, round=rnd).exists():
                        facts = build_recap_facts(org, rnd, None)
                        if facts:
                            text = compose_recap(facts) or fallback_line(facts)
                            rp = WallPost.objects.create(org=org, group=None, kind=WallPost.KIND_RECAP, body=text)
                            WallPost.objects.filter(pk=rp.pk).update(created_at=end + timedelta(hours=10))
                            rc = RoundRecap.objects.create(
                                org=org, group=None, round=rnd, post=rp, model_used=RECAP_ENGINE,
                                talking_points=[], leaderboard=leaderboard_snapshot(facts),
                            )
                            led["posts"].append(rp.pk)
                            led["recaps"].append(rc.pk)
                            for i, u in enumerate(rng.sample(people, 3)):
                                reply(rp, u, rng.choice([
                                    "Finally on the board this week.",
                                    "That table is lying, I'm coming for top spot.",
                                    "How did nobody pick that upset?",
                                    "Consistent. That's all it is.",
                                ]), end + timedelta(hours=11 + i))
                            react(rp, rng.sample(people, rng.randint(2, 5)))

                    # -- before the round
                    g = rng.choice(games)
                    a, b = rng.sample(people, 2)
                    p1 = post(series, a, rng.choice([
                        f"{g.home_team.name} v {g.away_team.name} this round. Who's brave enough to go against the home side?",
                        f"Locking in {g.home_team.name}. Talk me out of it.",
                        f"Big call: {g.away_team.name} on the road. Anyone with me?",
                    ]), kick - timedelta(hours=rng.randint(10, 30)))
                    r1 = reply(p1, b, rng.choice(["Not a chance.", "I'm with you on that one.", "Bold. I like it."]),
                               kick - timedelta(hours=8))
                    reply(p1, a, "We'll see who's laughing on Monday.", kick - timedelta(hours=7), to=r1)
                    react(p1, rng.sample(people, 2))

                    # -- after the round: the result everyone talks about
                    upset = next((m for m in games if m.result == "away"), games[0])
                    winner = upset.home_team.name if upset.result == "home" else upset.away_team.name
                    loser = upset.away_team.name if upset.result == "home" else upset.home_team.name
                    c, d_ = rng.sample(people, 2)
                    p2 = post(series, c, rng.choice([
                        f"{winner} over {loser}. Did not see that coming.",
                        f"Full time. {loser} fans, are we ok?",
                        f"That {winner} result cost me two spots on the ladder.",
                    ]), end + timedelta(hours=1),
                        image=(f"ft-{slug}-{rnd.round_number}.png", scoreboard_png(series, upset)))
                    for i, u in enumerate(rng.sample(people, 3)):
                        reply(p2, u, rng.choice([
                            "Screenshot of the year.",
                            f"I had {winner}. Quietly confident all week.",
                            "Still not over it.",
                            "The table after this is going to be chaos.",
                        ]), end + timedelta(hours=2 + i))
                    react(p2, rng.sample(people, rng.randint(3, 6)))

                    # -- you, so your side of the chat shows
                    if me:
                        my_tip = Tip.objects.filter(user=me, match__in=games, org=org).first()
                        p4 = post(series, me, rng.choice([
                            f"{winner} have been the story of this round.",
                            "Who's buying the coffees after that round?",
                            "Next round I'm going all in on the underdogs.",
                        ]), end + timedelta(hours=5), tip=my_tip)
                        r = reply(p4, rng.choice(people), "Count me in.", end + timedelta(hours=6))
                        reply(p4, me, "Deal.", end + timedelta(hours=6, minutes=20), to=r)
                        react(p4, rng.sample(people, 2))

        # Each game written up, with its photos, as the jobs loop will do live.
        # Final Whistle stories are written once for every organisation by the
        # jobs loop (generate_match_stories); the seed only adds the talk.
        games = []

        return chosen, games
