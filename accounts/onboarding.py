"""THE PER-PAGE WALKTHROUGHS.

Every private page introduces itself the first time a member opens it, once,
and then never again. This module is the whole registry: a page's tour lives
here as data, and the partial that draws it (templates/partials/onboarding.html)
knows nothing about any particular screen.

WHY IT IS PER PAGE RATHER THAN ONE TOUR AT THE DOOR. The original walkthrough
fired once, on the dashboard, and had to carry the entire product in four
bubbles — so it explained the four things a member would meet soonest and said
nothing about the ten screens behind them. Worse, it was gated on already being
in an organisation, which meant the one moment a new member is most lost — the
empty dashboard telling them to create an organisation or find one — was the
one moment nothing explained itself. Introducing each page when it is first
opened puts the explanation next to the thing it explains, and spends nobody's
attention on screens they have not reached yet.

THE RULES THAT KEEP IT FROM BECOMING A PRODUCT TOUR. The client's brief for the
original still binds all of these: "just enough to stop first-week confusion
getting mistaken for bugs."

- At most four steps a page. If a page needs five things explained, the page
  needs work, not a longer tour.
- Every step points at something already on screen. A step names an element by
  `data-coach`; the partial drops any step whose element is missing or hidden,
  and shows nothing at all if none survive. So a tour is never wrong about a
  page — at worst it is quiet. That is also why a key can be registered here
  before its template has the anchors: it simply does not fire yet.
- Skippable, always, and skipping counts as seen. Nobody is made to sit
  through it twice.

KEYS ARE NOT ROUTES. `SEEN` records the key, so renaming a URL does not
re-show a walkthrough somebody has already finished. Changing a key deliberately
does re-show it, which is the escape hatch when a page changes enough that its
old explanation has become a lie.
"""

# view_name -> tour key. Anything not in here has no walkthrough, which is the
# right default: a page nobody is confused by should stay out of the way.
#
# `dashboard` is the one route with two tours, because it is two screens: the
# empty one that tells a new member to create an organisation or find one, and
# the real one they get afterwards. Somebody who joins an organisation has
# finished the first and has never seen the second, so they must be separate
# keys — see tour_for_request.
PAGE_TOURS = {
    "dashboard": "dashboard",
    "orgs:create": "orgs-create",
    "orgs:search": "orgs-search",
    "orgs:groups": "groups",
    "orgs:members": "members",
    "orgs:invite": "invite",
    "orgs:settings": "org-settings",
    "orgs:charities": "charities",
    "orgs:charity_vote": "charity-vote",
    "orgs:group_charity_vote": "charity-vote",
    "orgs:wall": "wall",
    "tipping:my_tips": "my-tips",
    "tipping:tip_round": "tip-round",
    "tipping:ladder": "ladder",
    "tipping:leaderboard": "leaderboard",
    "tipping:tip_carry": "tip-carry",
    "profile": "profile",
    "billing:plans": "plans",
}

# The dashboard before the member is in anything. Its own key, so joining an
# organisation still earns the real dashboard tour afterwards.
DASHBOARD_EMPTY = "dashboard-start"

# key -> [(data-coach target, title, body), ...]
#
# The copy is here rather than in the templates because a step that finds no
# target is dropped entirely, text and all — the markup never carries wording
# for a bubble that may not be shown.
TOURS = {
    DASHBOARD_EMPTY: [
        ("create-org", "Start your own organisation",
         "This is the one to pick if your workplace or club is not on GoodTip yet. "
         "You name it, choose a sport, and you are the manager."),
        ("find-org", "Or join one that already exists",
         "Search for your workplace or club by name and ask to join. A manager "
         "approves you, and you are in."),
        ("preview-round", "The fixtures are real either way",
         "These are this week's actual matches. Once you are in an organisation "
         "you tip them here and they start counting."),
    ],
    # The original four, unchanged. These are the things a member meets first,
    # and this tour has earned its place — do not extend it.
    "dashboard": [
        ("tips", "Make your tips here",
         "Tap the team you think wins each match, then confirm. You can change any "
         "pick right up until the round locks."),
        ("ladder", "See where you sit",
         "Every tip scores on your ladder. Miss a round and you get the away side "
         "by default, so you are never on nothing."),
        ("wall", "The Wall is your group chat",
         "Banter, results and whatever else. It is per organisation, so only your "
         "lot can see it."),
        ("charity", "This is what it is all for",
         "Your organisation raises for a charity. Where the group votes on it, "
         "this is where you have your say."),
    ],
    "orgs-create": [
        # FIRST, AND THE ONLY ONE GUARANTEED TO SHOW. The wizard renders one
        # step at a time, so the name and sport fields below are usually on a
        # later screen than the one this tour opens on — and the tour runs
        # once, on arrival. The progress rail is on screen at every step, so
        # it is what this can actually count on.
        ("org-steps", "A few short steps",
         "Nothing here is permanent — the wizard keeps every answer, and a step "
         "you have passed is a link back to change it."),
        ("org-name", "Name it the way people say it",
         "This is what members search for and what shows on the leaderboard. Your "
         "workplace or club's usual name is nearly always right."),
        ("org-sport", "Pick the sport you tip",
         "It sets which fixtures your rounds are built from. You can add another "
         "competition later from Settings."),
        ("org-submit", "You become the manager",
         "Creating it makes you the manager: you invite people, approve who joins, "
         "and choose where the money goes."),
    ],
    "orgs-search": [
        ("search-box", "Search by name",
         "Type your workplace or club. Only organisations that allow being found "
         "are listed here."),
        ("search-request", "Asking to join is a request",
         "A manager sees it and approves or declines. You will get an email either "
         "way, so there is nothing to sit and watch."),
    ],
    "groups": [
        ("groups-list", "Groups are the smaller ladders",
         "A big organisation can be thousands of people. A group is the dozen you "
         "actually want to beat — a floor, a team, a shift."),
        ("groups-create", "Anyone can start one",
         "Give it a name and invite your lot. Your tips count on the main ladder "
         "and in the group at the same time."),
    ],
    "members": [
        ("members-list", "Everyone in the organisation",
         "Who has joined, what they are tipping, and who manages. Search it when "
         "the list gets long."),
        ("members-invite", "Add people from here",
         "Invite by email or share a join link. New members land on the current "
         "round, not the start of the season."),
    ],
    "invite": [
        ("invite-link", "One link, share it anywhere",
         "Email, Slack, a poster in the kitchen. Anyone who opens it joins this "
         "organisation directly."),
        ("invite-email", "Or invite by address",
         "Paste in a list and everyone gets their own email. Nobody has to be "
         "told what to search for."),
    ],
    "org-settings": [
        ("settings-comps", "Which competitions you tip",
         "Add a second sport and your members get both sets of rounds. Existing "
         "tips and ladders are untouched."),
        ("settings-groups", "Groups, on or off",
         "Off is the right default for a small organisation. Switch it on when the "
         "single ladder stops being fun."),
    ],
    "charities": [
        ("charity-list", "The charities you can raise for",
         "A vetted list, plus any your organisation has added itself."),
        ("charity-add", "Add your own",
         "The local club or cause your people actually care about. It goes to your "
         "organisation's list, not everyone's."),
    ],
    "charity-vote": [
        ("vote-options", "The charities on the ballot",
         "Read them, then pick the one you want the money to go to."),
        ("vote-cast", "One vote each",
         "You can change it while the vote is open. When it closes the winner takes "
         "the season's fundraising."),
        ("vote-closes", "It closes on its own",
         "No one has to remember to end it. You will be told the result."),
    ],
    "wall": [
        ("wall-composer", "Say something",
         "Results, banter, a photo from the day. It is per organisation, so only "
         "your lot can see it."),
        ("wall-feed", "Everything your organisation has posted",
         "React or reply to any of it. Managers can take a post down if it needs it."),
    ],
    "my-tips": [
        ("tips-rounds", "Every round you have tipped",
         "What you picked, what happened, and what it scored. Rounds you missed "
         "show the away side you were given by default."),
        ("tips-current", "The round in play",
         "Still open? You can still change it. This is the quickest way back to "
         "picks you have not confirmed."),
    ],
    "tip-round": [
        ("round-matches", "Tap the team you think wins",
         "One tap per match. It saves the instant you choose — there is no submit "
         "button and nothing to lose by leaving."),
        ("round-lock", "The round locks at the first bounce",
         "Up to then you can change any pick as often as you like. After it, the "
         "round is final."),
    ],
    "tip-carry": [
        ("carry-choice", "What happens to the rounds you miss",
         "Carry your last picks forward, or take the away side. Either way you are "
         "never on nothing."),
    ],
    "ladder": [
        ("ladder-table", "How the season is going",
         "Wins, losses and where each team sits. This is the competition itself, "
         "not the tipping."),
        ("ladder-series", "One ladder per competition",
         "Tipping two sports? Switch between them here."),
    ],
    "leaderboard": [
        ("board-table", "Who is winning the tipping",
         "Everyone in your organisation, by total score. Yours is highlighted."),
        ("board-scope", "Organisation or group",
         "Switch to your group to see the ladder that actually matters to you."),
    ],
    "profile": [
        ("profile-photo", "Click the photo to change it",
         "It shows next to your name on the leaderboard and on The Wall."),
        ("profile-2fa", "The code at sign-in",
         "Leave it on and every sign-in needs a code from your email. Turn it off "
         "here if you would rather not."),
        ("profile-carry", "What happens to rounds you miss",
         "Carry your last picks forward, take the away side, or be asked each time."),
    ],
    "plans": [
        ("plan-tiers", "What each plan includes",
         "Sized by how many people are in your organisation."),
        ("plan-current", "What you are on now",
         "Changing plan takes effect immediately and is prorated."),
    ],
}


def tour_for_request(request):
    """The walkthrough this page owes this member, or None.

    None covers all the ordinary cases — not signed in, a page with no tour
    registered, a page whose tour this person has already put away — because
    the overwhelmingly common answer on any given page view is "nothing to
    show", and that answer should cost as little as possible.
    """
    user = getattr(request, "user", None)
    if user is None or not user.is_authenticated:
        return None

    match = getattr(request, "resolver_match", None)
    if match is None:
        return None

    key = PAGE_TOURS.get(match.view_name)
    if key is None:
        return None

    # The empty dashboard is a different screen with a different job, so it is
    # a different tour. One EXISTS query, and only on the dashboard route.
    if key == "dashboard" and not user.memberships.exists():
        key = DASHBOARD_EMPTY

    if user.has_seen_tour(key):
        return None

    # A member who finished the old single walkthrough before this existed has
    # had the dashboard's four bubbles already; showing them again as if they
    # were new would be a regression for every existing member on the day this
    # ships. Their other pages are still new to them and still introduce
    # themselves.
    if key == "dashboard" and user.onboarding_seen_at is not None:
        return None

    steps = TOURS.get(key)
    if not steps:
        return None

    return {
        "key": key,
        "steps": [
            {"target": target, "title": title, "body": body}
            for target, title, body in steps
        ],
    }
