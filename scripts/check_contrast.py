#!/usr/bin/env python
"""Find text nobody can read, by computing the ratio instead of squinting at it.

    venv/bin/python scripts/check_contrast.py            # every theme
    venv/bin/python scripts/check_contrast.py --theme light
    venv/bin/python scripts/check_contrast.py --max 3.5  # only the bad ones

WHY THIS EXISTS
---------------
The client, Sep 2026: "make sure on the different themes text — and I mean ALL
text — can be seen. That has been an issue where, let's say it's a light
theme, the text has a shade that is not easy to see, even on the cream. So go
page by page confirming that."

Page by page is how you find the first six. There are four palettes here —
the member app's green and light, and the admin shell's light and dark — and
the same rule is painted on a different ground in each, so "looks fine" on the
screen you happen to have open is not evidence about the other three. This
reads the stylesheets and does the arithmetic for all four.

HOW IT DECIDES WHAT IS PAINTED ON WHAT
--------------------------------------
Each theme is a stack of token blocks: the light theme is `:root` with the
light block layered over it, and so on. A rule's text colour is resolved
through that stack, and its background either comes from the rule itself or —
where it has none, which is most of them — from the surfaces that theme
actually uses.

IT REPORTS THE KINDEST READING. A translucent background is composited over
each candidate surface first, because rgba(200,241,53,.12) is not a colour
until you know what is behind it, and a rule with no background of its own is
tried on every surface. The BEST result is the one printed: if even the most
generous ground fails, the pair is unreadable somewhere for certain. That
keeps the list short enough to act on and free of pairs that never occur.

WHAT IT DELIBERATELY DOES NOT FLAG
----------------------------------
Colour that is not carrying information — the outsized watermark numerals
behind a card, a hairline drawn as text, a decorative arrow. Those are listed
in DECORATION below, by selector, each with the reason. Everything else under
4.5:1 is a finding. Non-text marks are held to 3:1, which is the standard's
own bar for them, and brand glyphs (a network's own red or blue) are exempt
from being recoloured into something that is no longer the brand.
"""
import argparse
import re
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent

HEX = re.compile(r"#([0-9a-fA-F]{3}|[0-9a-fA-F]{6})\b")
RGBA = re.compile(r"rgba?\(\s*(\d+)\s*,\s*(\d+)\s*,\s*(\d+)\s*(?:,\s*([\d.]+)\s*)?\)")
VAR = re.compile(r"var\(\s*(--[\w-]+)\s*(?:,\s*(.+))?\)\s*$", re.S)
RULE = re.compile(r"([^{}]+)\{([^{}]*)\}")
COLOUR_DECL = re.compile(r"(?<![-\w])color\s*:\s*([^;]+)")
BG_DECL = re.compile(r"background(?:-color)?\s*:\s*([^;]+)")

DARK_ATTR = '[data-theme="dark"]'

# Rules whose colour is not carrying information. Each is here with its
# reason; a selector added without one is a finding being hidden rather than
# answered.
DECORATION = {
    ".card-lift .cl-n": "the outsized watermark numeral behind a card",
    ".card-lift:hover .cl-n": "the same numeral, on hover",
    ".on-dark .card-lift .cl-n": "the same numeral, on the deep ground",
    ".ladder-card .lrank": "the watermark rank behind a ladder card",
    ".founding-pro::after": "a decorative sheen over a panel",
    ".flow-seq .flow-arrow": "a chevron between steps, not a character to read",
    ".art-hero.noimg .noimg-mark": "the ghosted wordmark where a picture is missing",
    ".rdc-mark": "the ghosted wordmark where a picture is missing",
    ".amb-photo .ph": "initials behind a portrait placeholder",
    ".amb-portrait .pinitials": "initials behind a portrait placeholder",
    ".ned-sw-bar": "a 15x5px bar showing the chosen colour; there is no text in it",
    ".ned-fm-thumb": "the featured-media thumbnail; it holds a picture or a clip, and the "
                     "only words on it (the order badge, Picture/Video) set their own colour",
    ".ned-surface .gt-slides .gs-slide": "a slideshow thumbnail in the editor; it holds "
                                         "a picture or a clip and no text",
}

# Glyphs that are a brand's own colour. Recolouring them to pass is the same
# as not using the brand, so they are held to the 3:1 non-text bar instead.
BRAND = re.compile(r"s-(youtube|facebook|instagram|linkedin|x|whatsapp|telegram)\b")

# Marks rather than words: an icon, a crest, an avatar's initial, a medal.
# WCAG puts these at 3:1 (1.4.11 Non-text Contrast) rather than 4.5:1, and
# holding a 24px glyph to the bar for 12px prose makes the whole palette
# darker for no one's benefit. Listed rather than pattern-matched, so adding
# one is a decision somebody made.
NON_TEXT = {
    ".feat2:nth-child(6n+3) .feat-ic": "a feature icon beside its own heading",
    ".pg-scope-tab .pgs-ic": "the tab's icon; the tab's label carries the meaning",
    ".pod.third .medal": "the bronze medal glyph on a podium card",
    ".charity-icon": "a cause glyph on its own tinted disc",
    ".composer .av": "the initial in an avatar disc",
}

# Rules whose ground is set somewhere this checker cannot see — by a parent, a
# sibling rule on the same element, or an inline custom property written by the
# server. Each is here with what actually paints it, because the alternative is
# a checker that reports five confident falsehoods and stops being read.
PAINTED_ON = {
    ".gt-tip .r .k": "inside .gt-tip, which sets background: var(--gt-deep)",
    ".gt-tip b": "inside .gt-tip, which sets background: var(--gt-deep)",
    ".gts-theme button.on:hover": ".gts-theme button.on sets the forest pill under it",
    'html[data-theme="dark"] .gts-theme button.on': "the lime pill, set two rules below",
    ".gta-tbl-acts .gta-btn": "--card-accent, written inline from sysadmin/hub.py, "
                              "with --card-ink paired to it by hub.INK",
    ".gta-tbl-acts .gta-btn:hover": "as above",
    ".gta-shell .object-tools a.addlink": "--tbl-accent, written into the page head "
                                          "by admin/base_site.html, with --tbl-ink paired to it",
    ".ned-tag-check input:checked + span": "white on the ticked code's own colour, --ntc, "
                                           "set per chip; each one is measured by "
                                           "goodtip.tests.ContrastTests."
                                           "test_every_story_tag_chip_is_readable_when_ticked",
    ".ned-toolbar .ned-menu-item.is-on em": "inside .ned-menu-item.is-on, which fills "
                                            "the row with lime; deep green on lime is 12.9:1",
    ".ned-publish-row label": "the story editor's save row, which is a dark green "
                              "gradient this checker cannot parse; white on #0C2A1F "
                              "is 14.6:1",
}


def parse_colour(text):
    text = text.strip().rstrip("!important").strip()
    if text.startswith("#"):
        m = HEX.match(text)
        if m:
            h = m.group(1)
            if len(h) == 3:
                h = "".join(c * 2 for c in h)
            return (int(h[0:2], 16), int(h[2:4], 16), int(h[4:6], 16), 1.0)
    m = RGBA.match(text)
    if m:
        return (int(m.group(1)), int(m.group(2)), int(m.group(3)),
                float(m.group(4)) if m.group(4) else 1.0)
    return None


def resolve(value, table, depth=0):
    """A declaration's value as RGBA, following var() through `table`."""
    if depth > 6 or value is None:
        return None
    value = value.strip().replace("!important", "").strip()
    direct = parse_colour(value)
    if direct:
        return direct
    m = VAR.match(value)
    if m:
        name, fallback = m.group(1), m.group(2)
        if name in table:
            got = resolve(table[name], table, depth + 1)
            if got:
                return got
        if fallback:
            return resolve(fallback, table, depth + 1)
    return None


def dark_media_ranges(css):
    """The (start, end) spans of every `@media (prefers-color-scheme: dark)`.

    gtadmin.css declares its dark palette twice — once under
    `html[data-theme="dark"]` for the explicit choice, and once under
    `@media (prefers-color-scheme: dark) :root { ... }` for `auto`. The second
    one is a `:root` block like any other as far as a text search is concerned,
    so reading `:root` naively picks up the DARK values, later in the file,
    and hands them to the light theme. Every finding downstream of that is
    then wrong in the same direction.
    """
    spans = []
    for m in re.finditer(r"@media[^{]*prefers-color-scheme:\s*dark[^{]*\{", css):
        depth, j = 0, m.end() - 1
        while j < len(css):
            if css[j] == "{":
                depth += 1
            elif css[j] == "}":
                depth -= 1
                if depth == 0:
                    break
            j += 1
        spans.append((m.start(), j))
    return spans


def block_tokens(css, selector, want_dark=False):
    """The --name: value pairs in EVERY block matching `selector`, in order.

    Every, not the first: goodtip.css opens `:root` three times — the brand
    colours near the top, the app surfaces fifteen hundred lines down, the
    competition codes at the end — and reading only the first one leaves most
    of the palette unresolved. Which does not fail loudly: it makes the checker
    quietly unable to price the surfaces, so it falls back to the one ground it
    did resolve and reports the whole stylesheet as unreadable on it.
    """
    out = {}
    dark = dark_media_ranges(css)

    def inside_dark(pos):
        return any(a <= pos <= b for a, b in dark)

    at = 0
    while True:
        i = css.find(selector, at)
        if i < 0:
            return out
        if inside_dark(i) != want_dark:
            at = i + len(selector)
            continue
        # `:root` must not match `:root:not([data-theme="light"])` and friends:
        # those are a different block with a different palette.
        tail = css[i + len(selector):]
        if not tail.lstrip().startswith("{") and "," not in tail[:2]:
            at = i + len(selector)
            continue
        start = css.find("{", i)
        depth, j = 0, start
        while j < len(css):
            if css[j] == "{":
                depth += 1
            elif css[j] == "}":
                depth -= 1
                if depth == 0:
                    break
            j += 1
        out.update(re.findall(r"(--[\w-]+)\s*:\s*([^;}]+)", css[start:j]))
        at = j


def over(fg, bg):
    a = fg[3]
    return tuple(round(fg[i] * a + bg[i] * (1 - a)) for i in range(3))


def lum(rgb):
    def ch(v):
        v /= 255
        return v / 12.92 if v <= 0.04045 else ((v + 0.055) / 1.055) ** 2.4
    r, g, b = (ch(x) for x in rgb[:3])
    return 0.2126 * r + 0.7152 * g + 0.0722 * b


def ratio(fg, bg):
    a, b = lum(fg), lum(bg)
    hi, lo = max(a, b), min(a, b)
    return round((hi + 0.05) / (lo + 0.05), 2)


class Theme:
    """One palette, and the surfaces text is actually painted on in it.

    SURFACES ARE NAMED AS TOKENS, not as hex. `--gts-surface` is a white card
    on the admin's light palette and a dark panel on its dark one; writing the
    hex would mean maintaining a second copy of the palette here, and getting
    it wrong in exactly the direction that hides findings.
    """

    def __init__(self, name, files, layers, surfaces, other_theme, scope=None):
        self.name = name
        self.files = files
        self.other_theme = other_theme
        #: The [data-theme] attribute selector THIS theme's own overrides
        #: carry, or None for the palette that is the unscoped default.
        self.scope = scope
        self.tokens = {}
        for layer in layers:
            path, selector = layer[0], layer[1]
            want_dark = len(layer) > 2 and layer[2]
            self.tokens.update(
                block_tokens((REPO / path).read_text(), selector, want_dark))
        self.surfaces = {}
        for label, value in surfaces.items():
            got = resolve(value, self.tokens)
            if got:
                self.surfaces[label] = got
        self.note_overrides()

    def applies(self, selector):
        """Whether a rule is painted in THIS theme.

        A rule scoped to `html[data-theme="dark"]` never renders on the light
        palette, so measuring it against a white ground invents a failure that
        cannot happen. This is the single biggest source of noise in a check
        like this one.

        `html:not([data-theme="light"])` counts as dark-scoped: it is the
        partner rule inside an `@media (prefers-color-scheme: dark)`, and it
        means the same thing — this is what `auto` paints on a dark device.
        """
        if self.other_theme == DARK_ATTR and ':not([data-theme="light"])' in selector:
            return False
        return self.other_theme not in selector

    def note_overrides(self):
        """Selectors this theme restates under its own [data-theme] scope.

        `.gta-sec-n.alert` sets a dark amber that is right on white, and three
        lines later `html[data-theme="dark"] .gta-sec-n.alert` sets the light
        one that is right on the panel. Measuring the base rule against the
        dark palette reports a failure that the override has already fixed —
        and "fix" it by changing the base, which breaks the light theme. So a
        base rule with an override in this theme is skipped: the override is
        what paints, and the override is measured on its own account.
        """
        self._overridden = set()
        if self.scope is None:
            return
        paths = list(self.files)
        if self.name.startswith("admin"):
            paths.append("static/css/goodtip.css")
        for path in paths:
            css = (REPO / path).read_text()
            for m in RULE.finditer(css):
                for sel in m.group(1).split(","):
                    sel = sel.strip()
                    if not sel:
                        continue
                    sel = sel.splitlines()[-1].strip()
                    if self.scope in sel:
                        # "html[data-theme="dark"] .x" overrides ".x"
                        tail = sel.split("]", 1)[-1].strip()
                        if tail.startswith("body.gt-shell "):
                            tail = tail[len("body.gt-shell "):].strip()
                        if tail:
                            self._overridden.add(tail)

    def overridden(self, selector):
        return selector.strip() in getattr(self, "_overridden", ())


def themes():
    app = "static/css/goodtip.css"
    shell = "static/css/gt-shell.css"
    admin = "static/gtadmin/css/gtadmin.css"
    LIGHT, DARK = '[data-theme="light"]', '[data-theme="dark"]'
    # The surfaces text sits on, widest first. A rule with no background of
    # its own is tried on all of them and reported at its best.
    app_surfaces = {
        "card": "var(--app-card)", "second card": "var(--app-card-2)",
        "page": "var(--app-bg)", "deep green": "var(--deep)",
    }
    shell_surfaces = {
        "panel": "var(--gts-surface)", "raised panel": "var(--gts-surface-2)",
        "hover": "var(--gts-surface-3)", "sheet": "var(--gts-shell)",
        "card": "var(--gt-card)",
    }
    return [
        Theme("member / green", [app], [(app, ":root")], app_surfaces, LIGHT),
        Theme("member / light", [app],
              [(app, ":root"), (app, 'html[data-theme="light"] body.gt-app')],
              app_surfaces, DARK, scope=LIGHT),
        # gtadmin.css declares its own --gt-* palette and its own dark block,
        # and the admin skin paints from both that and gt-shell.css. Layering
        # only one of them leaves half the backgrounds unresolvable, which
        # shows up as every white-on-green pill being reported as white on
        # cream.
        Theme("admin / light", [shell, admin],
              [(app, ":root"), (admin, ":root"), (shell, ":root")],
              shell_surfaces, DARK),
        Theme("admin / dark", [shell, admin],
              [(app, ":root"), (admin, ":root"), (shell, ":root"),
               (admin, ":root", True), (shell, ":root", True),
               (admin, 'html[data-theme="dark"]'), (shell, 'html[data-theme="dark"]')],
              shell_surfaces, LIGHT, scope=DARK),
    ]


# goodtip.css components that are drawn INSIDE the admin shell. The admin
# palettes used to scan only gt-shell.css and gtadmin.css, so a goodtip.css
# rule painted on the dark HQ panel was never measured against it — which is
# how the story editor shipped with `background: #fff` and `color: var(--ink)`:
# fine on light, and near-white text on a white box on dark, i.e. a headline
# you could not see yourself typing. Only these prefixes are pulled in: the
# rest of goodtip.css is the member app, and measuring it on the admin
# palette would bury the real findings under components that never appear
# there.
SHELL_BORROWS = (".ned-", ".pub", ".pp-", ".sched")


def _rules(theme):
    """(path, css, match) for every rule this theme paints."""
    for path in theme.files:
        css = (REPO / path).read_text()
        for m in RULE.finditer(css):
            yield path, css, m
    if theme.name.startswith("admin"):
        path = "static/css/goodtip.css"
        css = (REPO / path).read_text()
        for m in RULE.finditer(css):
            sel = m.group(1).strip().splitlines()[-1].strip()
            if sel.startswith(SHELL_BORROWS) or any(
                    ("body.gt-shell " + b) in sel for b in SHELL_BORROWS):
                yield path, css, m


def findings(theme, floor=4.5):
    out = []
    for path, css, m in _rules(theme):
        if True:
            sel = m.group(1).strip().splitlines()[-1].strip()
            body = m.group(2)
            cm = COLOUR_DECL.search(body)
            inherited = False
            if not cm:
                # A BACKGROUND WITH NO COLOUR STILL HAS TEXT ON IT. It inherits
                # the ink — and that is the shape the story editor's toolbar
                # shipped in: `background: #fff` on the font, size and colour
                # pickers, text inherited from --ink, which the dark palette
                # makes near-white. Labels you could not read, and no `color:`
                # anywhere for this checker to have measured.
                #
                # Only for the editor rules borrowed into the admin shell,
                # where that bug lives. Across the member app most rules with a
                # dark background and no colour hold children that set their
                # own, and measuring those would bury the real findings.
                borrowed = path.endswith("goodtip.css") and theme.name.startswith("admin")
                if not borrowed or not BG_DECL.search(body):
                    continue
                fg = resolve("var(--ink)", theme.tokens)
                inherited = True
            else:
                fg = resolve(cm.group(1), theme.tokens)
            if not fg:
                continue
            if any(d in sel for d in DECORATION) or not theme.applies(sel):
                continue
            if sel in PAINTED_ON:
                continue
            if theme.overridden(sel):
                continue
            bgm = BG_DECL.search(body)
            bg = resolve(bgm.group(1), theme.tokens) if bgm else None
            if inherited and (bg is None or bg[3] < 1):
                continue

            best, where = 0, ""
            for label, ground in theme.surfaces.items():
                if bg is None:
                    paint = ground[:3]
                elif bg[3] >= 1:
                    paint, label = bg[:3], "its own background"
                else:
                    paint = over(bg, ground[:3])
                r = ratio(over(fg, paint), paint)
                if r > best:
                    best, where = r, label
                if bg is not None and bg[3] >= 1:
                    break

            bar = 3.0 if (BRAND.search(sel) or sel in NON_TEXT) else floor
            if best < bar:
                out.append({
                    "ratio": best, "file": path,
                    "line": css[:m.start()].count("\n") + 1,
                    "selector": sel,
                    "value": "(inherits --ink)" if inherited else cm.group(1).strip(),
                    "ground": where, "bar": bar,
                })
    return sorted(out, key=lambda f: f["ratio"])


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--theme", help="only this theme (substring match)")
    ap.add_argument("--max", type=float, default=4.5,
                    help="report pairs under this ratio (default 4.5, WCAG AA)")
    args = ap.parse_args()

    total = 0
    for theme in themes():
        if args.theme and args.theme.lower() not in theme.name.lower():
            continue
        found = findings(theme, args.max)
        total += len(found)
        print(f"\n=== {theme.name} " + "=" * (56 - len(theme.name)))
        if not found:
            print("  nothing under %.1f:1" % args.max)
            continue
        for f in found:
            print(f"  {f['ratio']:5.2f}  {f['file'].split('/')[-1]}:{f['line']:<6} "
                  f"{f['selector'][:50]:<50} {f['value'][:22]:<22} on {f['ground']}")
    print(f"\n{total} pair(s) under the bar.")
    return 1 if total else 0


if __name__ == "__main__":
    sys.exit(main())
