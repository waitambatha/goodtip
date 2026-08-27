"""Finding a charity's logo, so a picker card is a card rather than a name.

WHY THIS EXISTS
---------------
Charities were stored as a name, a slug and an optional website. Everywhere
one was offered — the wizard's picker, the election ballot, the result — that
gave a line of text and nothing else, which is not enough to recognise a cause
by and not enough to look like a considered choice. Uploading a logo per
charity by hand was the alternative; it does not survive contact with a list
that grows every time someone suggests a new one.

WHAT IT DOES
------------
Reads the charity's OWN site and takes the icon it already publishes for this
purpose — an apple-touch-icon, an og:image, a <link rel="icon">, or /favicon.ico
as a last resort. No third-party logo API, no scraping of anyone else's index:
the only host contacted is the one the charity itself named.

WHAT IT DELIBERATELY DOES NOT DO
--------------------------------
Run inside a request. A charity's site can be slow, redirect four times, or be
down altogether, and none of that may be allowed to hold up a signup form.
Callers either run this from the `fetch_charity_logos` management command or
hand it to a thread and carry on. A charity with no logo renders its initials
tile, which is a finished design rather than a placeholder, so nothing is
waiting on the fetch to look right.
"""
import logging
import re
from io import BytesIO
from urllib.parse import urljoin, urlparse

import requests
from django.core.files.base import ContentFile
from django.utils import timezone

logger = logging.getLogger(__name__)

# Short. This runs over a list, and one unreachable charity must not stall the
# rest for a minute apiece.
TIMEOUT = 8
MAX_BYTES = 2 * 1024 * 1024
# Anything smaller is a UI sprite or a tracking pixel, not a logo.
MIN_PIXELS = 32
USER_AGENT = (
    "Mozilla/5.0 (compatible; GoodTipBot/1.0; +https://goodtip.com.au) "
    "charity-logo-fetch"
)

# og:image in particular is a social-share slot, and plenty of sites fill it
# with a generic banner or a literal placeholder — headspace served
# "placeholder-image.jpg", which passed every size check and would have been
# published as their logo. A name test is crude but catches exactly the class
# of file that is named for not being the real thing.
JUNK_NAME = re.compile(
    r"(placeholder|default[-_.]|no[-_]?image|share|social|banner|cover|"
    r"opengraph|og[-_]image|spacer|blank)", re.I,
)
# A logo is roughly square. A social banner is about 1.91:1, and cropping one
# to a tile gives a slice of someone's stock photograph.
MAX_ASPECT = 2.0

# Ordered best-first. An apple-touch-icon is a deliberately-made square logo at
# a usable size; a favicon is often 16px of mush. og:image is in between —
# usually a real asset, sometimes a whole banner.
ICON_PATTERNS = [
    (r'<link[^>]+rel=["\'][^"\']*apple-touch-icon[^"\']*["\'][^>]*>', "href"),
    (r'<meta[^>]+property=["\']og:image["\'][^>]*>', "content"),
    (r'<link[^>]+rel=["\'][^"\']*\bicon\b[^"\']*["\'][^>]*>', "href"),
]


class LogoNotFound(Exception):
    """No usable image at that site. An ordinary outcome, not an error."""


def _session():
    s = requests.Session()
    s.headers["User-Agent"] = USER_AGENT
    return s


def _attr(tag: str, name: str) -> str:
    m = re.search(rf'{name}=["\']([^"\']+)["\']', tag, re.I)
    return m.group(1).strip() if m else ""


def candidate_icon_urls(html: str, base_url: str) -> list[str]:
    """Every icon the page advertises, best-first, absolute, de-duplicated."""
    found = []
    for pattern, attr in ICON_PATTERNS:
        for tag in re.findall(pattern, html, re.I):
            value = _attr(tag, attr)
            if value and not value.startswith("data:"):
                found.append(urljoin(base_url, value))
    found.append(urljoin(base_url, "/favicon.ico"))
    seen, ordered = set(), []
    for url in found:
        if url in seen or JUNK_NAME.search(urlparse(url).path):
            continue
        seen.add(url)
        ordered.append(url)
    return ordered


def _usable_image(raw: bytes):
    """Decode and vet the bytes. Returns (PIL image, format) or None.

    Vetted rather than trusted because `/favicon.ico` frequently returns an
    HTML 404 page with a 200 status, and a 16px icon is worse than the initials
    tile it would replace.
    """
    from PIL import Image

    try:
        img = Image.open(BytesIO(raw))
        img.load()
    except Exception:                       # noqa: BLE001 — any decode failure
        return None
    w, h = img.size
    if min(w, h) < MIN_PIXELS:
        return None
    if max(w, h) / min(w, h) > MAX_ASPECT:
        return None
    return img, (img.format or "PNG")


def fetch_logo(website: str) -> tuple[bytes, str]:
    """The best logo `website` publishes, as (png_bytes, source_url).

    Raises LogoNotFound when the site is unreachable or advertises nothing
    usable — which is a normal answer for plenty of charities.
    """
    if not website:
        raise LogoNotFound("no website")
    if not urlparse(website).scheme:
        website = f"https://{website}"

    session = _session()
    try:
        page = session.get(website, timeout=TIMEOUT, allow_redirects=True)
        page.raise_for_status()
    except requests.RequestException as e:
        raise LogoNotFound(f"site unreachable: {e}") from e

    for url in candidate_icon_urls(page.text, page.url):
        try:
            r = session.get(url, timeout=TIMEOUT, allow_redirects=True, stream=True)
            r.raise_for_status()
            raw = r.raw.read(MAX_BYTES + 1, decode_content=True)
        except requests.RequestException:
            continue
        if not raw or len(raw) > MAX_BYTES:
            continue
        vetted = _usable_image(raw)
        if vetted is None:
            continue
        img, _fmt = vetted
        # Normalised to PNG on the way in. An .ico holding four sizes and a
        # WEBP nobody's browser wants both become one predictable thing, so
        # the template never has to care what the charity happened to serve.
        out = BytesIO()
        img.convert("RGBA").save(out, format="PNG")
        return out.getvalue(), url

    raise LogoNotFound("no usable icon on the page")


def derive_website(name: str) -> str:
    """A best-guess website for a charity that has none.

    Only ever a GUESS, so it is confirmed by actually fetching it before being
    written anywhere — see `backfill_charity`. Australian charities are
    overwhelmingly .org.au, so that is tried first.
    """
    slug = re.sub(r"[^a-z0-9]+", "", name.lower())
    if not slug:
        return ""
    return f"https://{slug}.org.au"


def resolve_website(name: str) -> str:
    """Confirm a guessed website actually belongs to this charity, or "".

    A guessed URL is worth nothing on its own — writing an unverified address
    onto a charity record puts it in front of members as though somebody had
    checked it. So the guess is fetched, and accepted only when the response
    says we reached the right place.

    A 403 counts. Several large Australian charities (redcross.org.au,
    gamblershelp.com.au) sit behind bot protection that refuses any
    non-browser request, and refusing us is not the same as not existing. But
    a 403 alone could equally be a parking page or an unrelated squatter, so
    it is only accepted when the host we finally landed on still matches the
    name we derived it from.
    """
    guess = derive_website(name)
    if not guess:
        return ""
    try:
        r = _session().get(guess, timeout=TIMEOUT, allow_redirects=True)
    except requests.RequestException:
        return ""
    final = r.url.rstrip("/")
    if r.ok:
        return final
    if r.status_code == 403:
        slug = re.sub(r"[^a-z0-9]+", "", name.lower())
        host = (urlparse(final).hostname or "").replace("www.", "")
        if slug and host.split(".")[0] == slug:
            return final
    return ""


def backfill_charity(charity, *, force: bool = False) -> bool:
    """Give one charity a website (if missing) and a logo. True if it changed.

    Safe to call on a charity that already has both — it does nothing unless
    `force`. Never raises: a charity that cannot be resolved is stamped as
    tried so the next pass skips it rather than hammering a dead domain.
    """
    if charity.logo and not force:
        return False

    changed = False
    website = charity.website
    if not website:
        resolved = resolve_website(charity.name)
        if resolved:
            charity.website = website = resolved
            changed = True

    try:
        png, source = fetch_logo(website)
    except LogoNotFound as e:
        logger.info("No logo for %s: %s", charity.name, e)
    else:
        charity.logo.save(f"{charity.slug}.png", ContentFile(png), save=False)
        logger.info("Logo for %s from %s", charity.name, source)
        changed = True

    charity.logo_fetched_at = timezone.now()
    charity.save(update_fields=["website", "logo", "logo_fetched_at"])
    return changed


def backfill_in_background(charity) -> None:
    """Fetch a suggested charity's logo without making anyone wait for it.

    A charity is created in the middle of the signup wizard, and the fetch
    talks to a third-party web server that may be slow, redirecting or down.
    None of that may be allowed to sit between somebody and their new
    organisation, and the card renders correctly from initials in the
    meantime — so this is fire-and-forget by design.

    Daemon thread deliberately: if the process is going down, an in-flight
    logo fetch is not a reason to hold it open.
    """
    import threading

    def run():
        try:
            backfill_charity(charity)
        except Exception:               # noqa: BLE001 — never surface from a thread
            logger.exception("Background logo fetch failed for %s", charity)

    threading.Thread(target=run, daemon=True, name="charity-logo").start()
