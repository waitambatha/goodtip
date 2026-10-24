"""The coming-soon pages' one menu (client, 23 Oct 2026: "what do we have, 2
menus... need one clear menu for the coming soon pages").

There were four, and they disagreed: the trailer's bar, the "page tour" bar
inside each showcase (which added Terms and Privacy, and whose Blog opened a
filmed tour while the bar's Blog opened the real page), the footer (which
added Pricing), and the real pages' bar (the logo and nothing else). Every one
of them now reads this list, so they cannot drift apart again.

What an item does follows staging_gate.HOLDING_REAL_PAGES rather than being
written down twice: a page that is let through is a real link; one that is not
but has a filmed tour opens that tour over the trailer; one that has neither
is left out. That keeps this correct on both branches — staging lets five
pages through, main lets only the blog through.
"""
from django.conf import settings
from django.templatetags.static import static

from accounts.trailer import TRAILER_PEEKS

from .staging_gate import HOLDING_HOME_PATH, HOLDING_REAL_PAGES, NEWS_MEDIA_PREFIX

# (slug, label, the real page, icon, one line about it, picture)
_MAIN = [
    ("how-it-works", "How It Works", "/how-it-works/", "ic-f-lifebuoy",
     "The season end to end, from sign-up to the charity cheque.", "video/trailer/how-it-works.jpg"),
    ("wall", "The Wall", "/wall/", "ic-f-msg",
     "What tipping organisations choose to share with the world.", "video/trailer/wall-public.jpg"),
    ("blog", "Blog", "/news/", "ic-f-doc",
     "Stories, previews and news from around the grounds.", "video/trailer/news.jpg"),
    ("about", "About Us", "/about/", "ic-f-spark",
     "Why we built GoodTip, and who it is for.", "video/trailer/about.jpg"),
    ("pricing", "Pricing", "/pricing/", "ic-f-coins",
     "One price a year. Tipping is free for everyone playing.", "img/scenes/stadium-lights-grass.jpg"),
]
_LEGAL = [
    ("terms", "Terms & Conditions", "/terms/"),
    ("privacy", "Privacy Policy", "/privacy/"),
]
_TOURS = {p["slug"] for p in TRAILER_PEEKS}


def is_trailer_path(path: str) -> bool:
    return path in (HOLDING_HOME_PATH, "/")


def _resolve(slug, real, on_trailer, path):
    """(href, kind) for one item, or None when it has no way in at all."""
    if real in HOLDING_REAL_PAGES:
        return real, "page"
    if slug in _TOURS:
        return (f"#{slug}" if on_trailer else f"{HOLDING_HOME_PATH}#{slug}"), "tour"
    return None


def holding_menu(request) -> dict:
    path = request.path
    on_trailer = is_trailer_path(path)
    main = []
    for slug, label, real, icon, blurb, picture in _MAIN:
        found = _resolve(slug, real, on_trailer, path)
        if not found:
            continue
        href, kind = found
        main.append({
            "slug": slug, "label": label, "href": href, "kind": kind, "icon": icon,
            "blurb": blurb, "picture": static(picture),
            "current": kind == "page" and path.startswith(real),
        })
    legal = []
    for slug, label, real in _LEGAL:
        found = _resolve(slug, real, on_trailer, path)
        if found:
            legal.append({"slug": slug, "label": label, "href": found[0], "kind": found[1],
                          "current": found[1] == "page" and path.startswith(real)})
    # The waiting list's pop-ups only live on the trailer page, so from
    # anywhere else these go back to it and the address hash opens them.
    home = "#top" if on_trailer else HOLDING_HOME_PATH
    return {
        "on_trailer": on_trailer,
        "home_href": HOLDING_HOME_PATH,
        "trailer_href": home,
        "join_href": "#join" if on_trailer else f"{HOLDING_HOME_PATH}#join",
        "signin_href": "#signin" if on_trailer else f"{HOLDING_HOME_PATH}#signin",
        "main": main,
        "legal": legal,
        # What a link on these pages may still reach; anything else on this
        # site is a page not open yet, and gt-holding-menu.js keeps a click on
        # it where it is (client, 24 Oct 2026: "it does not take the user
        # anywhere"). "/" is the trailer itself.
        "open_paths": " ".join((HOLDING_HOME_PATH, *HOLDING_REAL_PAGES, NEWS_MEDIA_PREFIX, settings.STATIC_URL)),
    }
