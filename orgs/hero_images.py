"""Pictures for an in-app hero, taken from the product's own media library.

CLIENT BRIEF, 22 SEP 2026, for the Groups page hero: "the image MUST NOT be
hardcoded ... use images that already exist in the application's image/media
system ... if multiple suitable images exist, support a rotating behaviour ...
if there is no image available, gracefully fall back to a clean dark-green
visual area rather than using an external image."

WHERE THE PICTURES COME FROM. sysadmin.media_library.site_assets() already
enumerates every picture shipped under static/img — it is what the super-admin
media screen lists, and it is the one inventory in the codebase that knows what
images actually exist. Reading it here means a picture added to the folder is
available to this hero without anybody editing a template, which is what
"configurable from the backend rather than hardcoded" asks for.

WHY A CURATED PREFIX LIST AND NOT EVERYTHING. site_assets() returns favicons,
the wordmark, sprite sheets, avatars and eight loader frames alongside the
photographs. A hero that rotates through a favicon is worse than one that does
not rotate. PREFERRED names the folders that hold photographs; anything outside
them is ignored rather than filtered afterwards, so a new folder of icons
cannot start appearing behind a heading.

DETERMINISTIC PER ORGANISATION. The starting picture is chosen from the
organisation's id rather than at random, so a given organisation's Groups page
looks the same on every visit — a hero that is a different photograph on each
reload reads as the page not having settled yet. The rotation then moves on
from there in the browser.
"""
from __future__ import annotations

# Folders under static/ whose contents are photographs rather than furniture.
PREFERRED = ("img/scenes/",)

# Pictures directly in img/ that are photographs. Named individually because
# that folder also holds the logo, the favicons and the avatars.
EXTRA = ("img/stadium-night.jpg", "img/nrlw-players.jpg")

# How many the hero rotates through. More than four is a slideshow nobody
# watches to the end; fewer than two is not a rotation.
HOW_MANY = 4


def hero_images(seed: int = 0, how_many: int = HOW_MANY) -> list[str]:
    """URLs for a rotating hero, or [] when the library has no photographs.

    An empty list is a real answer, not a failure: the template draws its
    plain dark-green panel instead, which is what the brief asks for rather
    than a placeholder from somewhere else.
    """
    try:
        from sysadmin.media_library import site_assets

        assets = site_assets()
    except Exception:  # noqa: BLE001 — a hero picture is never worth a 500
        return []

    pool = [
        a.url for a in assets
        if a.kind == "image"
        and (a.key.startswith(PREFERRED) or a.key in EXTRA)
    ]
    if not pool:
        return []

    # Rotate the list so different organisations open on different pictures,
    # without any of them getting a random one.
    start = (seed or 0) % len(pool)
    ordered = pool[start:] + pool[:start]
    return ordered[:how_many]
