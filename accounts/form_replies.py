"""What a public form says back, and where that wording lives.

CLIENT, 22 SEP 2026: "make sure when all is entered ... it will also say thank
you or something to show whatever they were doing is successful ... and if they
have entered their email, based on which form, make sure we will send an email
saying thank you for what they were trying to do, and have an appropriate
response based on what they were trying to do. If it's contact, have that we
value their enquiry and that we will respond as quick as possible."

ONE FILE, BECAUSE THE SAME WORDS ARE SAID TWICE. Every form answers in two
places — the panel that replaces it on screen, and the acknowledgement that
lands in the person's inbox a second later. Those two saying different things
is the thing somebody notices: the screen promises a quick reply and the email
does not mention one, and now neither is trusted. Keeping both in one dict
means a change to the promise changes both.

WHAT IS DELIBERATELY NOT PROMISED. "As quick as possible" and "within one
business day" are different sentences, and only one of them is a commitment
nobody has made. Where the business has actually committed to a turnaround the
wording says so; where it has not, it says what is true — a person reads every
one — rather than inventing a number the inbox cannot keep. The client asked
for "or something like that, so find the wording", which is the licence to get
this right rather than literal.
"""
from __future__ import annotations

# kind -> what the screen says, and what the email is for.
#
#   title    — the heading on the panel that replaces the form
#   message  — one sentence under it
#   template — templates/emails/<name>.{html,txt}, or None for no acknowledgement
#   subject  — that email's subject line
REPLIES = {
    "enquiry": {
        "title": "Thanks — we've got it.",
        "message": (
            "Your message is with the GoodTip team. A person reads every one of "
            "these, and we'll come back to you as quickly as we can."
        ),
        "template": "enquiry_received",
        "subject": "We've got your message — GoodTip",
    },
    "launch": {
        "title": "You're on the list.",
        "message": (
            "We'll email you the moment GoodTip opens for sign-ups — one email, "
            "when there's actually something to tell you. Nothing before then."
        ),
        "template": "launch_received",
        "subject": "You're on the list — GoodTip",
    },
    "sponsorship": {
        "title": "Application received.",
        "message": (
            "We read every one of these ourselves. We'll be in touch about your "
            "group directly, and nothing you wrote is published anywhere."
        ),
        "template": "sponsorship_received",
        "subject": "We've got your application — GoodTip",
    },
    "boss": {
        "title": "Sent. It's on their desk.",
        "message": (
            "The note is in their inbox with your name on it. Follow it up over "
            "a coffee — that's usually all it takes."
        ),
        "template": None,
        "subject": "",
    },
}


def reply_for(kind: str) -> dict:
    """The wording for a form, falling back to something true for an unknown one."""
    return REPLIES.get(kind, {
        "title": "Thank you.",
        "message": "That came through — we'll be in touch.",
        "template": None,
        "subject": "",
    })


def wants_json(request) -> bool:
    """Whether this submit came from gt-forms.js rather than the browser.

    Checked on the header the script sets, not on Accept alone: some proxies
    and privacy extensions rewrite Accept, and answering JSON to a browser
    doing an ordinary POST would show the visitor a page of raw JSON instead
    of the site. The old redirect path stays the default for that reason.
    """
    return request.headers.get("X-Requested-With") == "XMLHttpRequest"


def json_ok(kind: str):
    from django.http import JsonResponse

    reply = reply_for(kind)
    return JsonResponse({
        "ok": True, "title": reply["title"], "message": reply["message"],
    })


def json_error(message: str):
    from django.http import JsonResponse

    # 200, not 4xx. The request was understood and handled; what failed is the
    # content of the form, which is the response body's business. A 400 here
    # trips error-reporting middleware on what is an ordinary typo.
    return JsonResponse({"ok": False, "error": message})
