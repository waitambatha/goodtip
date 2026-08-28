"""The slot map for the public site.

Every piece of the marketing site the super admin can edit is declared here
once: its key, what to call it in the editor, what kind of thing it is, and
what it says when nobody has edited it.

Three consumers read this file and they all have to agree, which is exactly
why it is one file rather than three:

  * the {% site_text %} / {% site_rich %} / {% site_img %} / {% site_video %}
    tags in the public templates, for the fallback value;
  * the Site content editor in the admin, for the form it renders;
  * a reader trying to work out where a line of copy on the home page comes
    from.

`default` for text and rich blocks is the literal copy. For image and video
blocks it is a path under static/ — so an unedited slot serves the same
version-controlled asset the template always did, and an edited one serves the
upload out of MEDIA_ROOT.

Adding a slot is: add a Block here, then swap the literal in the template for
the matching tag. Nothing else — no migration, no fixture.
"""
from dataclasses import dataclass, field


TEXT, RICH, IMAGE, VIDEO = "text", "rich", "image", "video"


@dataclass(frozen=True)
class Block:
    key: str
    label: str
    kind: str = TEXT
    default: str = ""
    help: str = ""
    # Textarea height for text blocks. 1 renders a single-line input, which is
    # what most of these are — a heading in a 6-row box reads as an invitation
    # to write a paragraph into a slot that has no room for one.
    rows: int = 1


@dataclass(frozen=True)
class Group:
    label: str
    blocks: list
    note: str = ""


@dataclass(frozen=True)
class Page:
    slug: str
    label: str
    url_name: str
    blurb: str
    groups: list = field(default_factory=list)
    icon: str = "ic-doc"

    @property
    def block_count(self):
        return sum(len(g.blocks) for g in self.groups)


# ---------------------------------------------------------------------------
# HOME
# ---------------------------------------------------------------------------
HOME = Page(
    slug="home",
    label="Home",
    url_name="landing",
    icon="ic-home",
    blurb="The front page — hero, the ladder band, how it works, who it's for, "
          "ambassadors, charities, pricing, the Founding Partner window and the FAQ.",
    groups=[
        Group(
            "Hero",
            note="The first screen. The three background photos on each side cross-fade "
                 "in the order they are listed; the last slot on the right can be a video.",
            blocks=[
                Block("home.hero.pill", "Announcement pill",
                      default="Finals start August 28 &middot; Are you in?",
                      help="The small pill above the headline. Leave blank to hide it."),
                Block("home.hero.title", "Headline", RICH,
                      default="Footy tipping <em>that does good.</em>",
                      help="Wrap the emphasised part in italics to get the lime accent."),
                Block("home.hero.sub", "Sub-headline", TEXT, rows=3,
                      default="You tip. GoodTip gives. Your organisation picks the charity, "
                              "and tipping is free for everyone playing."),
                Block("home.hero.cta_ghost", "Secondary button", default="How it works"),
                Block("home.hero.cta_primary", "Primary button",
                      default="Set up your organisation &rarr;"),
                Block("home.hero.cta_note", "Line under the buttons",
                      default="Set up your organisation. Pay when you launch."),
                Block("home.hero.shot1", "Background photo 1 (left)", IMAGE, "img/scenes/mcg-match.jpg"),
                Block("home.hero.shot2", "Background photo 2 (left)", IMAGE, "img/stadium-night.jpg"),
                Block("home.hero.shot3", "Background photo 3 (left)", IMAGE, "img/scenes/stadium-panorama.jpg"),
                Block("home.hero.shot4", "Background photo 4 (right)", IMAGE, "img/scenes/aussie-crowd-flag.jpg"),
                Block("home.hero.shot5", "Background photo 5 (right)", IMAGE, "img/scenes/nrl-posts-sunset.jpg"),
                Block("home.hero.shot6", "Background photo 6 (right)", IMAGE, "img/scenes/afl-posts-mcg.jpg"),
                Block("home.hero.clip", "Background video (right)", VIDEO, "video/match-action.mp4",
                      help="Muted, looping, no sound. Its poster frame is what visitors on a "
                           "metered connection or with reduced motion turned on see instead."),
            ],
        ),
        Group(
            "Scoreboard",
            note="The three-cell strip under the hero buttons.",
            blocks=[
                Block("home.sb.1_value", "Cell 1 — big word", default="FREE"),
                Block("home.sb.1_label", "Cell 1 — caption", default="to play, for every participant"),
                Block("home.sb.2_value", "Cell 2 — number", default="23",
                      help="Digits only; it counts up from zero when the strip scrolls in."),
                Block("home.sb.2_label", "Cell 2 — caption", default="rounds a season"),
                Block("home.sb.3_value", "Cell 3 — number", default="4", help="Digits only."),
                Block("home.sb.3_label", "Cell 3 — caption", default="codes, every season"),
            ],
        ),
        Group(
            "Ladder band",
            blocks=[
                Block("home.ladder.eyebrow", "Eyebrow", default="Live this season"),
                Block("home.ladder.heading", "Heading", RICH,
                      default="The ladder still runs. The total just goes somewhere good."),
                Block("home.ladder.tally_label", "Label above the total",
                      default="Raised so far &middot; this organisation"),
                Block("home.ladder.tally_caption", "Caption under the total", TEXT, rows=3,
                      default="GoodTip funds this from its own revenue. Every dollar lands "
                              "with the charity your organisation picked."),
            ],
        ),
        Group(
            "Comparison",
            blocks=[
                Block("home.compare.heading", "Heading", RICH,
                      default="Every other tipping comp ends the same way."),
                Block("home.compare.sub", "Sub-heading", TEXT, rows=4,
                      default="Someone wins the money. Everyone else waits until next year. "
                              "With GoodTip nobody puts money in at all. Your organisation pays "
                              "one platform fee, and GoodTip gives a share of it to the charity "
                              "your organisation picks."),
                Block("home.compare.them_title", "Left column — title",
                      default="Your current footy tipping"),
                Block("home.compare.us_title", "Right column — title",
                      default="Same comp. Better outcome."),
                Block("home.compare.callout", "Pull quote", RICH,
                      default="Your organisation still tips every round. The leaderboard still "
                              "runs. The result on Saturday still matters. The only difference: "
                              "nobody has to put a cent in for any of it to count. "
                              "<strong>Same ritual. Something good comes out the other end.</strong>"),
            ],
        ),
        Group(
            "How it works",
            blocks=[
                Block("home.how.eyebrow", "Eyebrow", default="How it works"),
                Block("home.how.heading", "Heading", RICH, default="Same game. Different stakes."),
                Block("home.how.sub", "Sub-heading", TEXT, rows=3,
                      default="Three steps. One season. A donation your organisation made "
                              "together, without anyone having to organise a fundraiser."),
                Block("home.how.s1_title", "Step 1 — title",
                      default="Set up your organisation. Run the Charity Vote."),
                Block("home.how.s1_body", "Step 1 — body", TEXT, rows=3,
                      default="Name it. Invite your team. No payment until you're ready to go. "
                              "While your organisation fills, run the Charity Vote."),
                Block("home.how.s2_title", "Step 2 — title", default="Tip and track."),
                Block("home.how.s2_body", "Step 2 — body", TEXT, rows=3,
                      default="AFL. AFLW. NRL. NRLW. Weekly tips. Live leaderboard. Running "
                              "charity total. State of Origin tips worth four points. Finals worth two."),
                Block("home.how.s3_title", "Step 3 — title", default="Season done well."),
                Block("home.how.s3_body", "Step 3 — body", TEXT, rows=3,
                      default="Last grand final done. Impact Report goes out. Total raised. "
                              "Charity named. Tipping results. Season done well."),
            ],
        ),
        Group(
            "Who it's for",
            blocks=[
                Block("home.for.eyebrow", "Eyebrow", default="Who it's for"),
                Block("home.for.heading", "Heading", RICH,
                      default="Your community already tips. Now it does something with it."),
                Block("home.for.sub", "Sub-heading", TEXT, rows=3,
                      default="Tipping comps are the best community engagement tool in Australian "
                              "sport. GoodTip keeps all of that and adds one thing: somewhere "
                              "worth going when the season ends."),
                Block("home.for.c1_tag", "Card 1 — tag", default="Workplaces"),
                Block("home.for.c1_title", "Card 1 — title",
                      default="A culture tool your team actually enjoys."),
                Block("home.for.c1_body", "Card 1 — body", TEXT, rows=5,
                      default="Your team tips every round, free. GoodTip gives to the charity they "
                              "voted for. At season end you get an Impact Report with a real number "
                              "your board can cite. It sits in culture budgets. It belongs in ESG "
                              "reporting. And people actually look forward to it."),
                Block("home.for.c2_tag", "Card 2 — tag", default="Sporting clubs"),
                Block("home.for.c2_title", "Card 2 — title",
                      default="Run a season-long comp for members."),
                Block("home.for.c2_body", "Card 2 — body", TEXT, rows=5,
                      default="Your members already tip. GoodTip gives it somewhere to land. Your "
                              "club picks the cause and GoodTip gives to it. The leaderboard runs "
                              "all season. The giving is real at the end of it."),
                Block("home.for.c3_tag", "Card 3 — tag", default="Community organisations"),
                Block("home.for.c3_title", "Card 3 — title",
                      default="Bring people together around the footy."),
                Block("home.for.c3_body", "Card 3 — body", TEXT, rows=5,
                      default="Tipping already happens. GoodTip makes it count. Your community votes "
                              "for the charity, tips the games, and raises real money for a cause "
                              "they chose themselves. Something worth coming back for every round."),
            ],
        ),
        Group(
            "Image band",
            blocks=[
                Block("home.band.shot1", "Photo 1", IMAGE, "img/nrlw-players.jpg"),
                Block("home.band.shot2", "Photo 2", IMAGE, "img/scenes/stadium-lights-grass.jpg"),
                Block("home.band.shot3", "Photo 3", IMAGE, "img/scenes/aussie-crowd-flag.jpg"),
                Block("home.band.eyebrow", "Eyebrow", default="Women's sport included, always"),
                Block("home.band.quote", "Quote", RICH,
                      default="AFLW comes with AFL. NRLW comes with NRL. Same price, same season."),
                Block("home.band.credit", "Photo credit",
                      default="Sydney Roosters NRLW &middot; Allianz Stadium"),
            ],
        ),
        Group(
            "Quote break",
            blocks=[
                Block("home.quote.body", "Quote", RICH,
                      default="Your organisation picks the charity. <em>Then tips for it all season.</em>"),
                Block("home.quote.attr", "Line underneath",
                      default="That's the bit that changes everything."),
            ],
        ),
        Group(
            "The Wall",
            blocks=[
                Block("home.wall.eyebrow", "Eyebrow", default="New for 2026 &middot; The Wall"),
                Block("home.wall.heading", "Heading", RICH,
                      default="The banter is half the reason people tip. <em>Now it has a home.</em>"),
                Block("home.wall.line1", "Rotating line 1", TEXT, rows=2,
                      default="Every round, the Wall is where the organisation lives: tips, sledges, "
                              "and the leaderboard shuffle, in real time."),
                Block("home.wall.line2", "Rotating line 2", TEXT, rows=2,
                      default="Big Dave just backed the Pies by 30+ and told the whole room to "
                              "screenshot it. That's the Wall."),
                Block("home.wall.line3", "Rotating line 3", TEXT, rows=2,
                      default="Monday's ladder screenshot posts itself now, and the room does the rest."),
                Block("home.wall.line4", "Rotating line 4", TEXT, rows=2,
                      default="The quiet one in Legal tipped 12 from 12 and said nothing. The Wall "
                              "said it for them."),
                Block("home.wall.line5", "Rotating line 5", TEXT, rows=2,
                      default="It's the bit ESPN FootyTips never built, and the reason your "
                              "organisation keeps showing up every round."),
                Block("home.wall.cta", "Button", default="See the Wall &rarr;"),
            ],
        ),
        Group(
            "Supporters",
            blocks=[
                Block("home.amb.eyebrow", "Eyebrow", default="Supporters"),
                Block("home.amb.heading", "Heading", RICH, default="People who get it."),
                Block("home.amb.sub", "Sub-heading", TEXT, rows=3,
                      default="People who back what we're doing — on the field, in the rooms, and "
                              "everywhere else. You don't need a title to be one of them."),
                Block("home.amb.a1_photo", "Supporter 1 — portrait", IMAGE, "img/nick-maxwell.jpg"),
                Block("home.amb.a1_quote", "Supporter 1 — quote", TEXT, rows=3,
                      default="Same ritual the rooms have always had. Now it leaves something behind."),
                Block("home.amb.a1_name", "Supporter 1 — name", default="Nick Maxwell"),
                Block("home.amb.a1_role", "Supporter 1 — role", default="Premiership Captain"),
                Block("home.amb.a2_quote", "Supporter 2 — quote", TEXT, rows=3,
                      default="The women's game deserves the same Saturday energy, and a cause behind it."),
                Block("home.amb.a2_name", "Supporter 2 — name", default="Announcing soon"),
                Block("home.amb.a2_role", "Supporter 2 — role", default="AFLW Supporter"),
                Block("home.amb.a3_quote", "Supporter 3 — quote", TEXT, rows=3,
                      default="Footy brings the room together. This gives the room something to play for."),
                Block("home.amb.a3_name", "Supporter 3 — name", default="Announcing soon"),
                Block("home.amb.a3_role", "Supporter 3 — role", default="NRL &amp; NRLW Supporter"),
            ],
        ),
        Group(
            "Charity partners",
            blocks=[
                Block("home.char.eyebrow", "Eyebrow", default="Charity partners"),
                Block("home.char.heading", "Heading", RICH,
                      default="Supporting the causes closest to Australian hearts."),
                Block("home.char.sub", "Sub-heading", TEXT, rows=4,
                      default="Your organisation votes on which charity receives the season's "
                              "donations. Choose from our donation pool, or pair with a hands-on "
                              "activity partner for something your whole organisation can do together."),
                Block("home.char.note", "Line underneath", RICH,
                      default='Want your charity in the pool? <a href="/#start">Get in touch.</a>'),
            ],
        ),
        Group(
            "Pricing teaser",
            note="The prices themselves. Changing one here changes the home page only — "
                 "the full pricing page has its own slots.",
            blocks=[
                Block("home.price.eyebrow", "Eyebrow", default="Pricing"),
                Block("home.price.heading", "Heading", RICH,
                      default="One flat fee. All year. All four codes."),
                Block("home.price.sub", "Sub-heading", TEXT, rows=3,
                      default="Priced by team size, nothing else. Tipping is free for your people, "
                              "and GoodTip funds a donation to the charity your organisation picked."),
                Block("home.price.t1_num", "Starter — price", default="$99"),
                Block("home.price.t2_num", "Team — price", default="$299"),
                Block("home.price.t3_num", "Workplace — price", default="$799"),
                Block("home.price.t4_num", "Organisation — price", default="$1,299"),
                Block("home.price.footnote", "Footnote", RICH,
                      default='2,500+ people? <a href="/pricing/#enterprise">Let\'s talk Enterprise.</a> '
                              '&middot; Founding Partner pricing closes December 31. '
                              '<a href="/#founding">Learn more.</a>'),
            ],
        ),
        Group(
            "How the donation works",
            blocks=[
                Block("home.give.eyebrow", "Eyebrow", default="How the donation works"),
                Block("home.give.heading", "Heading", RICH,
                      default="One fee. <em>That's the whole story.</em>"),
                Block("home.give.sub", "Sub-heading", TEXT, rows=3,
                      default="Your organisation pays one flat fee. GoodTip funds a donation from "
                              "its own revenue and sends it to the charity your organisation picked."),
            ],
        ),
        Group(
            "Founding Partner band",
            blocks=[
                Block("home.found.badge", "Badge", default="Founding Partner Program"),
                Block("home.found.heading", "Heading", RICH,
                      default="In before the finals. <em>In for life.</em>"),
                Block("home.found.lead", "Lead paragraph", TEXT, rows=4,
                      default="Founding Partners lock their pricing permanently, not for year one, "
                              "for good. Plus first access to new sports and features in 2027, and "
                              "your name on the list of organisations that backed it before it was "
                              "obvious."),
                Block("home.found.b1", "Benefit 1", default="Pricing locked permanently, never increases"),
                Block("home.found.b2", "Benefit 2", default="First access to every new sport and feature in 2027"),
                Block("home.found.b3", "Benefit 3", default="Founding Partner recognition across the platform"),
                Block("home.found.deadline_label", "Deadline label", default="Window closes"),
                Block("home.found.deadline", "Deadline", default="Dec 31, 2026"),
                Block("home.found.deadline_sub", "Deadline caption", TEXT, rows=3,
                      default="After that, standard rates. No exceptions, no late extensions. Set up "
                              "your organisation free today and pay only when you launch."),
                Block("home.found.cta", "Button", default="Set up your organisation &rarr;"),
            ],
        ),
        Group(
            "FAQ",
            blocks=[
                Block("home.faq.heading", "Heading", RICH, default="Fair questions."),
                Block("home.faq.sub", "Sub-heading", TEXT, rows=2,
                      default="The honest answers, up front. Anything else, just ask."),
                Block("home.faq.q1", "Question 1", default="How is this different from a normal tipping comp?"),
                Block("home.faq.a1", "Answer 1", RICH,
                      default="Your organisation tips for free, every round. Your organisation pays "
                              "one platform fee, and GoodTip gives a share of it to the charity your "
                              "organisation picked."),
                Block("home.faq.q2", "Question 2", default="How does GoodTip make money?"),
                Block("home.faq.a2", "Answer 2", RICH,
                      default="Your organisation pays one platform fee. GoodTip gives a share of that "
                              "fee to the charity your organisation picks, from its own revenue. "
                              "Tipping is free for every participant. It always will be."),
                Block("home.faq.q3", "Question 3", default="Who picks the charity?"),
                Block("home.faq.a3", "Answer 3", RICH,
                      default="Your organisation does. Everyone nominates. Everyone votes. Majority "
                              "wins. That's where the money goes."),
                Block("home.faq.q4", "Question 4", default="Which sports are included?"),
                Block("home.faq.a4", "Answer 4", RICH,
                      default="AFL, AFLW, NRL, and NRLW at launch. Four codes, all season. Women's "
                              "competitions come with the men's, same price either way. More in 2027."),
                Block("home.faq.q5", "Question 5", default="Where does the donation come from?"),
                Block("home.faq.a5", "Answer 5", RICH,
                      default="GoodTip funds it from its own revenue. Your organisation pays one flat "
                              "fee to run the comp; a share of that goes to the charity your "
                              "organisation picked. Participants are never asked for money."),
                Block("home.faq.q6", "Question 6", default="When can we start?"),
                Block("home.faq.a6", "Answer 6", RICH,
                      default="Set up your organisation now, free. Pay when you launch. Finals start "
                              "August 28."),
            ],
        ),
    ],
)

PAGES = [HOME]

# key -> Block, built once at import. The tags hit this on every render.
BLOCKS = {b.key: b for page in PAGES for group in page.groups for b in group.blocks}


def page_by_slug(slug):
    for page in PAGES:
        if page.slug == slug:
            return page
    return None
