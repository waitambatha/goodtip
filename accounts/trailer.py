"""The chapters of the holding-page trailer.

One entry per screen recording in static/video/trailer/ (made by
scripts/trailer/film.py against a throwaway database of invented people).
They live here rather than in the template so the page, its tests and the
recordings cannot drift apart: a chapter without a file is caught by a test,
not by a visitor's broken video.

`clip` is the file stem, `url` is what the browser-frame address bar shows,
`title` is the kinetic line and `blurb` the one sentence under it.
"""

TRAILER_CHAPTERS = [
    # ---- Act I: join ----
    {"act": "Act I · Get in", "clip": "home", "url": "goodtip.com.au",
     "title": "The front door.", "blurb": "Footy tipping that does good. Your organisation plays, GoodTip gives.",
     "tag": "Home"},
    {"act": "Act I · Get in", "clip": "signup", "url": "goodtip.com.au/signup",
     "title": "Sign up in a minute.", "blurb": "A name, an email, a code from your inbox. That is the whole queue.",
     "tag": "Sign up"},
    {"act": "Act I · Get in", "clip": "signin", "url": "goodtip.com.au/login",
     "title": "Two steps, then you are in.", "blurb": "Password, then a one-time code. Your comp is exactly where you left it.",
     "tag": "Sign in"},
    {"act": "Act I · Get in", "clip": "create-org", "url": "goodtip.com.au/leagues/new",
     "title": "Start your own organisation.", "blurb": "Informal for mates, formal for a club or workplace. Pick one and the questions change to fit. Saved as you go.",
     "tag": "Create an organisation"},
    {"act": "Act I · Get in", "clip": "create-group", "url": "goodtip.com.au/leagues/groups",
     "title": "Split into teams.", "blurb": "Departments, junior sides, the social crew. Every group gets its own ladder and its own wall.",
     "tag": "Create a group"},
    # ---- Act II: play ----
    {"act": "Act II · Play", "clip": "fixtures", "url": "goodtip.com.au/dashboard",
     "title": "Every fixture. Four codes.", "blurb": "Pick your winners in AFL, AFLW, NRL and NRLW, each with its own round, then see them all in My Tips.",
     "tag": "Fixtures"},
    {"act": "Act II · Play", "clip": "matchreader", "url": "goodtip.com.au/dashboard",
     "title": "Read the match first.", "blurb": "MatchReader looks into the past, form, home ground and head to head, before you back a side.",
     "tag": "MatchReader"},
    {"act": "Act II · Play", "clip": "picks", "url": "goodtip.com.au/dashboard",
     "title": "Tap a team. Confirm. Done.", "blurb": "Pick your winners, review them once, and carry them into every group you tip in.",
     "tag": "Pick your matches"},
    {"act": "Act II · Play", "clip": "my-tips", "url": "goodtip.com.au/tips",
     "title": "Your tips. Your results.", "blurb": "Every pick, marked as it lands, with your rank and your season so far.",
     "tag": "My tips and results"},
    {"act": "Act II · Play", "clip": "ladder", "url": "goodtip.com.au/ladder",
     "title": "The ladder never sleeps.", "blurb": "The real club ladder for every code, with statistics for the whole competition and for any one club.",
     "tag": "The ladder"},
    {"act": "Act II · Play", "clip": "leaderboard", "url": "goodtip.com.au/leaderboard",
     "title": "Climb the leaderboard.", "blurb": "Gold, silver, bronze and everybody chasing them, plus your own stats, round by round.",
     "tag": "The leaderboard"},
    # ---- Act III: together ----
    {"act": "Act III · Together", "clip": "wall", "url": "goodtip.com.au/wall",
     "title": "Where the room talks footy.", "blurb": "Post, react, reply. Share a pick, or share it with the world on the public Wall.",
     "tag": "The Wall"},
    {"act": "Act III · Together", "clip": "messages", "url": "goodtip.com.au/messages",
     "title": "Message the whole room.", "blurb": "The organisation, a group or one mate. Chat, photos and voice notes in one place.",
     "tag": "Messaging"},
    {"act": "Act III · Together", "clip": "charity-vote", "url": "goodtip.com.au/charity-vote",
     "title": "Vote where the money goes.", "blurb": "A blind vote inside your organisation. Every member gets a say, and GoodTip gives.",
     "tag": "The charity vote"},
]

# The clips the hero cycles through on its video side, by clip name.
TRAILER_HERO_CLIPS = ["picks", "ladder", "wall", "charity-vote"]

# The menu and footer items. On the holding page none of them leaves the page:
# each opens a showcase (a filmed tour of the real page and three stills of it)
# so a visitor sees what the page is without being able to click around inside
# it. `slug` is the address hash (`/coming-soon/#how-it-works`); `clip` is the
# stem of the tour in static/video/trailer/; `shots` are that clip's stills,
# `<clip>-<n>.jpg`, with what each one shows; `real` is the page itself, which
# only the team (who hold the gate cookie) are offered a link to.
TRAILER_PEEKS = [
    {"slug": "how-it-works", "clip": "how-it-works", "label": "How It Works", "real": "/how-it-works/",
     "url": "goodtip.com.au/how-it-works", "eyebrow": "The season, end to end",
     "title": "You tip. GoodTip gives.",
     "blurb": "Three moves take an organisation from sign-up to a season of tipping that raises money for a cause it chose.",
     "points": ["Set up your organisation and run the charity vote",
                "Tip and track every round, with scoring that means something",
                "Two roles, one season, and a leaderboard nobody has seen before"],
     "shots": [(1, "Where the page starts"), (2, "Three moves, one season"), (3, "Points that mean something")]},
    {"slug": "blog", "clip": "news", "label": "Blog", "real": "/news/",
     "url": "goodtip.com.au/news", "eyebrow": "Around the grounds",
     "title": "News that moves the comp.",
     "blurb": "Stories, previews and updates from the GoodTip team, across AFL, AFLW, NRL and NRLW.",
     "points": ["Filter by code in one tap",
                "Every story wears its code's colour",
                "Julie's Tips puts an everyday tipper in the spotlight"],
     "shots": [(1, "The news front page"), (2, "What's moving this week"), (3, "Stories from GoodTip organisations")]},
    {"slug": "wall", "clip": "wall-public", "label": "The Wall", "real": "/wall/",
     "url": "goodtip.com.au/wall", "eyebrow": "The public Wall",
     "title": "Every room, one feed.",
     "blurb": "A live feed of what tipping organisations choose to share with the world. Read it, react, jump in.",
     "points": ["Members choose, post by post, what goes public",
                "Replies from outside go up after a quick check",
                "Your own organisation's Wall stays yours"],
     "shots": [(1, "The public Wall"), (2, "One feed, every room"), (3, "Why the Wall matters")]},
    {"slug": "about", "clip": "about", "label": "About Us", "real": "/about/",
     "url": "goodtip.com.au/about", "eyebrow": "Why we built it",
     "title": "A comp with a cause.",
     "blurb": "A tipping comp is already a community. GoodTip just gives it something to give to.",
     "points": ["Three things we will not trade away",
                "Nobody in the comp puts a dollar in",
                "Straight answers before anyone signs up"],
     "shots": [(1, "Why we built it"), (2, "What we stand on"), (3, "Straight answers")]},
    {"slug": "terms", "clip": "terms", "label": "Terms & Conditions", "real": "/terms/",
     "url": "goodtip.com.au/terms", "eyebrow": "The short version",
     "title": "Terms, in plain words.",
     "blurb": "A short version up top, then every section numbered so you can find the bit you want.",
     "points": ["Three things worth knowing first",
                "No stake and no prize pool",
                "How the donation and the platform fee work"],
     "shots": [(1, "Terms of Use"), (2, "The short version"), (3, "How the donation works")]},
    {"slug": "privacy", "clip": "privacy", "label": "Privacy Policy", "real": "/privacy/",
     "url": "goodtip.com.au/privacy", "eyebrow": "Your information",
     "title": "Privacy, in plain words.",
     "blurb": "What we collect, why we collect it and who can see it, one numbered section at a time.",
     "points": ["What we collect, and why",
                "Who can see what",
                "Your choices, and how to make them"],
     "shots": [(1, "Privacy Policy"), (2, "What we collect"), (3, "Your choices")]},
]

# Fill in what the template would otherwise have to work out: each still's file
# stem, and the neighbours for the previous / next links (they wrap round).
for _i, _p in enumerate(TRAILER_PEEKS):
    _p["shots"] = [{"n": n, "cap": cap, "file": f"{_p['clip']}-{n}"} for n, cap in _p["shots"]]
    _p["prev"] = TRAILER_PEEKS[_i - 1]["slug"]
    _p["next"] = TRAILER_PEEKS[(_i + 1) % len(TRAILER_PEEKS)]["slug"]
