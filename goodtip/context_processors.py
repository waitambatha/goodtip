"""Template context that is not tied to any one view."""

from django.conf import settings


def analytics(request):
    """Expose the GA measurement id to every template.

    A context processor rather than a hardcoded id in the base template, so
    the tag can be switched off (locally, on staging, for a client demo) by
    changing one environment variable instead of editing markup.
    """
    return {"GA_MEASUREMENT_ID": getattr(settings, "GA_MEASUREMENT_ID", "")}
