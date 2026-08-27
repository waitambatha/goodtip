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
    """
    return {"is_staging": getattr(settings, "IS_STAGING", False)}
