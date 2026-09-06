#!/usr/bin/env python3
"""Build static/data/emoji.json from Unicode's own emoji-test.txt.

WHY A GENERATOR AND NOT A HAND-TYPED LIST. The picker started as thirty
characters written into the composer template, then about thirteen hundred. Both
were typed by hand, which means both were somebody's guess at what people reach
for, in somebody's order, with no names to search by — and every one of them was
re-sent inside the markup on every render of the chat pane.

This reads the file the Unicode Consortium publishes for exactly this purpose
(UTS #51's keyboard/display test data), which carries every emoji, its official
name, and the group and subgroup a keyboard is meant to sort it into. So the
categories are not a design decision anybody has to maintain: they are the ones
every other keyboard already uses.

    python scripts/build_emoji.py path/to/emoji-test.txt
    python scripts/build_emoji.py --url        # fetch 15.0 from unicode.org

The output is committed. This is a build step, not a runtime one: nothing in the
app fetches from unicode.org, and the file only changes when somebody runs this
against a newer Unicode release.

WHAT IS LEFT OUT, and why:

  names                 lowercased, because the search box lowercases what you
                        type and "Christmas tree" would otherwise be unfindable.
  not fully-qualified   emoji-test.txt lists the same character several times,
                        once per qualification. Only the fully-qualified form is
                        what a keyboard should insert; the others are there for
                        implementations to recognise, not to offer.
  skin-tone variants    five copies of every hand and person, which is a tone
                        PICKER's job and not a reason to make the grid five
                        times longer. The base emoji is kept.
  the Component group   skin tones and hair styles on their own. They are
                        modifiers, not emoji anybody sends.
"""
from __future__ import annotations

import json
import re
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent.parent
OUT = HERE / "static" / "data" / "emoji.json"
URL = "https://www.unicode.org/Public/emoji/15.0/emoji-test.txt"

# Unicode's ten groups, in its order, with the two the keyboards merge actually
# merged: "Smileys & Emotion" and "People & Body" are one tab on every phone
# anybody has used, and splitting them puts 👍 three tabs away from 🙂.
#
# `tab` is the glyph on the tab itself. Chosen rather than derived: the first
# emoji of a group is an accident of codepoint order (Symbols would open on a
# cash-register sign), and a tab is a signpost.
GROUPS = [
    ("smileys",  "Smileys & people",  "🙂", ["Smileys & Emotion", "People & Body"]),
    ("nature",   "Animals & nature",  "🐶", ["Animals & Nature"]),
    ("food",     "Food & drink",      "🍔", ["Food & Drink"]),
    ("activity", "Activity",          "⚽", ["Activities"]),
    ("travel",   "Travel & places",   "🚗", ["Travel & Places"]),
    ("objects",  "Objects",           "💡", ["Objects"]),
    ("symbols",  "Symbols",           "❤️", ["Symbols"]),
    ("flags",    "Flags",             "🏳️", ["Flags"]),
]

SKIN_TONES = {0x1F3FB, 0x1F3FC, 0x1F3FD, 0x1F3FE, 0x1F3FF}
LINE = re.compile(
    r"^(?P<codes>[0-9A-F ]+?)\s*;\s*(?P<status>[\w-]+)\s*#\s*\S+\s+E[\d.]+\s+(?P<name>.+)$"
)


def parse(text: str):
    """(group, subgroup, char, name) for every fully-qualified emoji."""
    group = subgroup = ""
    for raw in text.splitlines():
        line = raw.strip()
        if line.startswith("# group:"):
            group = line.split(":", 1)[1].strip()
            continue
        if line.startswith("# subgroup:"):
            subgroup = line.split(":", 1)[1].strip()
            continue
        if not line or line.startswith("#"):
            continue
        m = LINE.match(line)
        if not m or m.group("status") != "fully-qualified":
            continue
        points = [int(c, 16) for c in m.group("codes").split()]
        if SKIN_TONES.intersection(points):
            continue
        # LOWERCASED HERE, once, rather than in the browser on every keystroke.
        # Most Unicode short names already are ("grinning face"); the exceptions
        # are proper nouns — "Christmas tree", "flag: Australia" — and the
        # picker's search lowercases what you type, so leaving those capitalised
        # meant typing "christmas" found nothing.
        yield group, subgroup, "".join(chr(c) for c in points), m.group("name").lower()


def build(text: str) -> dict:
    by_group: dict[str, list] = {}
    for group, _subgroup, char, name in parse(text):
        by_group.setdefault(group, []).append([char, name])

    out = []
    for key, label, tab, sources in GROUPS:
        rows = [row for src in sources for row in by_group.get(src, [])]
        if not rows:
            raise SystemExit(f"no emoji found for {label} ({sources}) — wrong file?")
        out.append({"key": key, "label": label, "tab": tab, "emoji": rows})

    known = {s for _k, _l, _t, srcs in GROUPS for s in srcs} | {"Component"}
    for unseen in sorted(set(by_group) - known):
        print(f"  ! group not mapped, dropped: {unseen}", file=sys.stderr)
    return {"groups": out}


def main() -> None:
    arg = sys.argv[1] if len(sys.argv) > 1 else "--url"
    if arg == "--url":
        from urllib.request import urlopen

        print(f"fetching {URL}")
        text = urlopen(URL, timeout=60).read().decode("utf-8")
    else:
        text = Path(arg).read_text(encoding="utf-8")

    data = build(text)
    OUT.parent.mkdir(parents=True, exist_ok=True)
    # Compact, and NOT ensure_ascii: the whole point is the characters, and
    # \u-escaping every one of them doubles the file for no reader's benefit.
    OUT.write_text(
        json.dumps(data, ensure_ascii=False, separators=(",", ":")), encoding="utf-8"
    )
    total = sum(len(g["emoji"]) for g in data["groups"])
    print(f"wrote {OUT.relative_to(HERE)} — {total} emoji in {len(data['groups'])} groups")
    for g in data["groups"]:
        print(f"  {g['tab']} {g['label']:<20} {len(g['emoji']):>5}")


if __name__ == "__main__":
    main()
