"""Template helpers for the app UI.

`crest` / `team_cell` render a generated club badge — a monogram in the club's
own colours (from tipping/team_colors.py). These are original marks, not the
clubs' real (trademarked) logos; licensed logo files can be dropped in later
behind the same tags.
"""
from django import template
from django.contrib.staticfiles import finders
from django.templatetags.static import static
from django.utils import timezone
from django.utils.html import format_html

from ..team_colors import get_team_colors, team_initials

register = template.Library()

# slug -> static url (or None) so we only hit the staticfiles finders once
# per team per process.
_STATIC_LOGOS: dict = {}


def _logo_url(team):
    """URL of the club's real logo, or None to fall back to the monogram.

    Priority: uploaded Team.logo file, then a bundled static file at
    img/teams/<slug>.png (or .svg).
    """
    logo = getattr(team, "logo", None)
    if logo:
        try:
            return logo.url
        except ValueError:
            pass
    slug = _slug(team)
    if not slug:
        return None
    if slug not in _STATIC_LOGOS:
        url = None
        for ext in ("png", "svg", "webp"):
            rel = f"img/teams/{slug}.{ext}"
            if finders.find(rel):
                try:
                    url = static(rel)
                except ValueError:
                    # file exists on disk but isn't in the staticfiles
                    # manifest yet (collectstatic pending) — use the monogram
                    url = None
                break
        _STATIC_LOGOS[slug] = url
    return _STATIC_LOGOS[slug]


# Nice, recognisable codes per club slug; anything unlisted falls back to
# initials derived from the name.
CREST_CODES = {
    "adelaide-crows": "ADE", "brisbane-lions": "BL", "carlton": "CAR",
    "collingwood": "COL", "essendon": "ESS", "fremantle": "FRE",
    "geelong-cats": "GEE", "gold-coast-suns": "GC", "gws-giants": "GWS",
    "hawthorn": "HAW", "melbourne": "MEL", "north-melbourne": "NM",
    "port-adelaide": "PA", "richmond": "RIC", "st-kilda": "STK",
    "sydney-swans": "SYD", "west-coast-eagles": "WCE", "western-bulldogs": "WB",
    "brisbane-broncos": "BRI", "canberra-raiders": "CAN", "bulldogs": "BD",
    "sharks": "CRO", "dolphins": "DOL", "titans": "GCT", "sea-eagles": "MAN",
    "storm": "STM", "knights": "NEW", "warriors": "WAR", "cowboys": "NQL",
    "eels": "PAR", "panthers": "PEN", "rabbitohs": "SOU", "dragons": "SGI",
    "roosters": "ROO", "wests-tigers": "WT",
}


@register.simple_tag
def team_colors(slug):
    return get_team_colors(slug)


@register.filter
def initials(value, max_chars=3):
    try:
        max_chars = int(max_chars)
    except (TypeError, ValueError):
        max_chars = 3
    return team_initials(str(value), max_chars=max_chars)


def _slug(team):
    return (getattr(team, "slug", "") or "").lower()


def _name(team):
    return getattr(team, "name", None) or (str(team) if team is not None else "")


def _code(team):
    slug = _slug(team)
    if slug in CREST_CODES:
        return CREST_CODES[slug]
    name = _name(team)
    words = [w for w in name.replace("-", " ").split() if w]
    if len(words) == 1:
        return words[0][:3].upper()
    return "".join(w[:1] for w in words[:3]).upper() or "TBD"


@register.simple_tag
def crest(team):
    """A standalone club badge — real logo when we have one, monogram otherwise."""
    if team is None:
        return ""
    url = _logo_url(team)
    if url:
        return format_html(
            '<span class="crest has-logo"><img src="{}" alt="{}" loading="lazy"></span>',
            url, _name(team),
        )
    c = get_team_colors(_slug(team))
    return format_html(
        '<span class="crest" style="--crest-bg:{};--crest-fg:{}">{}</span>',
        c["primary"], c["text_on_primary"], _code(team),
    )


@register.simple_tag
def team_cell(team):
    """A club badge next to the club name — for table cells."""
    if team is None:
        return ""
    url = _logo_url(team)
    if url:
        return format_html(
            '<span class="team-cell"><span class="crest has-logo"><img src="{}" alt="" loading="lazy"></span>{}</span>',
            url, _name(team),
        )
    c = get_team_colors(_slug(team))
    return format_html(
        '<span class="team-cell"><span class="crest" style="--crest-bg:{};--crest-fg:{}">{}</span>{}</span>',
        c["primary"], c["text_on_primary"], _code(team), _name(team),
    )


@register.simple_tag
def is_checked(field, value):
    """Return 'checked'/'selected' if a bound form field currently holds `value`.

    Handles both single values and multi-value fields (checkbox/multi-select),
    so option cards keep their state when a form re-renders with errors.
    """
    data = field.value()
    if data is None:
        return ""
    try:
        vals = [str(v) for v in data] if isinstance(data, (list, tuple)) else [str(data)]
    except TypeError:
        vals = [str(data)]
    return "checked" if str(value) in vals else ""


@register.simple_tag
def countdown_cells(dt):
    """Render days/hrs/min cells until `dt` (or a Locked chip once passed)."""
    if not dt:
        return format_html('<div class="countdown"><div class="cd-cell"><b>&mdash;</b><small>tbd</small></div></div>')
    secs = int((dt - timezone.now()).total_seconds())
    if secs <= 0:
        return format_html(
            '<div class="cd-locked"><svg style="width:15px;height:15px;stroke:currentColor;fill:none;vertical-align:-2px"><use href="#ic-lock"/></svg> Locked</div>'
        )
    days, hrs, mins = secs // 86400, (secs % 86400) // 3600, (secs % 3600) // 60
    # Pre-format the padded values: format_html escapes each arg into a
    # SafeString first, and "{:02d}".format(SafeString) raises ValueError.
    return format_html(
        '<div class="countdown"><div class="cd-cell"><b>{}</b><small>days</small></div>'
        '<div class="cd-cell"><b>{}</b><small>hrs</small></div>'
        '<div class="cd-cell"><b>{}</b><small>min</small></div></div>',
        days, f"{hrs:02d}", f"{mins:02d}",
    )
