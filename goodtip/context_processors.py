"""Template context that is not tied to any one view."""

from django.conf import settings


def analytics(request):
    """Expose the GA measurement id to every template.

    A context processor rather than a hardcoded id in the base template, so
    the tag can be switched off (locally, on staging, for a client demo) by
    changing one environment variable instead of editing markup.
    """
    return {"GA_MEASUREMENT_ID": getattr(settings, "GA_MEASUREMENT_ID", "")}


def environment(request):
    """Expose which environment this is, for chrome that has to say so.

    The admin wears it as a tag beside the wordmark. On staging that badge is
    the difference between "I am testing something" and "I have just edited
    the live database" — the admin's own chrome is identical in both, and the
    two checkouts are told apart by working directory rather than by anything
    a human sees on screen.

    ``is_holding`` is the other thing every public template may need to ask:
    whether THIS visitor is the one lockdown or holding mode is standing in
    front of the real site for (goodtip.staging_gate.is_holding_visitor). Only
    a handful of pages are ever let through to render at all while that is
    true (About, Terms, Privacy, Pricing, the blog — see staging_gate.py's
    HOLDING_REAL_PAGES), and when one is, it wears the stripped nav and
    footer rather than the full site map (client, 12 Oct 2026: "remove any
    other type of menu... just see the blogs").
    """
    from .staging_gate import is_holding_visitor

    return {
        "is_staging": getattr(settings, "IS_STAGING", False),
        "is_holding": is_holding_visitor(request),
    }
