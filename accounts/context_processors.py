import json

from .onboarding import tour_for_request


def onboarding(request):
    """Hand the current page its walkthrough, if it owes one.

    A context processor rather than something each view returns, because the
    partial is included once in app_base.html and therefore has to work on all
    twenty-seven private pages — including the ones whose views are in other
    apps and have no business knowing this feature exists.

    Cheap on the common path: tour_for_request stops at a dict lookup for any
    page with no tour, and at a list membership test for any tour already put
    away, which between them is nearly every page view the site ever serves.
    """
    tour = tour_for_request(request)
    if tour is None:
        return {}
    return {
        "onboarding_tour_key": tour["key"],
        # Goes into a data- attribute, not into the script body. Django's
        # autoescaping makes an attribute safe for free, where JSON dropped
        # inside <script> needs its own escaping and is one careless edit away
        # from not having it.
        "onboarding_tour_steps": json.dumps(tour["steps"]),
    }
