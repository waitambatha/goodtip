"""The Terms and Privacy Policy as the product refers to them.

WHY A MODULE AND NOT A CONSTANT IN A TEMPLATE. Three places need to agree
about the same four facts — the Terms page itself, the checkbox on the last
step of the org wizard, the summary printed next to that checkbox, and the
version stamped onto Organisation.terms_accepted_at when somebody ticks it. If
the wording of the summary and the version string can drift apart, then every
acceptance recorded between the drift and somebody noticing is an acceptance of
nothing identifiable.

SIGNED OFF 22 SEP 2026. The NFP/gaming review is complete and the client has
approved both documents, so the draft banner is off /terms/ and the third
summary below was reworded as part of it. What is here still describes what the
system actually does, which is what made it reviewable. Bump TERMS_VERSION
whenever the wording changes, so acceptances either side of the change can be
told apart — this one moved from 2026-09-20 to 2026-09-22 for exactly that.
"""
from __future__ import annotations

# Dated, not numbered. "v2" tells a support person nothing; "2026-09-17" tells
# them which draft an organisation agreed to without opening anything.
TERMS_VERSION = "2026-09-22"

# THE PLAIN-ENGLISH TOP THREE, in the client's words: "a plain-English top-3
# summary of what they're agreeing to".
#
# Three, and only three, because the whole point is that it gets read. The
# three chosen are the ones that would surprise somebody who did not read the
# full document — what the money does, what they personally are taking on by
# being the one who set this up, and that this is not a wagering product.
# Anything a reasonable person would already assume is left to the Terms.
#
# CLIENT, 17 SEP 2026: "with the Terms and Conditions for the Organiser — who
# runs the management — they are responsible for the culture in the team and we
# need to simply tell them they are in charge of the behaviour as part of being
# the Admin/Manager/Captain."
#
# It went into the SECOND of the three rather than becoming a fourth, and that
# is the deliberate part. A list of four is a list people skim; three is the
# number that gets read, which is the only reason any of this is here instead
# of inside the Terms. It also belongs there on the merits: "you can see your
# members' addresses" and "you are answerable for how they behave" are two
# halves of the same sentence — they are both what being the admin costs, as
# against what it gives you. Clause 06 of /terms/ spells the same thing out at
# length.
#
# Each is (heading, body). Kept as data so the wizard and the Terms page print
# the identical words.
TERMS_TOP_THREE = [
    (
        "Your organisation pays one fee. Nobody playing pays anything.",
        "One platform fee per season, priced by how many people are in it, and "
        "GoodTip gives a share of that fee to the charity your organisation "
        "picks. Nobody in your comp is ever asked to contribute, and GoodTip "
        "never collects money from them.",
    ),
    (
        "You're in charge of your people, and of their details.",
        "As the Admin, Manager or Captain you set the tone of your comp and you "
        "are responsible for how your group behaves in it — the Wall, the chat, "
        "all of it. You can also see your members' email addresses and manage "
        "their membership, and you agree to use that only for running this comp, "
        "and to have a reasonable basis for adding the people you add — an "
        "invite they expect, not a list you exported from somewhere.",
    ),
    (
        "It's a tipping comp, not a wagering one.",
        # REWORDED AT SIGN-OFF, 22 Sep 2026. The old wording made the organiser
        # agree not to run a side pot "on the back of" GoodTip — a promise about
        # their private conduct that GoodTip can neither see nor enforce. The
        # client's position is narrower and truer: nothing of that kind through
        # the platform, nothing wearing GoodTip's name, and what a group does
        # privately is on them. §3 of the Terms says the same at length.
        "There is no entry fee, no stake, no prize pool and no cash prize. The "
        "ladder is for bragging rights. You agree not to run a side pot, a "
        "buy-in or any betting arrangement through GoodTip, or to attach one to "
        "GoodTip's name. What your group does privately is your own affair — we "
        "would rather you didn't, and we ask you to keep it away from here.",
    ),
]
