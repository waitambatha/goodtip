"""Which public pages are editable, and what is editable on them.

Slots are DISCOVERED by reading template source rather than declared in a
registry. A registry is a second list to keep in step with the templates, and
the failure mode is silent: somebody adds a heading, forgets the registry, and
the client cannot edit the one line they asked about. Reading the source
cannot drift, because the source is the thing being edited.

Regex rather than Django's own parser: the parser needs a compiled template,
and compiling every public page to build an editor screen means executing
their tags — {% url %} against a request that does not exist, database reads
for a page nobody asked for. Matching two tags whose exact spelling this
project controls is the cheaper and more predictable of the two.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path

from django.conf import settings


# The public pages a client may edit, in the order they appear in the nav.
# `template` is what gets scanned; `title` is what the editor calls it.
PAGES = [
    {"slug": "home",     "title": "Home",            "template": "public/home.html",         "url_name": "landing"},
    {"slug": "how",      "title": "How it works",    "template": "public/how_it_works.html", "url_name": "how_it_works"},
    {"slug": "pricing",  "title": "Pricing",         "template": "public/pricing.html",      "url_name": "pricing"},
    {"slug": "about",    "title": "About us",        "template": "public/about.html",        "url_name": "about"},
    {"slug": "privacy",  "title": "Privacy policy",  "template": "public/privacy.html",      "url_name": "privacy"},
]

PAGES_BY_SLUG = {p["slug"]: p for p in PAGES}

# {% ptext "page" "key" %} ... {% endptext %}
_TEXT_RE = re.compile(
    r"""\{%\s*ptext\s+["'](?P<page>[\w-]+)["']\s+["'](?P<key>[\w.\-]+)["']\s*%\}"""
    r"""(?P<default>.*?)"""
    r"""\{%\s*endptext\s*%\}""",
    re.S,
)
# {% pimage "page" "slot" %} ... {% endpimage %}
_IMAGE_RE = re.compile(
    r"""\{%\s*pimage\s+["'](?P<page>[\w-]+)["']\s+["'](?P<slot>[\w.\-]+)["']\s*%\}""",
)


@dataclass
class Slot:
    key: str
    default: str
    value: str = ""
    group: str = ""

    @property
    def is_overridden(self) -> bool:
        return bool(self.value)

    @property
    def label(self) -> str:
        """"hero.title" reads as "Title" under a "Hero" heading."""
        tail = self.key.rsplit(".", 1)[-1]
        return tail.replace("_", " ").replace("-", " ").capitalize()

    @property
    def is_long(self) -> bool:
        """Whether the editor should give this a tall box.

        Driven off the DEFAULT rather than off the current value, so a field
        does not change shape when somebody shortens the text in it.
        """
        return len(self.default) > 90 or "\n" in self.default.strip()


@dataclass
class PageInfo:
    slug: str
    title: str
    template: str
    url_name: str
    slots: list = field(default_factory=list)
    image_slots: list = field(default_factory=list)

    @property
    def edited_count(self) -> int:
        return sum(1 for s in self.slots if s.is_overridden)


def _template_path(name: str) -> Path | None:
    for engine in settings.TEMPLATES:
        for base in engine.get("DIRS", []):
            candidate = Path(base) / name
            if candidate.exists():
                return candidate
    return None


def _tidy(raw: str) -> str:
    """Collapse a template's indentation out of a default string.

    Defaults are written across several indented lines to fit the markup they
    sit in; the client should see the sentence, not the indentation.
    """
    lines = [line.strip() for line in raw.strip().splitlines()]
    return " ".join(line for line in lines if line)


def discover(slug: str) -> PageInfo | None:
    """Every editable slot on one page, with its default and current value."""
    meta = PAGES_BY_SLUG.get(slug)
    if meta is None:
        return None
    info = PageInfo(**meta)
    path = _template_path(meta["template"])
    if path is None:
        return info                       # page exists, template does not yet
    source = path.read_text(encoding="utf-8")

    from .models import PageText

    overrides = dict(
        PageText.objects.filter(page=slug).values_list("key", "value")
    )
    seen = set()
    for m in _TEXT_RE.finditer(source):
        if m.group("page") != slug or m.group("key") in seen:
            continue
        seen.add(m.group("key"))
        key = m.group("key")
        info.slots.append(Slot(
            key=key,
            default=_tidy(m.group("default")),
            value=overrides.get(key, ""),
            group=key.rsplit(".", 1)[0] if "." in key else "",
        ))
    for m in _IMAGE_RE.finditer(source):
        if m.group("page") == slug and m.group("slot") not in info.image_slots:
            info.image_slots.append(m.group("slot"))
    return info


def all_pages() -> list:
    """Every editable page, each with its slot counts filled in."""
    return [p for p in (discover(meta["slug"]) for meta in PAGES) if p is not None]
