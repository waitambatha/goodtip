"""Every picture and clip in the product, and where each one is shown.

WHY THIS EXISTS
---------------
CLIENT, 20 SEP 2026: "the admin did want some images removed, so that had me
thinking we need to add a functionality in the super admin that will be images
and videos — all the images and videos, when clicked, it should [show] where
and where it is being displayed, and have the option of changing it."

The request came out of a real dead end. Somebody asked for a photograph to
come off the site, and answering that meant opening the codebase and searching
it, because nothing in the admin could say where a given picture appeared — or
that it appeared at all. Half of what this product shows is not in the database
at all, so "look at the table" was never going to answer it.

TWO KINDS OF PICTURE, AND THEY ARE NOT THE SAME KIND OF THING
-------------------------------------------------------------
SITE PICTURES live in `static/img/` and are part of the build. A template or a
stylesheet names them, they are the same on every deployment, and nobody
uploaded them. Changing one is a change to the product.

UPLOADED MEDIA lives in MEDIA_ROOT and belongs to a row: this organisation's
logo, that charity's mark, the pictures on a story. Changing one is a change to
that row, and the admin already has a screen for it.

Both are listed here, and the difference is the whole reason the "where is it
shown" answer looks different for each. For an upload the answer is a ROW, and
the way to change it is that row's own form. For a site picture the answer is a
list of FILES, and the way to change what a visitor sees is the Pages editor —
which already swaps a picture on a page without anybody touching the repository
(admin_panel.models.PageEdit, KIND_IMAGE).

WHAT THIS DELIBERATELY DOES NOT DO
----------------------------------
It does not write to `static/`. A web request that overwrites a file inside the
git checkout would be undone by the next deploy — worse, `deploy.sh` rebases,
so an uncommitted modified file is a deploy that stops working rather than a
change that gets reverted. The honest answer for a site picture is "here is
exactly where it is used, and here is the screen that can change it", and that
is what the detail page gives.

MESSAGE ATTACHMENTS ARE NOT LISTED, and that is not an oversight. A file
someone sent inside a private conversation is not site media; putting the
company's photographs and a member's voice note in one gallery would make a
browsable archive of private uploads out of a tool for managing the brand.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path

from django.conf import settings
from django.templatetags.static import static
from django.urls import NoReverseMatch, reverse


#: Suffix → what kind of thing it is. Anything not here is not listed at all:
#: this is a gallery, and a .txt in the pictures folder is a mistake to fix
#: rather than a row to render.
IMAGE_SUFFIXES = {".jpg", ".jpeg", ".png", ".webp", ".gif", ".avif", ".svg"}
VIDEO_SUFFIXES = {".mp4", ".webm", ".mov", ".m4v"}

#: Where under static/ the product's own pictures live. A list rather than
#: "everything under static/" so vendored assets, sprite sheets and the css
#: folder do not arrive in the gallery as broken thumbnails.
SITE_DIRS = ["img"]


@dataclass
class Asset:
    """One picture or clip, from either source.

    `key` is what a URL carries to come back to this exact asset. For a site
    picture it is the path under static/; for an upload it is the owner and the
    row, because two rows can point at the same file and they are still two
    different things to manage.
    """
    key: str
    source: str          # "site" | "upload"
    kind: str            # "image" | "video"
    name: str
    url: str
    size: int = 0
    owner_label: str = ""
    owner_url: str = ""
    subtitle: str = ""

    @property
    def size_label(self) -> str:
        if not self.size:
            return ""
        if self.size < 1024:
            return f"{self.size} B"
        if self.size < 1024 * 1024:
            return f"{self.size / 1024:.0f} KB"
        return f"{self.size / (1024 * 1024):.1f} MB"


def _kind_for(suffix: str) -> str | None:
    s = suffix.lower()
    if s in IMAGE_SUFFIXES:
        return "image"
    if s in VIDEO_SUFFIXES:
        return "video"
    return None


# ---------------------------------------------------------------------------
# The site's own pictures
# ---------------------------------------------------------------------------

def _static_root() -> Path:
    """The source tree's static folder, not the collected one.

    STATICFILES_DIRS is what a developer edits and what git holds; STATIC_ROOT
    is a build artefact full of hashed duplicates of the same picture. Listing
    the build would show every photograph three times and give a path nobody
    can act on.
    """
    dirs = getattr(settings, "STATICFILES_DIRS", None) or []
    if dirs:
        return Path(dirs[0])
    return Path(settings.BASE_DIR) / "static"


def site_assets() -> list[Asset]:
    """Every picture and clip shipped with the product, newest folder first."""
    root = _static_root()
    out: list[Asset] = []
    for folder in SITE_DIRS:
        base = root / folder
        if not base.is_dir():
            continue
        for path in sorted(base.rglob("*")):
            if not path.is_file():
                continue
            kind = _kind_for(path.suffix)
            if kind is None:
                continue
            rel = path.relative_to(root).as_posix()
            try:
                size = path.stat().st_size
            except OSError:
                size = 0
            out.append(Asset(
                key=rel,
                source="site",
                kind=kind,
                name=path.name,
                url=static(rel),
                size=size,
                subtitle=path.parent.relative_to(root).as_posix(),
            ))
    return out


def site_asset(key: str) -> Asset | None:
    """One site picture by its path under static/.

    THE PATH IS FROM A URL, so it is checked rather than trusted: resolved
    against the static root and refused if it lands outside it. "../../.env" is
    a perfectly ordinary thing for a scanner to try, and a viewer that reads
    any file on disk is a viewer that reads the environment file.
    """
    root = _static_root().resolve()
    try:
        target = (root / key).resolve()
        target.relative_to(root)
    except (ValueError, OSError):
        return None
    if not target.is_file() or _kind_for(target.suffix) is None:
        return None
    for asset in site_assets():
        if asset.key == key:
            return asset
    return None


# ---------------------------------------------------------------------------
# Where a site picture is actually shown
# ---------------------------------------------------------------------------

#: Where a reference to a picture can be written. Templates name them through
#: {% static %}, stylesheets through url(), and a handful of Python modules
#: hold a list of them — orgs.services.WALLPAPERS is one, and it is exactly the
#: kind of reference that is invisible from the template side.
_SEARCH_DIRS = [
    ("templates", {".html", ".txt"}),
    ("static/css", {".css"}),
    ("static/js", {".js"}),
]
_SEARCH_PY_APPS = ["orgs", "accounts", "catalog", "admin_panel", "billing", "tipping", "goodtip"]

#: How many hits to read out. A picture used in a loop over a folder can match
#: a hundred lines and the answer is the same after the first ten.
MAX_HITS = 40


@dataclass
class Hit:
    path: str
    line: int
    text: str


def where_used(key: str) -> list[Hit]:
    """Every file that names this picture, with the line that names it.

    Matched on the FILE NAME rather than on the full path. A stylesheet writes
    `url(../img/scenes/mcg-stadium.jpg)`, a template writes
    `{% static 'img/scenes/mcg-stadium.jpg' %}` and a Python list writes
    `"img/scenes/mcg-stadium.jpg"` — three spellings of one reference, and the
    only part all three share is the name at the end. Names in here are
    distinctive enough for that to be safe; where two folders ever hold the
    same name, the answer is a slightly long list rather than a wrong one.
    """
    base = Path(settings.BASE_DIR)
    name = Path(key).name
    if not name:
        return []
    needle = re.compile(re.escape(name))
    hits: list[Hit] = []

    def scan(path: Path) -> None:
        if len(hits) >= MAX_HITS:
            return
        try:
            text = path.read_text(encoding="utf-8", errors="ignore")
        except OSError:
            return
        if name not in text:
            return
        for n, line in enumerate(text.splitlines(), start=1):
            if needle.search(line):
                hits.append(Hit(
                    path=path.relative_to(base).as_posix(),
                    line=n,
                    text=line.strip()[:220],
                ))
                if len(hits) >= MAX_HITS:
                    return

    for folder, suffixes in _SEARCH_DIRS:
        root = base / folder
        if not root.is_dir():
            continue
        for path in sorted(root.rglob("*")):
            if path.is_file() and path.suffix.lower() in suffixes:
                scan(path)

    for app in _SEARCH_PY_APPS:
        root = base / app
        if not root.is_dir():
            continue
        for path in sorted(root.rglob("*.py")):
            # Migrations name files only as historical defaults; a picture
            # mentioned in one is not "displayed" anywhere.
            if "migrations" in path.parts:
                continue
            scan(path)

    return hits


# ---------------------------------------------------------------------------
# Uploaded media
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class Owner:
    """One model field that holds an upload, described for a gallery.

    `label` is a callable rather than a string because the useful caption is
    the ROW — "Lifeline" beats "Charity logo" when you are looking for a
    particular mark among sixty of them.
    """
    ref: str
    fieldname: str
    title: str
    label: object
    kind_for: object = None


def _owners() -> list[Owner]:
    return [
        Owner("orgs.Organisation", "logo", "Organisation logos",
              lambda o: o.name),
        Owner("catalog.Charity", "logo", "Charity logos",
              lambda o: o.name),
        Owner("admin_panel.NewsPost", "image", "Story lead pictures",
              lambda o: o.title),
        Owner("admin_panel.NewsMedia", "file", "Story pictures and clips",
              lambda o: str(o),
              lambda o: "video" if o.kind == "video" else "image"),
        Owner("admin_panel.PageEdit", "image", "Pictures swapped in on a page",
              lambda o: f"{o.page} · {o.block_key}"),
        Owner("accounts.User", "avatar", "Member photographs",
              lambda o: o.display_name or o.email),
    ]


def _change_url(instance) -> str:
    meta = instance._meta
    try:
        return reverse(
            f"admin:{meta.app_label}_{meta.model_name}_change", args=[instance.pk],
        )
    except NoReverseMatch:
        return ""


def uploaded_assets() -> list[Asset]:
    """Every uploaded picture and clip, with the row it belongs to.

    Each owner is queried on its own and failures are swallowed per owner: a
    gallery that will not render because one app is mid-migration is a gallery
    nobody can use to answer the question they came with.
    """
    from django.apps import apps

    out: list[Asset] = []
    for owner in _owners():
        try:
            model = apps.get_model(*owner.ref.split(".", 1))
            rows = model.objects.exclude(**{owner.fieldname: ""}).exclude(
                **{f"{owner.fieldname}__isnull": True},
            )
            for row in rows.iterator():
                f = getattr(row, owner.fieldname, None)
                if not f:
                    continue
                kind = owner.kind_for(row) if owner.kind_for else _kind_for(Path(f.name).suffix)
                if kind is None:
                    continue
                try:
                    size = f.size
                except (OSError, ValueError):
                    size = 0
                try:
                    url = f.url
                except (ValueError, NotImplementedError):
                    url = ""
                out.append(Asset(
                    key=f"{owner.ref}:{owner.fieldname}:{row.pk}",
                    source="upload",
                    kind=kind,
                    name=Path(f.name).name,
                    url=url,
                    size=size,
                    owner_label=str(owner.label(row))[:120],
                    owner_url=_change_url(row),
                    subtitle=owner.title,
                ))
        except Exception:  # noqa: BLE001 — one broken owner must not empty the gallery
            continue
    return out


def uploaded_asset(key: str) -> Asset | None:
    for asset in uploaded_assets():
        if asset.key == key:
            return asset
    return None


def log_replacement(user, key: str, original_name: str, size: int) -> None:
    """Record that somebody overwrote a shipped picture.

    This is the one action in the library that edits the codebase, so it is
    the one that has to leave a trail. It goes in the admin's own log rather
    than a table of its own: "who changed what in here" already has an answer
    and a screen, and a second ledger nobody thinks to look at is worse than
    none. Best-effort — failing to write the note must not undo the write that
    already happened, or the screen would report a failure for a file that is
    sitting on disk.
    """
    try:
        from django.contrib.admin.models import CHANGE, LogEntry
        from django.contrib.contenttypes.models import ContentType

        from .models import MailLog  # any registered model gives a content type

        LogEntry.objects.log_action(
            user_id=user.pk,
            content_type_id=ContentType.objects.get_for_model(MailLog).pk,
            object_id=0,
            object_repr=f"static/{key}",
            action_flag=CHANGE,
            change_message=(
                f"Replaced the shipped picture static/{key} with "
                f"{original_name} ({size} bytes). Uncommitted change in the "
                "working tree."
            ),
        )
    except Exception:  # noqa: BLE001 — see above
        pass


def counts(assets: list[Asset]) -> dict:
    return {
        "total": len(assets),
        "images": sum(1 for a in assets if a.kind == "image"),
        "videos": sum(1 for a in assets if a.kind == "video"),
        "bytes": sum(a.size for a in assets),
    }
