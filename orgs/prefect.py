"""Prefect — the thing that reads the rooms so nobody has to read all of them.

WHAT IT IS FOR
--------------
Asked for by the client: "the user wants that inappropriate content like porn,
abuses, things like that are flagged, and the prefect reports it to the
organisation admin or captain... so the captain will see the content — it will
come as an inbox but a clickable link to check, review and flag it as not an
issue, be able to learn that that is not an issue, because sometimes friends
talk like 'what's up you crazy fool', their friendship is at that level, but
when you read it plainly you might think it's an abuse. So the prefect has to be
smart."

That last sentence is the whole design brief and it is a warning as much as a
requirement. A moderator that cannot tell banter from abuse does not fail
quietly — it accuses people of things in front of the people who run their
workplace comp. So the rules here are deliberately conservative, everything it
raises goes to a human before anything happens to anybody, and the ONE thing it
is allowed to learn on its own is that it was wrong.

THE SEAM
--------
`classify()` resolves its implementation through `get_classifier()`, which reads
settings.PREFECT_CLASSIFIER. Today that is the keyword classifier below. It is
written to be replaced by a language model — which is the only honest way to
read tone — and the swap is one setting plus a class with a `.look(text, ctx)`
method. Nothing else in the product imports the implementation directly.

WHY A KEYWORD CLASSIFIER IS STILL WORTH SHIPPING. The words that carry the
worst of it are a short list and they are not ambiguous; the ambiguity is in
tone, which is the part a model is for. So this catches the unmistakable, defers
on the rest, and never raises anything on a bare insult alone if the room has
already told it that phrase is fine here.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field

from django.conf import settings
from django.utils.module_loading import import_string

#: Everything below this is not reported at all. A moderator that flags on a
#: hunch trains people to ignore it, which is worse than one that misses things:
#: the misses are invisible and the noise is not.
REPORT_AT = 60


@dataclass
class Verdict:
    """What Prefect thinks, and why. `terms` is what a reviewer is shown.

    `score` is 0-100 and is NOT a probability — it is a rank, so a reviewer's
    queue can put the worst first. Naming it a confidence would invite somebody
    to act on it without reading, which is exactly what this must not become.
    """
    category: str = ""
    score: int = 0
    terms: list[str] = field(default_factory=list)
    reason: str = ""

    @property
    def reportable(self) -> bool:
        return self.score >= REPORT_AT


# ---------------------------------------------------------------------------
# The word lists.
#
# Split by category rather than pooled, because what a reviewer should do about
# each is different and the queue says which is which. Deliberately short: every
# entry here is a word whose presence is worth a human glance in a workplace
# tipping league, and the temptation to add "anything rude" is the temptation to
# make this useless.
#
# Stored as stems and matched on word boundaries, so "classic" is not "ass" and
# "Scunthorpe" is nobody's problem.
# ---------------------------------------------------------------------------
LISTS: dict[str, tuple[int, tuple[str, ...]]] = {
    # Sexual content posted into a workplace room. The highest weight, because
    # it is the one category where context almost never makes it acceptable.
    "sexual": (95, (
        "porn", "pornhub", "onlyfans", "xxx", "nudes", "nsfw",
        "blowjob", "handjob", "cum", "dick pic", "tits",
    )),
    # Slurs. Weighted at the top and listed by stem; a reviewer sees the term
    # that matched, so nothing is hidden behind a score.
    "hate": (100, (
        "faggot", "fag", "tranny", "nigger", "nigga", "paki", "abo",
        "coon", "retard", "spastic", "wog",
    )),
    # Threats. The words are ordinary; what makes them a threat is that they are
    # pointed at somebody, which is why this category is the one that leans
    # hardest on the "aimed at a person" test below.
    "threat": (85, (
        "kill you", "bash you", "smash your head", "find you and",
        "watch your back", "you're dead", "youre dead", "i'll hurt",
    )),
    # Ordinary abuse. The LOWEST weight on purpose: this is the category the
    # client's example lives in — "what's up you crazy fool" — and on its own it
    # does not reach the reporting floor. It only gets there with something else.
    "abuse": (45, (
        "fuck you", "fuck off", "piss off", "shut up", "idiot", "moron",
        "stupid", "dumb", "loser", "pathetic", "useless", "clown", "fool",
        "crazy",
    )),
}

#: Marks of the banter the client described. None of them makes anything safe on
#: its own; each one takes weight off, because a sentence carrying a laugh, a
#: nickname or an emoji is a sentence being said to a friend far more often than
#: it is an attack.
SOFTENERS = (
    "lol", "haha", "hahaha", "lmao", "😂", "🤣", "😅", "😜", "😉", "🙃", "❤",
    "mate", "buddy", "champ", "legend", "brother", "bro", "sis",
    "jk", "joking", "kidding", "love you", "no offence", "no offense",
)

#: Aimed at a person. "This is stupid" is an opinion about a fixture; "you are
#: stupid" is aimed at somebody, and only the second is Prefect's business.
AIMED = re.compile(
    r"\b(you|your|you're|youre|u|ur|he|she|they|him|her|them)\b", re.I,
)


def _words(text: str) -> str:
    return re.sub(r"\s+", " ", (text or "").lower())


class KeywordPrefect:
    """The default reader. Rules, weights, and a bias toward saying nothing.

    Not a model and not pretending to be one. What it is good at is the
    unmistakable — a slur, a porn link — and what it is bad at is tone, which is
    why the abuse list alone cannot reach the reporting floor and why every
    softener takes weight off rather than being ignored.
    """

    def look(self, text: str, ctx: dict | None = None) -> Verdict:
        body = _words(text)
        if not body:
            return Verdict()

        ctx = ctx or {}
        allowed = {p.lower() for p in ctx.get("allowed_phrases", ())}

        hits: list[tuple[str, int, str]] = []
        for category, (weight, terms) in LISTS.items():
            for term in terms:
                if term in allowed:
                    # The room has already told a human reviewer this is fine
                    # here. See PrefectAllowance: this is the one thing Prefect
                    # learns, and it only ever learns to say less.
                    continue
                pattern = r"\b" + re.escape(term).replace(r"\ ", r"\s+") + r"\b"
                if re.search(pattern, body):
                    hits.append((category, weight, term))
        if not hits:
            return Verdict()

        category, score, _ = max(hits, key=lambda h: h[1])
        terms = sorted({t for _, _, t in hits})

        # A second category is a second signal — "stupid" beside a slur is not
        # the same message as "stupid" alone.
        if len({c for c, _, _ in hits}) > 1:
            score = min(100, score + 10)

        reasons = []
        # CONTEXT ONLY MOVES THE CATEGORIES CONTEXT CAN EXPLAIN.
        #
        # Both discounts below were applied to everything, and the tests caught
        # what that means: "haha you faggot lol 😂" fell under the reporting
        # floor because three laughing marks took 45 points off a slur, and
        # "check this out pornhub.com/watch" fell under it because the sentence
        # was not pointed at a person.
        #
        # Both are wrong, and wrong in the direction that matters. Whether a
        # slur was funny is not the question; a pornographic link addressed to
        # nobody is still in the room. Tone explains an INSULT — which is the
        # client's own example — and it explains nothing about the other two.
        SOFTENABLE = {"abuse", "threat"}
        AIMABLE = {"abuse", "threat"}

        if category in AIMABLE and not AIMED.search(body):
            # "That fixture list is stupid" is an opinion about a fixture.
            score = int(score * 0.5)
            reasons.append("not aimed at anyone")
        softeners = [s for s in SOFTENERS if s in body]
        if softeners and category in SOFTENABLE:
            # Each mark of banter takes a slice off, to a floor. This is the
            # rule the client asked for in as many words — "their friendship is
            # at that level" — and it is kept away from the categories where a
            # laugh is not a defence.
            score = max(int(score * 0.55), score - 18 * len(softeners))
            reasons.append(f"reads as banter ({', '.join(softeners[:3])})")
        elif softeners:
            # Recorded, not applied. A reviewer should still see that the room
            # was laughing; they are allowed to weigh what Prefect may not.
            reasons.append(
                f"laughing ({', '.join(softeners[:3])}), which does not excuse a {category} term"
            )
        if len(body) > 400:
            # A long message with one bad word in it is usually a long message.
            score = int(score * 0.85)
            reasons.append("one word in a long message")

        return Verdict(
            category=category, score=max(0, min(100, score)), terms=terms,
            reason="; ".join(reasons),
        )


def get_classifier():
    """The seam. One setting, one class, a `.look(text, ctx)` method.

    settings.PREFECT_CLASSIFIER is a dotted path. It is the keyword reader here
    and it is meant to become a language model, which is the only honest way to
    read tone — the client's "the prefect has to be smart" cannot be satisfied
    by a word list and this module does not pretend otherwise.
    """
    path = getattr(settings, "PREFECT_CLASSIFIER", "")
    if path:
        return import_string(path)()
    return KeywordPrefect()


def classify(text: str, ctx: dict | None = None) -> Verdict:
    """Read one message. Never raises: a moderator that can break a chat is
    worse than no moderator, so a classifier that throws is treated as having
    seen nothing and the message goes through."""
    try:
        return get_classifier().look(text, ctx)
    except Exception:  # noqa: BLE001 - deliberately swallowed, see docstring
        import logging

        logging.getLogger(__name__).exception("Prefect could not read a message")
        return Verdict()
