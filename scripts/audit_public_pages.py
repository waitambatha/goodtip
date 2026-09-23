"""Fetch every public page and look for the things that actually break.

Run against a live server (default http://localhost:8000) after touching
anything on the public side:

    python3 scripts/audit_public_pages.py [base-url]

WHAT IT CATCHES, and why each one is here rather than being caught by the test
suite. Django's tests render templates in isolation and assert on content;
none of them look at a whole assembled page, which is where these live:

  * DUPLICATE ids — two elements claiming #start on the home page was a real
    bug on 22 Sep 2026, introduced by a partial whose default id collided with
    a new section. Which one an anchor reached was decided by source order.
  * DEAD ANCHORS — /#start was linked from 17 places across 8 pages for months
    with no element of that id anywhere. Every one of them silently scrolled
    to the top of the page instead.
  * RAW TEMPLATE SYNTAX — a {% templatetag openblock %} that reaches the browser means a tag was
    mistyped or a comment left unclosed, and it renders as visible text.
  * EMPTY SECTIONS — a section whose content is entirely inside a failed
    condition still renders its padding, so the page gets a blank band.
  * BROKEN STATIC — every src, href and CSS url() under /static/ is fetched.
    A renamed image is invisible until somebody scrolls to it.

Exit code 1 if anything is found, so it can gate a deploy.
"""
import re, sys, urllib.request, collections

PAGES = ["/", "/pricing/", "/terms/", "/privacy/", "/about/", "/sponsorship/",
         "/how-it-works/", "/wall/", "/coming-soon/", "/leaderboard/", "/tell-the-boss/"]
BASE = (sys.argv[1] if len(sys.argv) > 1 else "http://localhost:8000").rstrip("/")

problems = collections.defaultdict(list)
ids_seen = {}
anchors = collections.defaultdict(set)
assets = set()

for path in PAGES:
    try:
        html = urllib.request.urlopen(BASE + path, timeout=20).read().decode("utf8", "replace")
    except Exception as e:
        problems[path].append(f"FETCH FAILED: {e}")
        continue

    # --- duplicate ids on one page ---
    found = re.findall(r'\sid="([^"]+)"', html)
    dupes = [i for i, n in collections.Counter(found).items() if n > 1]
    for d in dupes:
        problems[path].append(f"duplicate id: {d}")
    ids_seen[path] = set(found)

    # --- same-page anchors that point at nothing ---
    for href in re.findall(r'href="#([^"]+)"', html):
        if href and href not in found:
            problems[path].append(f"anchor #{href} has no target on this page")
    for href in re.findall(r'href="(/[^"#]*)#([^"]+)"', html):
        anchors[href[0]].add(href[1])

    # --- unrendered template syntax leaking to the page ---
    for pat, label in [(r"\{\%", "raw {% tag"), (r"\{\{", "raw {{ var"),
                       (r"\{#", "raw {# comment")]:
        if re.search(pat, html):
            problems[path].append(f"{label} rendered as text")

    # --- a section that rendered with no content ---
    for m in re.finditer(r'<section[^>]*class="[^"]*section[^"]*"[^>]*>(.*?)</section>', html, re.S):
        inner = re.sub(r"<[^>]+>", "", m.group(1)).strip()
        if len(inner) < 3:
            problems[path].append("a .section rendered empty")

    # --- collect static assets to check ---
    for src in re.findall(r'(?:src|href)="(/static/[^"]+)"', html):
        assets.add(src)
    for url in re.findall(r"url\('(/static/[^']+)'\)", html):
        assets.add(url)

# --- cross-page anchors: does /#start actually exist on / ? ---
for page, frags in anchors.items():
    target_ids = ids_seen.get(page if page.endswith("/") else page + "/")
    if target_ids is None:
        continue
    for frag in frags:
        if frag not in target_ids:
            problems[page].append(f"cross-page anchor {page}#{frag} has no target")

# --- every static asset referenced must actually serve ---
bad_assets = []
for a in sorted(assets):
    try:
        code = urllib.request.urlopen(BASE + a, timeout=15).getcode()
        if code != 200:
            bad_assets.append((a, code))
    except Exception as e:
        bad_assets.append((a, str(e)[:40]))

print("=" * 66)
if problems:
    for page in PAGES:
        if problems.get(page):
            print(f"\n{page}")
            for p in sorted(set(problems[page])):
                print(f"   - {p}")
else:
    print("no page-level problems")

print(f"\nassets checked: {len(assets)}")
if bad_assets:
    print("BROKEN ASSETS:")
    for a, c in bad_assets:
        print(f"   - {a}  ->  {c}")
else:
    print("every referenced static asset serves 200")
print("=" * 66)
sys.exit(1 if (problems or bad_assets) else 0)
