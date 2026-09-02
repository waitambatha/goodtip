"""Find the editable blocks in a rendered page, and swap in an admin's edits.

WHY THE HTML IS REWRITTEN ON THE WAY OUT
----------------------------------------
The other way to build this is to mark up every editable string in every
template by hand — `{% editable "home.hero" %}…{% endeditable %}` around each
one. That is precise, and it is also several hundred edits across forty
templates, every one of which is a chance to miss a sentence and a permanent
tax on writing new pages. The client asked for "everything that is on the
page, words and images", which is exactly the thing a per-string opt-in is bad
at.

So the page is parsed once on its way out and the blocks are found by shape:
the elements that carry text and contain no smaller block of their own. That
is the same set a person would point at.

TWO PASSES, NOT ONE
-------------------
Whether a <p> is a block depends on what is inside it — a <p> holding two <li>
is not the thing you edit, the <li>s are. That cannot be decided while
streaming, so the document is tokenised first, elements are matched to their
closing tags, and only then is each one judged.

FAITHFUL REASSEMBLY
-------------------
Every token is re-emitted as it arrived — start tags come back from
`get_starttag_text()` verbatim, entities are re-encoded as they were written —
so a page with no edits and no edit mode comes out byte-identical to the way it
went in. That property is what makes this safe to put in front of every page,
and `tests.py` asserts it.
"""
import hashlib
import re
from html.parser import HTMLParser


# Elements that hold a paragraph's worth of text. An element is a block only
# if it contains none of these, so a <li> beats the <ul>'s parent <div> and a
# <p> inside a <blockquote> beats the blockquote.
PRIMARY_TAGS = {
    "h1", "h2", "h3", "h4", "h5", "h6", "p", "li", "blockquote", "figcaption",
    "dt", "dd", "td", "th", "caption", "summary", "legend",
}

# Things that often carry a standalone piece of copy without a block around
# it — a button's label, a link in a nav, a stat's caption in a <span>. Only
# considered when there is no PRIMARY element anywhere inside.
SECONDARY_TAGS = {"a", "button", "label", "small", "span", "strong", "b", "em", "i"}

CANDIDATE_TAGS = PRIMARY_TAGS | SECONDARY_TAGS

# Never look inside these. Script and style bodies are code; svg is drawing
# instructions; a <title> or <option> cannot hold markup, so making it
# contenteditable would produce a page that renders the tags as text.
OPAQUE_TAGS = {
    "script", "style", "svg", "template", "textarea", "select", "option",
    "head", "title", "noscript", "pre", "code", "iframe", "canvas", "math",
}

# No end tag, so they never open an element range.
VOID_TAGS = {
    "area", "base", "br", "col", "embed", "hr", "img", "input", "link",
    "meta", "param", "source", "track", "wbr",
}

# Site furniture, skipped whole.
#
# <nav> is here because of what it did to the page in practice: the brand mark
# is a link wrapping "GOOD", "TIP" and a full stop in three separate spans, so
# the first thirteen editable blocks on every page were nav fragments — one of
# them a single "." — before the reader reached the headline. A menu is also
# the worst possible fit for the per-page scoping this feature has: edits
# belong to the page they were made on, so "rename the menu item" would rename
# it on exactly one page and look broken everywhere else.
FURNITURE_TAGS = {"nav"}

# The same idea by class: the editor's own chrome, and the parts of a page
# that are transient or generated rather than written — flash messages, the
# notification list, the loader, screen-reader-only text.
SKIP_CLASSES = {
    "gte-bar", "flash", "flash-stack", "bp-item", "bp-empty", "bp-head",
    "gt-loader", "loader", "loader-inner", "sr-only", "visually-hidden",
    "app-nav", "an-inner", "app-sheet", "sheet-panel",
}

_WS_RE = re.compile(r"\s+")


def block_key(tag: str, inner_html: str) -> str:
    """The stable name for one block: its tag plus a hash of its own wording.

    Whitespace is collapsed first so that re-indenting a template does not
    orphan every edit on the page.
    """
    norm = _WS_RE.sub(" ", inner_html or "").strip()
    digest = hashlib.sha1(norm.encode("utf-8", "replace")).hexdigest()[:16]
    return f"{tag}-{digest}"


class _Token:
    __slots__ = ("kind", "tag", "raw", "attrs")

    def __init__(self, kind, raw, tag=None, attrs=None):
        self.kind = kind      # start | end | void | text | other
        self.raw = raw
        self.tag = tag
        self.attrs = attrs or {}


class _Tokeniser(HTMLParser):
    """Splits the document into a flat list, keeping each piece's original text."""

    def __init__(self):
        # convert_charrefs=False so &amp; comes through as an entity token and
        # can be written back exactly as it was, rather than being decoded to
        # "&" and re-encoded differently (or not at all).
        super().__init__(convert_charrefs=False)
        self.tokens = []

    def handle_starttag(self, tag, attrs):
        raw = self.get_starttag_text()
        kind = "void" if tag in VOID_TAGS else "start"
        self.tokens.append(_Token(kind, raw, tag, dict(attrs)))

    def handle_startendtag(self, tag, attrs):
        self.tokens.append(_Token("void", self.get_starttag_text(), tag, dict(attrs)))

    def handle_endtag(self, tag):
        if tag in VOID_TAGS:
            # A stray "</br>". It opened nothing, so it closes nothing.
            self.tokens.append(_Token("other", f"</{tag}>"))
            return
        self.tokens.append(_Token("end", f"</{tag}>", tag))

    def handle_data(self, data):
        self.tokens.append(_Token("text", data))

    def handle_entityref(self, name):
        self.tokens.append(_Token("text", f"&{name};"))

    def handle_charref(self, name):
        self.tokens.append(_Token("text", f"&#{name};"))

    def handle_comment(self, data):
        self.tokens.append(_Token("other", f"<!--{data}-->"))

    def handle_decl(self, decl):
        self.tokens.append(_Token("other", f"<!{decl}>"))

    def handle_pi(self, data):
        self.tokens.append(_Token("other", f"<?{data}>"))

    def unknown_decl(self, data):
        self.tokens.append(_Token("other", f"<![{data}]>"))


def _match_ranges(tokens):
    """{start index: end index} for every properly closed element.

    A start tag with no matching end — which browsers forgive and templates
    occasionally contain — simply gets no entry, and so is never treated as a
    block. Better to leave one element uneditable than to guess at where it
    stopped and rewrite half the page.
    """
    ranges = {}
    stack = []
    for i, tok in enumerate(tokens):
        if tok.kind == "start":
            stack.append((tok.tag, i))
        elif tok.kind == "end":
            for depth in range(len(stack) - 1, -1, -1):
                if stack[depth][0] == tok.tag:
                    ranges[stack[depth][1]] = i
                    # Anything still open inside this one was never closed.
                    del stack[depth:]
                    break
    return ranges


def _classes(tok):
    return set((tok.attrs.get("class") or "").split())


def find_blocks(html: str):
    """The editable blocks in this page.

    Returns (tokens, ranges, blocks) where blocks is a list of
    (start_index, end_index, key, kind) in document order. `kind` is "text"
    for an element whose inner HTML is editable, "image" for an <img>.
    """
    parser = _Tokeniser()
    parser.feed(html)
    parser.close()
    tokens = parser.tokens
    ranges = _match_ranges(tokens)

    blocks = []
    seen = {}

    # How deep we are inside something opaque, and the index at which the
    # current block ends (nothing inside a block is itself a block).
    opaque_until = []
    skip_until = []
    block_end = -1

    for i, tok in enumerate(tokens):
        # Leaving a region we were told to stay out of.
        opaque_until = [end for end in opaque_until if end > i]
        skip_until = [end for end in skip_until if end > i]

        if tok.kind not in ("start", "void"):
            continue

        end = ranges.get(i, -1)

        if tok.tag in OPAQUE_TAGS:
            if end > i:
                opaque_until.append(end)
            continue
        if (
            tok.tag in FURNITURE_TAGS
            or _classes(tok) & SKIP_CLASSES
            or "data-no-edit" in tok.attrs
        ):
            if end > i:
                skip_until.append(end)
            continue
        if opaque_until or skip_until:
            continue

        # Inside a block already claimed, nothing else is its own block —
        # including a picture, which is part of that block's HTML and is
        # edited by editing it. Two mechanisms over one <img> would each
        # think they owned its src.
        if i < block_end:
            continue

        if tok.tag == "img":
            key = block_key("img", tok.attrs.get("src") or "")
            blocks.append((i, i, _unique(key, seen), "image"))
            continue

        if tok.kind != "start" or end <= i:
            continue
        if tok.tag not in CANDIDATE_TAGS:
            continue

        inner = tokens[i + 1:end]
        if not any(t.kind == "text" and t.raw.strip() for t in inner):
            continue
        # A block that contains a smaller block is not the thing you edit.
        if any(t.kind == "start" and t.tag in PRIMARY_TAGS for t in inner):
            continue
        if any(t.kind in ("start", "void") and t.tag in OPAQUE_TAGS for t in inner):
            continue

        inner_html = "".join(t.raw for t in inner)
        blocks.append((i, end, _unique(block_key(tok.tag, inner_html), seen), "text"))
        block_end = end

    return tokens, ranges, blocks


def _unique(key, seen):
    """Two blocks with identical wording on one page get -2, -3 and so on.

    The order they appear in is the order they are numbered, so the suffix is
    as stable as the wording itself.
    """
    n = seen.get(key, 0) + 1
    seen[key] = n
    return key if n == 1 else f"{key}-{n}"


def _tag_with(raw: str, extra: str) -> str:
    """Add attributes to a start tag, keeping everything already on it."""
    if raw.endswith("/>"):
        return raw[:-2].rstrip() + " " + extra + "/>"
    return raw[:-1].rstrip() + " " + extra + ">"


def _attr(value: str) -> str:
    return (
        str(value).replace("&", "&amp;").replace('"', "&quot;")
        .replace("<", "&lt;").replace(">", "&gt;")
    )


def _with_alt(raw: str, alt: str) -> str:
    """Set the alt attribute on one <img> tag, adding it if it has none."""
    if re.search(r'\salt\s*=', raw, re.IGNORECASE):
        return re.sub(
            r'(\salt\s*=\s*)("[^"]*"|\'[^\']*\'|[^\s>]+)',
            lambda m: m.group(1) + '"' + _attr(alt) + '"',
            raw, count=1, flags=re.IGNORECASE,
        )
    return _tag_with(raw, f'alt="{_attr(alt)}"')


def rewrite(html: str, edits: dict, *, edit_mode: bool = False):
    """Apply `edits` to `html`, optionally tagging the blocks for the editor.

    `edits` is {block_key: (kind, replacement, alt)} — replacement being the
    new inner HTML for a text block, or the new src for an image; `alt` is used
    only for images and may be absent on an older caller.

    Returns (html, applied_keys). With no edits and edit_mode off, the HTML
    comes back exactly as it went in.
    """
    if not edits and not edit_mode:
        return html, set()

    tokens, ranges, blocks = find_blocks(html)
    applied = set()

    # start index -> what to do there, so the emit loop stays a single pass.
    plan = {}
    for start, end, key, kind in blocks:
        edit = edits.get(key)
        plan[start] = (end, key, kind, edit)
        if edit is not None:
            applied.add(key)

    out = []
    skip_to = -1
    for i, tok in enumerate(tokens):
        if i < skip_to:
            continue

        job = plan.get(i)
        if job is None:
            out.append(tok.raw)
            continue

        end, key, kind, edit = job

        if kind == "image":
            raw = tok.raw
            if edit is not None and edit[1]:
                # Swap the src, keep every other attribute — sizes, loading,
                # the lot.
                raw = re.sub(
                    r'(\ssrc\s*=\s*)("[^"]*"|\'[^\']*\'|[^\s>]+)',
                    lambda m: m.group(1) + '"' + _attr(edit[1]) + '"',
                    raw, count=1, flags=re.IGNORECASE,
                )
                # ALT IS NOT ONE OF THE ATTRIBUTES THAT SURVIVES A SWAP.
                #
                # Everything else on the tag still describes the slot: how big
                # it is, when to load it. `alt` describes the PICTURE, and the
                # picture has just been replaced — so keeping the old one meant
                # every swapped photograph on the site was announced as the one
                # it replaced. An admin who typed a description gets it; one who
                # left it empty gets alt="", which is correct markup for a
                # decorative image and honest about saying nothing.
                raw = _with_alt(raw, edit[2] if len(edit) > 2 else "")
            elif edit is not None and len(edit) > 2 and edit[2]:
                # ALT ON ITS OWN, no new file.
                #
                # The picture on the page is the right picture and its
                # description is wrong or missing — which is most of them,
                # because the descriptions were written into the templates by
                # whoever put the photograph there. Without this the only way to
                # fix one from the admin was to replace a picture that did not
                # need replacing.
                raw = _with_alt(raw, edit[2])
            if edit_mode:
                raw = _tag_with(raw, f'data-gte-img="{_attr(key)}"')
            out.append(raw)
            continue

        raw = tok.raw
        if edit_mode:
            raw = _tag_with(raw, f'data-gte="{_attr(key)}"')
        out.append(raw)

        if edit is not None:
            out.append(edit[1])
            # Everything between the tags is replaced, so jump the original
            # content *and* its closing tag, which is re-emitted here.
            out.append(tokens[end].raw)
            skip_to = end + 1

    return "".join(out), applied
