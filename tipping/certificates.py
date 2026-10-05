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
  * §5 two exports, same content: a landscape PDF for print, a 1080×1080 PNG
    for social (plus the optional 1080×1920 story PNG). Name and headline sized
    to read as a feed thumbnail. -> render_pdf(), render_png()

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
DEEP = "#072F1E"
FOREST = "#2A7337"
LIME = "#CFF93A"
CREAM = "#F6F1E2"
# The trophy and frame are gold (client's mock-up, 3 Oct 2026): light at the
# top of the cup to deep at the foot, so it reads as metal, not a flat icon.
GOLD_LIGHT = "#F8E39A"
GOLD = "#E2B04A"
GOLD_DEEP = "#9C6B17"

FONT_DIR = os.path.join(os.path.dirname(__file__), "certificate_fonts")
FONTS = {
    "display": "ArchivoBlack-Regular.ttf",
    "label": "BarlowCondensed-ExtraBold.ttf",
    "label_b": "BarlowCondensed-Bold.ttf",
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


@dataclass
class Certificate:
    org_name: str
    winner: str
    series: str
    year: int
    closed_on: datetime
    last_round_label: str
    user_id: int

    @property
    def headline(self) -> str:
        """§3: competition + season, never a round number."""
        return f"{self.year} {self.series} Season Champion"

    @property
    def accent(self) -> str:
        return ACCENTS.get(self.series, FOREST)

    @property
    def closed_label(self) -> str:
        return timezone.localtime(self.closed_on).strftime("%-d %B %Y")

    @property
    def filename_png_square(self) -> str:
        return self.filename("png", "square")

    def filename(self, ext: str, shape: str = "") -> str:
        import re

        slug = re.sub(r"[^a-z0-9]+", "-", self.winner.lower()).strip("-") or "champion"
        tail = f"-{shape}" if shape else ""
        return f"goodtip-{self.year}-{self.series.lower()}-champion-{slug}{tail}.{ext}"


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


# ---------------------------------------------------------------------------
# The design (4 Oct 2026, client: "enhance it … something extremely clean",
# after the champion section's mock-up): deep green, a gold trophy inside a
# gold laurel, a fine double gold frame, the name large in white and the title
# in gold. The print PDF keeps a cream page — a full dark page is a lot of ink
# for an office printer — and shares every gold element with the images.
# ---------------------------------------------------------------------------

import math

GOLD_INK = "#9A6E14"        # gold dark enough to print as text on cream


def _hex(c):
    return tuple(int(c[i:i + 2], 16) for i in (1, 3, 5))


def _ellipse_pts(x, y, length, width, angle, n=18):
    """A leaf: an ellipse ``length`` long, ``width`` wide, centred on (x, y),
    turned to ``angle`` (radians)."""
    ca, sa = math.cos(angle), math.sin(angle)
    pts = []
    for k in range(n):
        t = 2 * math.pi * k / n
        ex, ey = (length / 2) * math.cos(t), (width / 2) * math.sin(t)
        pts.append((x + ex * ca - ey * sa, y + ex * sa + ey * ca))
    return pts


def _laurel_leaves(cx, cy, r, s):
    """Leaves for both branches of a wreath round (cx, cy), screen y-down.

    Each branch is a stem climbing from the bottom up one side, with leaves
    in PAIRS along it — one each side of the stem, both pointing up the
    branch, shrinking towards the tip — which is what makes it read as a
    laurel rather than a ring of dots."""
    leaves = []
    n = 11
    for side in (-1, 1):
        for i in range(n):
            t = i / (n - 1)
            a = math.radians(100 + 132 * t) if side < 0 else math.radians(80 - 132 * t)
            px, py = cx + r * math.cos(a), cy + r * math.sin(a)
            up = a + (math.pi / 2 if side < 0 else -math.pi / 2)      # along the stem, towards the tip
            size = (0.62 - 0.26 * t) * s
            for flank in (-1, 1):
                off = flank * 0.13 * s
                lx, ly = px + off * math.cos(a), py + off * math.sin(a)
                lx += 0.18 * size * math.cos(up); ly += 0.18 * size * math.sin(up)
                leaves.append((lx, ly, size, size * 0.38, up + flank * side * 0.62))
    return leaves


def _laurel_stems(cx, cy, r):
    """The two stems as point lists, for drawing as lines."""
    stems = []
    for side in (-1, 1):
        pts = []
        for k in range(30):
            t = k / 29
            a = math.radians(98 + 134 * t) if side < 0 else math.radians(82 - 134 * t)
            pts.append((cx + r * math.cos(a), cy + r * math.sin(a)))
        stems.append(pts)
    return stems


def _trophy_parts(cx, top, s):
    """The trophy as shapes, screen y-down, ``top`` at the rim. Returns
    (cup polygon, rim ellipse box, handle boxes, stem box, knot box, base boxes)."""
    cup = []
    depth = 1.25 * s
    for k in range(25):
        t = k / 24
        hw = 0.92 * s * (1 - 0.8 * t ** 1.7)
        cup.append((cx - hw, top + depth * t))
    for k in range(24, -1, -1):
        t = k / 24
        hw = 0.92 * s * (1 - 0.8 * t ** 1.7)
        cup.append((cx + hw, top + depth * t))
    rim = (cx - 0.98 * s, top - 0.12 * s, cx + 0.98 * s, top + 0.14 * s)
    handles = [(cx - 1.42 * s, top + 0.05 * s, cx - 0.62 * s, top + 0.85 * s),
               (cx + 0.62 * s, top + 0.05 * s, cx + 1.42 * s, top + 0.85 * s)]
    stem = (cx - 0.1 * s, top + depth - 0.05 * s, cx + 0.1 * s, top + depth + 0.42 * s)
    knot = (cx - 0.22 * s, top + depth + 0.1 * s, cx + 0.22 * s, top + depth + 0.26 * s)
    base = [(cx - 0.46 * s, top + depth + 0.4 * s, cx + 0.46 * s, top + depth + 0.6 * s),
            (cx - 0.66 * s, top + depth + 0.6 * s, cx + 0.66 * s, top + depth + 0.84 * s)]
    return cup, rim, handles, stem, knot, base


def _star_pts(cx, cy, r):
    pts = []
    for k in range(10):
        a = -math.pi / 2 + k * math.pi / 5
        rr = r if k % 2 == 0 else r * 0.45
        pts.append((cx + rr * math.cos(a), cy + rr * math.sin(a)))
    return pts


# ---------------------------------------------------------------------------
# Print: a landscape PDF (§5)
# ---------------------------------------------------------------------------

_pdf_fonts_ready = False


def _pdf_fonts():
    global _pdf_fonts_ready
    if _pdf_fonts_ready:
        return
    from reportlab.pdfbase import pdfmetrics
    from reportlab.pdfbase.ttfonts import TTFont

    for key, file in FONTS.items():
        pdfmetrics.registerFont(TTFont(f"GT-{key}", os.path.join(FONT_DIR, file)))
    _pdf_fonts_ready = True


def _fit_pdf(c, text, font, max_w, start, floor):
    size = start
    while size > floor and c.stringWidth(text, font, size) > max_w:
        size -= 1
    return size


def _pdf_gold_fill(c, draw_path, x0, y0, x1, y1):
    """Fill whatever ``draw_path`` builds with a vertical gold gradient."""
    from reportlab.lib.colors import HexColor

    c.saveState()
    p = c.beginPath()
    draw_path(p)
    c.clipPath(p, stroke=0, fill=0)
    c.linearGradient(x0, y1, x0, y0, (HexColor(GOLD_LIGHT), HexColor(GOLD), HexColor(GOLD_DEEP)),
                     (0, 0.45, 1), extend=True)
    c.restoreState()


def _pdf_emblem(c, cx, cy, s):
    """Trophy, star and laurel, centred on (cx, cy) in PDF space (y up)."""
    from reportlab.lib.colors import HexColor
    H = 2 * cy                                   # flip helper: screen y -> pdf y around cy
    def fy(y): return H - y
    cup, rim, handles, stem, knot, base = _trophy_parts(cx, cy - 0.9 * s, s)
    leaves = _laurel_leaves(cx, cy, 1.75 * s, s)

    # Each leaf and each part of the trophy is filled on its own, with the
    # gradient spanning the whole emblem so the gold still runs top to bottom.
    # One combined path would cancel itself wherever two shapes overlap (the
    # stem showed a chequerboard).
    def poly(pts):
        def build(p):
            p.moveTo(pts[0][0], fy(pts[0][1]))
            for (px, py) in pts[1:]:
                p.lineTo(px, fy(py))
            p.close()
        return build

    def box(b):
        x0, y0, x1, y1 = b
        return poly([(x0, y0), (x1, y0), (x1, y1), (x0, y1)])

    span = (cx - 2 * s, fy(cy + 2 * s), cx + 2 * s, fy(cy - 2 * s))
    for (x, y, L, Wd, a) in leaves:
        _pdf_gold_fill(c, poly(_ellipse_pts(x, y, L, Wd, a, 14)), *span)
    c.setStrokeColor(HexColor(GOLD)); c.setLineWidth(max(0.8, 0.05 * s))
    for stem_pts in _laurel_stems(cx, cy, 1.75 * s):
        p = c.beginPath(); p.moveTo(stem_pts[0][0], fy(stem_pts[0][1]))
        for (px, py) in stem_pts[1:]:
            p.lineTo(px, fy(py))
        c.drawPath(p, stroke=1, fill=0)

    for part in [poly(cup), box(stem), box(knot)] + [box(b) for b in base]:
        _pdf_gold_fill(c, part, *span)

    from reportlab.lib.colors import HexColor
    c.setStrokeColor(HexColor(GOLD)); c.setLineWidth(0.16 * s)
    for (x0, y0, x1, y1) in handles:
        start = 90 if x0 < cx else -90
        c.arc(x0, fy(y1), x1, fy(y0), start, 180)
    c.setFillColor(HexColor(GOLD_LIGHT))
    x0, y0, x1, y1 = rim
    c.ellipse(x0, fy(y1), x1, fy(y0), stroke=0, fill=1)
    star = _star_pts(cx, cy - 1.42 * s, 0.26 * s)
    p = c.beginPath(); p.moveTo(star[0][0], fy(star[0][1]))
    for (px, py) in star[1:]:
        p.lineTo(px, fy(py))
    p.close(); c.drawPath(p, stroke=0, fill=1)


def _pdf_frame(c, W, H):
    from reportlab.lib.colors import HexColor

    c.setStrokeColor(HexColor(GOLD)); c.setLineWidth(2.2); c.rect(24, 24, W - 48, H - 48, stroke=1, fill=0)
    c.setLineWidth(0.8); c.rect(32, 32, W - 64, H - 64, stroke=1, fill=0)
    c.setFillColor(HexColor(GOLD))
    for (x, y) in ((32, 32), (W - 32, 32), (32, H - 32), (W - 32, H - 32)):
        p = c.beginPath(); p.moveTo(x, y + 7); p.lineTo(x + 7, y); p.lineTo(x, y - 7); p.lineTo(x - 7, y); p.close()
        c.drawPath(p, stroke=0, fill=1)


def _pdf_divider(c, cx, y, half):
    from reportlab.lib.colors import HexColor

    c.setStrokeColor(HexColor(GOLD)); c.setLineWidth(1)
    c.line(cx - half, y, cx - 9, y); c.line(cx + 9, y, cx + half, y)
    c.setFillColor(HexColor(GOLD))
    p = c.beginPath(); p.moveTo(cx, y + 5); p.lineTo(cx + 5, y); p.lineTo(cx, y - 5); p.lineTo(cx - 5, y); p.close()
    c.drawPath(p, stroke=0, fill=1)


def _spaced(c, text, font, size, cx, y, tracking):
    width = sum(c.stringWidth(ch, font, size) for ch in text) + tracking * (len(text) - 1)
    x = cx - width / 2
    c.setFont(font, size)
    for ch in text:
        c.drawString(x, y, ch)
        x += c.stringWidth(ch, font, size) + tracking


def render_pdf(cert: Certificate) -> bytes:
    from reportlab.lib.colors import HexColor
    from reportlab.lib.pagesizes import A4, landscape
    from reportlab.pdfgen import canvas

    _pdf_fonts()
    W, H = landscape(A4)
    buf = io.BytesIO()
    c = canvas.Canvas(buf, pagesize=(W, H))
    c.setTitle(f"{cert.headline} — {cert.winner}")
    c.setAuthor("GoodTip")

    c.setFillColor(HexColor(CREAM)); c.rect(0, 0, W, H, stroke=0, fill=1)
    _pdf_frame(c, W, H)
    cx = W / 2

    chip = f"{cert.series}  ·  {cert.year}"
    cw = c.stringWidth(chip, "GT-label", 13) + 30
    c.setFillColor(HexColor(cert.accent)); c.roundRect(cx - cw / 2, H - 82, cw, 25, 12.5, stroke=0, fill=1)
    c.setFillColor(HexColor("#FFFFFF")); c.setFont("GT-label", 13); c.drawCentredString(cx, H - 74, chip)

    _pdf_emblem(c, cx, H - 158, 30)

    c.setFillColor(HexColor(DEEP)); _spaced(c, "SEASON CHAMPION", "GT-label", 30, cx, H - 250, 4)
    c.setFillColor(HexColor("#4A5649")); c.setFont("GT-text", 13)
    c.drawCentredString(cx, H - 274, "This certifies that")

    size = _fit_pdf(c, cert.winner, "GT-display", W - 200, 52, 24)
    c.setFillColor(HexColor(DEEP)); c.setFont("GT-display", size)
    name_y = H - 284 - size
    c.drawCentredString(cx, name_y, cert.winner)

    _pdf_divider(c, cx, name_y - 22, 120)
    c.setFillColor(HexColor(GOLD_INK)); c.setFont("GT-text_b", 21)
    c.drawCentredString(cx, name_y - 52, cert.headline)
    osz = _fit_pdf(c, cert.org_name, "GT-text", W - 260, 16, 10)
    c.setFillColor(HexColor("#4A5649")); c.setFont("GT-text", osz)
    c.drawCentredString(cx, name_y - 76, cert.org_name)

    c.setFillColor(HexColor("#5C6A5A")); c.setFont("GT-label_b", 11)
    _spaced(c, f"SEASON CLOSED {cert.closed_label.upper()}  ·  {cert.last_round_label.upper()}", "GT-label_b", 11, cx, 70, 1.2)
    c.setFont("GT-display", 17)
    gw, tw = c.stringWidth("GOOD", "GT-display", 17), c.stringWidth("TIP.", "GT-display", 17)
    c.setFillColor(HexColor(DEEP)); c.drawString(cx - (gw + tw) / 2, 48, "GOOD")
    c.setFillColor(HexColor(FOREST)); c.drawString(cx - (gw + tw) / 2 + gw, 48, "TIP.")

    c.showPage(); c.save()
    return buf.getvalue()


# ---------------------------------------------------------------------------
# Social: 1080×1080 square (and the optional 1080×1920 story) PNG (§5)
# ---------------------------------------------------------------------------

def _font(key, size):
    from PIL import ImageFont

    return ImageFont.truetype(os.path.join(FONT_DIR, FONTS[key]), size)


def _w(draw, text, font):
    l, t, r, b = draw.textbbox((0, 0), text, font=font)
    return r - l


def _fit_lines(draw, text, key, max_w, start, floor):
    """The winner's name as big as fits — wrapped onto two lines before it is
    shrunk below a thumbnail-legible size (§5: name reads clearly at feed size)."""
    size = start
    while size >= floor:
        f = _font(key, size)
        if _w(draw, text, f) <= max_w:
            return f, [text]
        size -= 4
    words = text.split()
    if len(words) > 1:
        # the split that leaves the longer of the two lines as short as possible
        best = min(
            ((" ".join(words[:i]), " ".join(words[i:])) for i in range(1, len(words))),
            key=lambda pair: max(len(pair[0]), len(pair[1])),
        )
        size = start
        while size > 30:
            f = _font(key, size)
            if all(_w(draw, ln, f) <= max_w for ln in best):
                return f, list(best)
            size -= 4
    return _font(key, floor), [text]


def _centre(draw, y, text, font, fill, W):
    draw.text(((W - _w(draw, text, font)) / 2, y), text, font=font, fill=fill)



def _gold_gradient(W, H, top, bottom):
    from PIL import Image, ImageDraw

    grad = Image.new("RGB", (W, H), GOLD)
    g = ImageDraw.Draw(grad)
    stops = [(0.0, _hex(GOLD_LIGHT)), (0.45, _hex(GOLD)), (1.0, _hex(GOLD_DEEP))]
    for y in range(max(0, top), min(H, bottom + 1)):
        t = (y - top) / max(1, bottom - top)
        for (p0, c0), (p1, c1) in zip(stops, stops[1:]):
            if p0 <= t <= p1:
                f = (t - p0) / (p1 - p0)
                g.line([(0, y), (W, y)], fill=tuple(int(c0[k] + (c1[k] - c0[k]) * f) for k in range(3)))
                break
    return grad


def _png_emblem(img, cx, cy, s):
    """Trophy, star and laurel in gold, centred on (cx, cy)."""
    from PIL import Image, ImageDraw

    W, H = img.size
    cup, rim, handles, stem, knot, base = _trophy_parts(cx, cy - 0.9 * s, s)
    mask = Image.new("L", (W, H), 0)
    m = ImageDraw.Draw(mask)
    for stem_pts in _laurel_stems(cx, cy, 1.75 * s):
        m.line(stem_pts, fill=255, width=max(3, int(0.05 * s)))
    for (x, y, L, Wd, a) in _laurel_leaves(cx, cy, 1.75 * s, s):
        m.polygon(_ellipse_pts(x, y, L, Wd, a), fill=255)
    m.polygon(cup, fill=255)
    lw = max(4, int(0.16 * s))
    m.arc(handles[0], 90, 270, fill=255, width=lw)
    m.arc(handles[1], 270, 90, fill=255, width=lw)
    m.rectangle(stem, fill=255)
    for box in base:
        m.rounded_rectangle(box, radius=max(3, int(0.06 * s)), fill=255)
    m.ellipse(knot, fill=255)
    img.paste(_gold_gradient(W, H, int(cy - 2 * s), int(cy + 2 * s)), (0, 0), mask)
    d = ImageDraw.Draw(img)
    d.ellipse(rim, fill=GOLD_LIGHT)
    d.polygon(_star_pts(cx, cy - 1.42 * s, 0.26 * s), fill=GOLD_LIGHT)
    # a highlight down the left of the cup
    d.polygon([(cx - 0.6 * s, cy - 0.78 * s), (cx - 0.42 * s, cy - 0.78 * s),
               (cx - 0.28 * s, cy + 0.12 * s), (cx - 0.44 * s, cy - 0.02 * s)], fill="#FFF4CC")


def _png_spaced(d, y, text, font, fill, W, tracking):
    widths = [_w(d, ch, font) for ch in text]
    x = (W - (sum(widths) + tracking * (len(text) - 1))) / 2
    for ch, wd in zip(text, widths):
        d.text((x, y), ch, font=font, fill=fill)
        x += wd + tracking


def render_png(cert: Certificate, shape: str = "square") -> bytes:
    """``square`` 1080×1080 (the primary social export) or ``story`` 1080×1920."""
    from PIL import Image, ImageDraw, ImageFilter

    W, H = (1080, 1920) if shape == "story" else (1080, 1080)
    s = 1.35 if shape == "story" else 1.0

    # deep green with a soft lift behind the emblem and a hint of the
    # competition's colour inside it
    img = Image.new("RGB", (W, H), "#05261A")
    glow = Image.new("RGB", (W, H), "#05261A")
    gd = ImageDraw.Draw(glow)
    gy = int(H * (0.27 if shape == "story" else 0.27))
    gd.ellipse([W / 2 - 520, gy - 420, W / 2 + 520, gy + 420], fill="#12583C")
    gd.ellipse([W / 2 - 230, gy - 200, W / 2 + 230, gy + 200], fill=cert.accent)
    glow = glow.filter(ImageFilter.GaussianBlur(160))
    img = Image.blend(img, glow, 0.6)
    d = ImageDraw.Draw(img)

    # the fine double gold frame, with diamonds at the corners
    d.rectangle([30, 30, W - 30, H - 30], outline=GOLD, width=3)
    d.rectangle([44, 44, W - 44, H - 44], outline=GOLD_DEEP, width=1)
    for (x, y) in ((44, 44), (W - 44, 44), (44, H - 44), (W - 44, H - 44)):
        d.polygon([(x, y - 11), (x + 11, y), (x, y + 11), (x - 11, y)], fill=GOLD)

    chip_f = _font("label", int(36 * s))
    chip = f"{cert.series} · {cert.year}"
    ch = int(58 * s)
    head_f = _font("label", int(64 * s))
    name_f, lines = _fit_lines(d, cert.winner, "display", W - 170, int(106 * s), int(68 * s))
    lh = int(name_f.size * 1.1)
    line_f = _font("text_b", int(44 * s))
    org_f = _font("text", int(36 * s))
    org = cert.org_name
    while _w(d, org, org_f) > W - 180 and org_f.size > 24:
        org_f = _font("text", org_f.size - 2)
    when_f = _font("label_b", int(27 * s))
    when = f"SEASON CLOSED {cert.closed_label.upper()} · {cert.last_round_label.upper()}"
    while _w(d, when, when_f) > W - 180 and when_f.size > 18:
        when_f = _font("label_b", when_f.size - 2)

    emblem = int(300 * s)
    gap = int((60 if shape == "story" else 18) * s)
    blocks = [ch, emblem, int(74 * s), lh * len(lines), int(18 * s), int(52 * s), int(44 * s), int(34 * s)]
    total = sum(blocks) + gap * (len(blocks) - 1)
    top, bottom = 66, H - int(128 * s)
    y = top + max(0, (bottom - top - total) // 2)

    cw = _w(d, chip, chip_f) + int(52 * s)
    d.rounded_rectangle([(W - cw) / 2, y, (W + cw) / 2, y + ch], radius=ch // 2, fill=cert.accent)
    _centre(d, y + int(6 * s), chip, chip_f, "#FFFFFF", W)
    y += ch + gap

    _png_emblem(img, W / 2, y + emblem / 2, int(80 * s))
    d = ImageDraw.Draw(img)
    y += emblem + gap

    _png_spaced(d, y, "SEASON CHAMPION", head_f, LIME, W, int(6 * s))
    y += int(74 * s) + gap
    for ln in lines:
        _centre(d, y, ln, name_f, "#FFFFFF", W)
        y += lh
    y += gap
    # a gold rule with a diamond, between the name and the title
    rw = int(150 * s)
    d.line([(W / 2 - rw, y + 8), (W / 2 - 14, y + 8)], fill=GOLD, width=2)
    d.line([(W / 2 + 14, y + 8), (W / 2 + rw, y + 8)], fill=GOLD, width=2)
    d.polygon([(W / 2, y), (W / 2 + 8, y + 8), (W / 2, y + 16), (W / 2 - 8, y + 8)], fill=GOLD)
    y += int(18 * s) + gap
    _centre(d, y, cert.headline, line_f, GOLD_LIGHT, W)
    y += int(52 * s) + gap
    _centre(d, y, org, org_f, "#E3EDE5", W)
    y += int(44 * s) + gap
    _png_spaced(d, y, when, when_f, "#9DB8A6", W, 2)

    mark_f = _font("display", int(42 * s))
    gw, tw = _w(d, "GOOD", mark_f), _w(d, "TIP.", mark_f)
    mx = (W - gw - tw) / 2
    my = H - int(100 * s)
    d.text((mx, my), "GOOD", font=mark_f, fill="#FFFFFF")
    d.text((mx + gw, my), "TIP.", font=mark_f, fill=LIME)

    out = io.BytesIO()
    img.save(out, "PNG", optimize=True)
    return out.getvalue()
