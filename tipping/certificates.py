"""Season Champion Certificates (Spec addendum, Ian Hopkinson, 14 Aug 2026).

WHAT THE SPEC ASKS FOR, AND WHERE EACH PART LIVES HERE
  * §2 season champion only — one certificate per internal leaderboard the
    organisation runs. AFL, AFLW, NRL and NRLW are scored as separate boards,
    so up to four a season, all four the same design and the same status.
    -> CERT_SERIES, competitions_for()
  * §2 co-champions (tiebreaker spec step 3) each get their OWN certificate,
    same title, same wording — no split billing, no asterisk.
    -> champions(): everyone still sharing rank 1 after apply_tiebreakers
  * §2 content is the win only: no charity total, no donation figure, no
    org-wide stats. -> Certificate carries name, competition, year, org and
    the season's close; nothing else exists on it to leak.
  * §3 fields: winner display name; "2026 AFL Season Champion" (comp + year,
    never a round number); organisation name; issue context (the season's
    final round, once the board is locked). -> Certificate
  * §4 trigger: only once that competition's season is final and locked —
    never speculatively mid-season. -> season_state()
  * §5 two exports, same content: a landscape PDF for print and a landscape
    PNG to post (client, 13 Oct 2026: the square and portrait story versions
    went — "it must look exactly as that image"). -> render_pdf(), render_png()

"FINAL AND LOCKED". There is no separate "close the season" switch in the
data, so it is read from the fixtures themselves: the GRAND FINAL has been
played and graded, and so has every other match the organisation's season
holds for that competition (a postponed match that never got a result is the
only exception — it was not played, so nothing waits on it).

The grand final is recognised as the competition's latest finals round having
exactly ONE match — true of the AFL, AFLW, NRL and NRLW alike, and of no other
finals week in any of them. It has to be that specific: finals fixtures arrive
a week at a time as the teams become known, so "nothing left ungraded" is also
briefly true after every earlier finals week, and a certificate issued then
would name a champion with games still to play. That is the same moment the
tiebreaker rules can name a champion: before it, a later result could still
change the top of the board, so a certificate would be a guess.
"""
from __future__ import annotations

import io
import os
from dataclasses import dataclass
from datetime import datetime

from django.utils import timezone

#: The internal leaderboards that get a certificate (§2). State of Origin and
#: the others are not separate season boards in the spec's sense.
CERT_SERIES = ("AFL", "AFLW", "NRL", "NRLW")

#: One accent per competition, the same four the site uses for them, deep
#: enough to carry white type and to print.
ACCENTS = {
    "AFL": "#1F6FD1",
    "AFLW": "#C2306B",
    "NRL": "#C9700A",
    "NRLW": "#6B34B8",
}
FOREST = "#2A7337"

# The certificate is the client's own artwork (an AI-generated banner they
# supplied on 12 Oct 2026, "the certificate must look exactly as that image").
# certificate_assets/ holds it with the sample person and the per-certificate
# words taken out: the template, the photo frame's shape as a mask, and the
# frame's lime outline. Each certificate is that artwork with the champion's
# own photo and details put back in — so the preview, the PNG and the PDF are
# all the same picture.
ASSET_DIR = os.path.join(os.path.dirname(__file__), "certificate_assets")
FONT_DIR = os.path.join(os.path.dirname(__file__), "certificate_fonts")
FONTS = {
    "name": "Montserrat-ExtraBold.ttf",
    "initials": "Montserrat-Bold.ttf",
    "pill": "Barlow-Bold.ttf",
    "text": "Barlow-Medium.ttf",
    "text_b": "Barlow-SemiBold.ttf",
}


# ---------------------------------------------------------------------------
# The rules: which competitions, whether a season is final, who won it
# ---------------------------------------------------------------------------

def competitions_for(org):
    """The certificate-bearing competitions this organisation actually runs —
    ones it has rounds for, in the spec's order."""
    from catalog.models import Series

    have = set(
        org.rounds.filter(series__name__in=CERT_SERIES).values_list("series__name", flat=True)
    )
    by_name = {s.name: s for s in Series.objects.filter(name__in=have)}
    return [by_name[n] for n in CERT_SERIES if n in by_name]


@dataclass
class SeasonState:
    final: bool
    reason: str                    # what to tell an admin while it is not
    closed_on: datetime | None     # kickoff of the season's last match
    last_round_label: str


def season_state(org, series) -> SeasonState:
    """Whether ``series``' season is final and locked for ``org`` (§4)."""
    from tipping.models import Match, Round

    rounds = Round.objects.filter(org=org, series=series)
    if not rounds.exists():
        return SeasonState(False, "No rounds yet.", None, "")
    matches = Match.objects.filter(round__in=rounds)
    if not matches.exists():
        return SeasonState(False, "No fixtures yet.", None, "")
    played = matches.exclude(status=Match.STATUS_POSTPONED, result__isnull=True)
    last = played.order_by("-kickoff_at").select_related("round").first()
    finals = rounds.filter(stage=Round.STAGE_FINALS)
    if not finals.exists():
        return SeasonState(False, "The season hasn't reached its finals yet.", None, "")
    latest_finals = max(finals, key=lambda r: (r.matches.order_by("-kickoff_at").values_list("kickoff_at", flat=True).first() or r.lockout_at, r.round_number))
    if latest_finals.matches.count() != 1:
        return SeasonState(False, "The finals are under way; the grand final hasn't been played yet.", None, "")
    waiting = played.filter(result__isnull=True).count()
    if waiting:
        return SeasonState(
            False,
            f"{waiting} match{'es' if waiting != 1 else ''} still to be played or graded.",
            None, "",
        )
    if last is None or last.kickoff_at > timezone.now():
        return SeasonState(False, "The season's last match hasn't been played.", None, "")
    # A certificate only exists after the grand final (see above), so that is
    # what the round is called on it — the app's own finals labels count weeks
    # from whichever finals rounds this league happens to hold, and a league
    # that joined mid-finals would otherwise print "Finals Week 2".
    return SeasonState(True, "", last.kickoff_at, "Grand Final")


#: Beyond the one organisation's own champion (client, 12 Oct 2026: "if my
#: organisation is the best... if it's me... irrespective of the type of
#: organisation... we should also get a certificate"). The scope decides who
#: "winner" names and how the headline reads; "org" is the original, unscoped
#: default so every certificate built before this addendum is unaffected.
SCOPE_ORG = "org"
SCOPE_INDUSTRY = "industry"
SCOPE_NATIONAL = "national"
SCOPE_INDIVIDUAL_NATIONAL = "individual_national"


@dataclass
class Certificate:
    org_name: str
    winner: str
    series: str
    year: int
    closed_on: datetime
    last_round_label: str
    user_id: int | None = None
    # A preview made before the season is final (client, 7 Oct 2026: "a test
    # button that showcases the certificate"). Same design, but it says SAMPLE
    # and "season in progress" on its face, and in its filename, so a
    # downloaded preview can never pass for a real champion's certificate.
    sample: bool = False
    scope: str = SCOPE_ORG
    #: The organisation type's name, for a SCOPE_INDUSTRY headline ("Best
    #: Community Organisation"). Unused by every other scope.
    scope_label: str = ""

    @property
    def headline(self) -> str:
        """§3 for the org's own champion; the wider scopes name what they
        beat instead of a round, since there is no single season to cite
        across organisations that each run their own."""
        if self.scope == SCOPE_INDUSTRY:
            return f"Best {self.scope_label} Organisation · {self.year} {self.series}"
        if self.scope == SCOPE_NATIONAL:
            return f"Best Organisation Nationwide · {self.year} {self.series}"
        if self.scope == SCOPE_INDIVIDUAL_NATIONAL:
            return f"Top Tipper Nationwide · {self.year} {self.series}"
        return f"{self.year} {self.series} Season Champion"

    @property
    def accent(self) -> str:
        return ACCENTS.get(self.series, FOREST)

    @property
    def closed_label(self) -> str:
        return timezone.localtime(self.closed_on).strftime("%-d %B %Y")

    def filename(self, ext: str) -> str:
        import re

        slug = re.sub(r"[^a-z0-9]+", "-", self.winner.lower()).strip("-") or "champion"
        tail = "-sample" if self.sample else ""
        return f"goodtip-{self.year}-{self.series.lower()}-champion-{slug}{tail}.{ext}"

    @property
    def footer_line(self) -> str:
        if self.sample:
            return "SAMPLE  ·  SEASON IN PROGRESS"
        return f"SEASON CLOSED {self.closed_label.upper()}  ·  {self.last_round_label.upper()}"


def champions(org, series) -> list[Certificate]:
    """Every season champion of ``series`` in ``org`` — one Certificate each.

    Empty until the season is final (§4). Uses the organisation's own ladder
    for that competition with the tiebreakers already specified (points, the
    paired comp, countback); anyone still sharing first after all of them is a
    co-champion and gets their own certificate, identical to the others (§2).
    Nobody is champion on zero points.
    """
    from tipping.services import leaderboard_for_org

    state = season_state(org, series)
    if not state.final:
        return []
    board = leaderboard_for_org(org, series=series)
    top = [u for u in board if getattr(u, "rank", None) == 1 and (u.points or 0) > 0]
    year = org.season.year if org.season_id else timezone.localtime(state.closed_on).year
    return [
        Certificate(
            org_name=org.name,
            winner=(u.display_name or u.email.split("@")[0]),
            series=series.name,
            year=year,
            closed_on=state.closed_on,
            last_round_label=state.last_round_label,
            user_id=u.id,
        )
        for u in top
    ]


def sample_certificate(org, series, viewer) -> Certificate:
    """A preview of ``series``'s certificate while its season is still going.

    It names the person asking for it, with their own profile photo (client,
    13 Oct 2026: "it should have my name and the image that I have as a
    profile") — it used to name whoever led the board, which read as somebody
    else's data. Never a champion: it carries ``sample``.
    """
    return Certificate(
        org_name=org.name,
        winner=(viewer.display_name or viewer.email.split("@")[0]),
        series=series.name,
        year=org.season.year if org.season_id else timezone.localtime().year,
        closed_on=timezone.now(),
        last_round_label="Grand Final",
        user_id=viewer.id,
        sample=True,
    )


# ---------------------------------------------------------------------------
# Beyond one organisation (client, 12 Oct 2026): "if my organisation is the
# best... if it's me... irrespective of the type of organisation... we should
# also get a certificate". Three more scopes on top of the per-org champion
# above — an organisation against its own type, an organisation against
# every organisation, and a person against every organisation's tippers.
#
# CONSENT. Comparing one organisation's numbers against another's is exactly
# what the Good List and the national/industry ladders already do, and both
# only ever include an organisation that opted in — ``is_public_listed``. The
# same gate applies here: an organisation that has not opted into being
# compared does not get pulled into a comparison just because a certificate
# asked for one.
#
# "FINAL", AT THIS SCOPE. Organisations do not all run the same fixtures on
# the same clock (``national_ladder`` scopes every org to its OWN season for
# the same reason) — so there is no single grand final to wait for. Instead:
# every consenting organisation that runs this competition must have reached
# ITS OWN final and locked state (season_state() again, unchanged) before a
# wider winner is named, the same way the per-org certificate refuses to
# speculate mid-season. Fewer than two such organisations, or too few
# consenting organisations overall (the credibility floor the ladder itself
# uses), and nothing is named — a "winner" of a field of one is not a claim
# worth printing.
# ---------------------------------------------------------------------------

def _consenting_orgs_for(series, organisation_type=None):
    """Consenting organisations that actually run ``series``, optionally
    narrowed to one organisation type (the "industry" scope)."""
    from orgs.models import Organisation

    qs = Organisation.objects.filter(is_public_listed=True).select_related("organisation_type", "season")
    if organisation_type is not None:
        qs = qs.filter(organisation_type=organisation_type)
    return [o for o in qs if o.season_id and series in competitions_for(o)]


def _scope_is_final(orgs, series) -> bool:
    from .national_ladder import board_is_live

    if len(orgs) < 2 or not board_is_live():
        return False
    return all(season_state(o, series).final for o in orgs)


def _org_standings(orgs, series, year):
    """Each organisation's season for ``series``/``year``: points and active
    tippers, ranked the same way ``national_ladder.ladder()`` ranks them —
    average points per tipper first, so a bigger roster does not just win by
    volume."""
    from django.db.models import Count, Sum, Value
    from django.db.models.functions import Coalesce

    from .models import Tip

    org_ids = [o.id for o in orgs]
    rows = {
        r["org"]: r
        for r in Tip.objects.filter(
            org_id__in=org_ids, group__isnull=True,
            match__round__series=series, match__round__competition__season__year=year,
            is_correct__isnull=False,
        ).values("org").annotate(
            points=Coalesce(Sum("points_awarded"), Value(0)),
            tippers=Count("user", distinct=True),
        )
    }
    out = []
    for o in orgs:
        r = rows.get(o.id)
        points = r["points"] if r else 0
        tippers = r["tippers"] if r else 0
        if points <= 0 or not tippers:
            continue
        out.append({"org": o, "points": points, "tippers": tippers, "avg": points / tippers})
    out.sort(key=lambda r: (-r["avg"], -r["points"], r["org"].name))
    return out


def _org_scope_champion(org, series, *, organisation_type=None, scope, scope_label="") -> Certificate | None:
    """``org``'s own certificate for a wider scope, or ``None`` when that
    scope is not yet final, ``org`` is not in the consenting pool, or ``org``
    is not the one that topped it."""
    if not org.is_public_listed or org.organisation_type_id is None:
        return None
    pool = _consenting_orgs_for(series, organisation_type)
    if org.id not in {o.id for o in pool} or not _scope_is_final(pool, series):
        return None
    year = org.season.year
    standings = _org_standings(pool, series, year)
    if not standings or standings[0]["org"].id != org.id:
        return None
    state = season_state(org, series)
    return Certificate(
        org_name=scope_label or "GoodTip",
        winner=org.name,
        series=series.name,
        year=year,
        closed_on=state.closed_on,
        last_round_label=state.last_round_label,
        scope=scope,
        scope_label=scope_label,
    )


def industry_champion(org, series) -> Certificate | None:
    """``org``'s certificate for topping every consenting organisation of
    its own type, once that whole pool is final — or ``None``."""
    label = org.organisation_type.name if org.organisation_type_id else ""
    return _org_scope_champion(
        org, series, organisation_type=org.organisation_type, scope=SCOPE_INDUSTRY, scope_label=label,
    )


def national_org_champion(org, series) -> Certificate | None:
    """``org``'s certificate for topping every consenting organisation on
    GoodTip, once that whole pool is final — or ``None``."""
    return _org_scope_champion(org, series, scope=SCOPE_NATIONAL, scope_label="GoodTip")


def national_individual_champion(org, series) -> Certificate | None:
    """The certificate for whoever has the most points for ``series`` across
    EVERY consenting organisation, shown on ``org``'s own page only when that
    person has tipped for ``org`` — an org celebrates its own, not a stranger
    who happens to be in first."""
    from django.db.models import Sum, Value
    from django.db.models.functions import Coalesce

    from .models import Tip

    if not org.is_public_listed or org.organisation_type_id is None or not org.season_id:
        return None
    pool = _consenting_orgs_for(series)
    if org.id not in {o.id for o in pool} or not _scope_is_final(pool, series):
        return None
    year = org.season.year
    org_ids = [o.id for o in pool]
    rows = list(
        Tip.objects.filter(
            org_id__in=org_ids, group__isnull=True,
            match__round__series=series, match__round__competition__season__year=year,
            is_correct__isnull=False,
        ).values("user").annotate(points=Coalesce(Sum("points_awarded"), Value(0)))
        .filter(points__gt=0).order_by("-points", "user")
    )
    if not rows:
        return None
    top_user_id = rows[0]["user"]
    if not Tip.objects.filter(user_id=top_user_id, org=org).exists():
        return None
    from accounts.models import User

    champ = User.objects.get(id=top_user_id)
    state = season_state(org, series)
    return Certificate(
        org_name="GoodTip",
        winner=(champ.display_name or champ.email.split("@")[0]),
        series=series.name,
        year=year,
        closed_on=state.closed_on,
        last_round_label=state.last_round_label,
        user_id=champ.id,
        scope=SCOPE_INDIVIDUAL_NATIONAL,
    )


# ---------------------------------------------------------------------------
# Drawing: the client's artwork, with this certificate's photo and words in it
# ---------------------------------------------------------------------------

#: The artwork's own size, in which every position below is measured.
ART_W, ART_H = 1672, 941
#: Output is the artwork ×1.5 (2508×1412): crisp type for a print and a post.
SCALE = 1.5

_assets = {}


def _asset(name):
    from PIL import Image

    if name not in _assets:
        _assets[name] = Image.open(os.path.join(ASSET_DIR, name))
        _assets[name].load()
    return _assets[name]


def _font(key, size):
    from PIL import ImageFont

    return ImageFont.truetype(os.path.join(FONT_DIR, FONTS[key]), max(1, int(size)))


def _tracked_w(font, text, tracking):
    return sum(font.getlength(ch) for ch in text) + tracking * max(0, len(text) - 1)


def _tracked(draw, x, y, text, font, fill, tracking):
    for ch in text:
        draw.text((x, y), ch, font=font, fill=fill)
        x += font.getlength(ch) + tracking


def _fit_tracked(text, key, size, ratio, max_w):
    """A tracked line at the artwork's size, made smaller only if it would
    run past ``max_w`` (a long organisation or a scope headline)."""
    while size > 8:
        f = _font(key, size)
        if _tracked_w(f, text, size * ratio) <= max_w:
            return f, size * ratio
        size -= 1
    return _font(key, size), size * ratio


def _gradient_text(img, xy, text, font, top, bottom):
    """``text`` filled top-to-bottom from ``top`` to ``bottom``, with the soft
    drop shadow the artwork's name has."""
    from PIL import Image, ImageDraw, ImageFilter

    m = Image.new("L", img.size, 0)
    ImageDraw.Draw(m).text(xy, text, font=font, fill=255)
    shadow = m.filter(ImageFilter.GaussianBlur(9 * SCALE / 1.5))
    img.paste((0, 0, 0), (0, int(6 * SCALE)), shadow.point(lambda v: int(v * 0.55)))
    l, t, r, b = font.getbbox(text)
    y0, y1 = int(xy[1] + t), int(xy[1] + b)
    grad = Image.new("RGB", img.size, top)
    g = ImageDraw.Draw(grad)
    for y in range(y0, y1 + 1):
        f = (y - y0) / max(1, y1 - y0)
        g.line([(0, y), (img.size[0], y)], fill=tuple(int(top[k] + (bottom[k] - top[k]) * f) for k in range(3)))
    img.paste(grad, (0, 0), m)


def _load_photo(photo_field):
    """Opens a champion's photo field (``ChampionPhoto.image``,
    ``User.avatar`` or ``Organisation.logo``) whatever the storage backend.
    ``None`` for no photo, or a file Pillow can't read as an image."""
    from PIL import Image, ImageOps

    if not photo_field:
        return None
    try:
        photo_field.open("rb")
        data = photo_field.read()
    except Exception:  # noqa: BLE001 — storage hiccup; fall back to initials
        return None
    finally:
        try:
            photo_field.close()
        except Exception:  # noqa: BLE001
            pass
    try:
        photo = ImageOps.exif_transpose(Image.open(io.BytesIO(data))).convert("RGB")
    except Exception:  # noqa: BLE001 — not an image Pillow can read
        return None
    return _trim_bars(photo)


def _trim_bars(photo):
    """Cut away the solid bars a phone or editor padded a picture with — a
    profile photo letterboxed in black filled the top of the frame with it."""
    from PIL import Image, ImageChops

    edge = Image.new("RGB", photo.size, photo.getpixel((0, 0)))
    box = ImageChops.difference(photo, edge).convert("L").point(lambda v: 255 if v > 24 else 0).getbbox()
    if box and (box[2] - box[0]) * (box[3] - box[1]) >= 0.3 * photo.size[0] * photo.size[1]:
        return photo.crop(box)
    return photo


def _scene(cert: "Certificate", photo=None):
    """The certificate as an image: the artwork, the photo in its frame, and
    this certificate's own words and trophy stacked down the right."""
    from PIL import Image, ImageDraw, ImageOps

    s = SCALE
    W, H = int(ART_W * s), int(ART_H * s)
    img = _asset("champion_template.png").convert("RGB").resize((W, H), Image.LANCZOS)
    mask = _asset("champion_photo_mask.png").resize((W, H), Image.LANCZOS)
    box = mask.getbbox()

    if photo is not None:
        fitted = ImageOps.fit(photo, (box[2] - box[0], box[3] - box[1]), Image.LANCZOS, centering=(0.5, 0.3))
        layer = Image.new("RGB", (W, H))
        layer.paste(fitted, box[:2])
        img.paste(layer, (0, 0), mask)
    else:
        initials = "".join(w[0] for w in cert.winner.split()[:2]).upper() or "?"
        f = _font("initials", 190 * s)
        d = ImageDraw.Draw(img)
        d.text((380 * s, 440 * s), initials, font=f, fill=(198, 247, 70), anchor="mm")
    outline = _asset("champion_frame_outline.png").resize((W, H), Image.LANCZOS)
    img.paste(outline, (0, 0), outline)

    d = ImageDraw.Draw(img)
    # One centred stack down the right (client, 13 Oct 2026): the GOODTIP
    # wordmark on top, the competition pill, SEASON CHAMPION, the trophy, then
    # the name on one line in capitals, the rule, headline and organisation.
    cx = 1195 * s
    lime_rule = (148, 206, 62)

    def centred(text, font, cap_bottom, fill, tracking):
        x = cx - _tracked_w(font, text, tracking) / 2
        _tracked(d, x, cap_bottom * s - font.getbbox("H")[3], text, font, fill, tracking)

    mark_f = _font("name", 50 * s)
    good, tip = "GOOD", "TIP."
    mw = mark_f.getlength(good) + mark_f.getlength(tip)
    mx, my = cx - mw / 2, 82 * s - mark_f.getbbox("H")[3]
    d.text((mx, my), good, font=mark_f, fill=(255, 255, 255))
    d.text((mx + mark_f.getlength(good), my), tip, font=mark_f, fill=(190, 246, 60))

    pill_f = _font("pill", 29 * s)
    label = f"{cert.series} \u00b7 {cert.year}"
    pw, ph = pill_f.getlength(label) + 90 * s, 50 * s
    top = 112 * s
    left, right = cx - pw / 2, cx + pw / 2
    rule_y = top + ph / 2
    d.line([(cx - 300 * s, rule_y), (left - 22 * s, rule_y)], fill=lime_rule, width=max(2, int(2 * s)))
    d.line([(right + 22 * s, rule_y), (cx + 300 * s, rule_y)], fill=lime_rule, width=max(2, int(2 * s)))
    d.rounded_rectangle([left, top, right, top + ph], radius=ph / 2, fill=(194, 250, 72))
    d.text((cx, rule_y), label, font=pill_f, fill=(9, 40, 22), anchor="mm")

    label_f = _font("text_b", 32 * s)
    centred("SEASON CHAMPION", label_f, 222, (255, 255, 255), 0.42 * 32 * s)

    trophy = _asset("champion_trophy.png")
    tw, th = int(trophy.size[0] * 0.92 * s), int(trophy.size[1] * 0.92 * s)
    trophy = trophy.resize((tw, th), Image.LANCZOS)
    img.paste(trophy, (int(cx - tw / 2), int(242 * s)), trophy)

    # The name on one line, in capitals: first name white, the rest lime.
    parts = cert.winner.upper().split(None, 1)
    first, rest = (parts[0], parts[1]) if len(parts) == 2 else (parts[0] if parts else "", "")
    size = 92
    while size > 36:
        name_f = _font("name", size * s)
        space = name_f.getlength(" ") if rest else 0
        total = name_f.getlength(first) + space + (name_f.getlength(rest) if rest else 0)
        if total <= 820 * s:
            break
        size -= 2
    # Centred in the gap between trophy and rule, so a long name set smaller
    # doesn't float away from the trophy.
    cap_t, cap_b = name_f.getbbox("H")[1], name_f.getbbox("H")[3]
    y = 494 * s - (cap_b - cap_t) / 2 - cap_t
    x = cx - total / 2
    _gradient_text(img, (x, y), first, name_f, (255, 255, 255), (206, 214, 210))
    if rest:
        _gradient_text(img, (x + name_f.getlength(first) + space, y), rest, name_f, (236, 255, 178), (168, 228, 48))
    d = ImageDraw.Draw(img)

    dy, half, dm = 562 * s, 310 * s, 8 * s
    d.line([(cx - half, dy), (cx - dm - 10 * s, dy)], fill=lime_rule, width=max(2, int(2 * s)))
    d.line([(cx + dm + 10 * s, dy), (cx + half, dy)], fill=lime_rule, width=max(2, int(2 * s)))
    d.polygon([(cx, dy - dm), (cx + dm, dy), (cx, dy + dm), (cx - dm, dy)], fill=(5, 30, 20), outline=(190, 246, 60), width=max(2, int(2 * s)))

    head = cert.headline.upper()
    hf, htr = _fit_tracked(head, "text_b", 28 * s, 0.29, 800 * s)
    centred(head, hf, 612, (255, 255, 255), htr)
    org = cert.org_name.upper()
    of, otr = _fit_tracked(org, "text", 21 * s, 0.21, 800 * s)
    centred(org, of, 656, (178, 192, 184), otr)

    # The status line along the foot.
    status = cert.footer_line.replace("  ·  ", " • ")
    ff, ftr = _fit_tracked(status, "text", 16 * s, 0.21, 640 * s)
    _tracked(d, 122 * s, 866 * s - ff.getbbox("H")[3], status, ff, (232, 238, 234), ftr)
    return img


# ---------------------------------------------------------------------------
# The two exports (§5): a landscape PNG, and the same picture as a PDF page
# ---------------------------------------------------------------------------

def render_png(cert: Certificate, photo_field=None) -> bytes:
    out = io.BytesIO()
    _scene(cert, _load_photo(photo_field)).save(out, "PNG", optimize=True)
    return out.getvalue()


def render_pdf(cert: Certificate, photo_field=None) -> bytes:
    """The PNG on a page cut to its own shape — A4 wide, no white border."""
    from reportlab.lib.pagesizes import A4, landscape
    from reportlab.lib.utils import ImageReader
    from reportlab.pdfgen import canvas

    pw = landscape(A4)[0]
    ph = pw * ART_H / ART_W
    img_buf = io.BytesIO()
    _scene(cert, _load_photo(photo_field)).save(img_buf, "PNG")
    img_buf.seek(0)
    out = io.BytesIO()
    c = canvas.Canvas(out, pagesize=(pw, ph))
    c.setTitle(f"{cert.headline} — {cert.winner}")
    c.setAuthor("GoodTip")
    c.drawImage(ImageReader(img_buf), 0, 0, width=pw, height=ph)
    c.showPage()
    c.save()
    return out.getvalue()
