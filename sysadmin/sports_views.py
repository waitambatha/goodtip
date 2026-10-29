"""SPORTS & COMPETITIONS (client, 27 Oct 2026: "add the top 10 sports... the
admin should be able to say turn off this and turn on this like golf, then if
it's turned on we see it").

One screen: every sport as a card with its own switch, and inside it each
competition — men's or women's, where it is played, where its fixtures come
from and whether that feed is connected yet — with a switch of its own.

What a switch means (orgs.forms.fed_competitions reads them): a sport or
competition that is OFF is offered to nobody. One that is ON is offered to
organisations once its feed is connected — offering a competition whose games
nothing fetches is how leagues ended up with an empty season (see that
function), so the screen says plainly which are waiting on a feed.
"""
from django.contrib import admin, messages
from django.http import Http404
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse
from django.views.decorators.http import require_POST

from catalog.models import Series, Sport

from . import access


def _guard(request):
    if not access.is_full_access(request.user):
        raise Http404("No sports screen here.")


def _connected(series) -> bool:
    from data_sync.services import competition_for_series
    return bool(competition_for_series(series.name))


def sports_hub(request):
    _guard(request)
    from data_sync.library import health

    lib = health()
    sports = []
    for sport in Sport.objects.prefetch_related("series").all():
        comps = []
        for s in sorted(sport.series.all(), key=lambda x: (x.sort_order, x.name)):
            comps.append({"obj": s, "connected": _connected(s), "lib": lib.get(s.id)})
        on = [c for c in comps if c["obj"].is_active]
        sports.append({
            "obj": sport, "comps": comps, "on": len(on),
            "live": sum(1 for c in on if c["connected"]) if sport.is_active else 0,
            "default": sport.slug in ("australian-rules", "rugby-league"),
        })
    ctx = admin.site.each_context(request)
    all_series = [c for s in sports for c in s["comps"]]
    ctx.update(
        title="Sports & competitions", sports=sports,
        sports_on=sum(1 for s in sports if s["obj"].is_active),
        comps_on=sum(1 for s in sports if s["obj"].is_active for c in s["comps"] if c["obj"].is_active),
        comps_total=len(all_series),
        live_total=sum(s["live"] for s in sports),
        lib_ok=sum(1 for h in lib.values() if h["ok"]),
        lib_total=len(lib),
        lib_games=sum(h["played"] + h["upcoming"] for h in lib.values()),
    )
    return render(request, "admin/hub/sports.html", ctx)


@require_POST
def sports_toggle(request):
    _guard(request)
    kind, pk = request.POST.get("kind"), request.POST.get("pk")
    model = {"sport": Sport, "series": Series}.get(kind)
    if model is None:
        raise Http404
    obj = get_object_or_404(model, pk=pk)
    obj.is_active = not obj.is_active
    obj.save(update_fields=["is_active"])
    sport = obj if kind == "sport" else obj.sport
    if obj.is_active:
        note = f"{obj.name} is on."
        if kind == "series" and not sport.is_active:
            note += f" {sport.name} itself is still off, so nobody sees it yet."
        elif kind == "series" and not _connected(obj):
            note += " It is offered to organisations once its feed is connected."
    else:
        note = f"{obj.name} is off — offered to nobody. Rounds and tips already made are kept."
    messages.success(request, note)
    return redirect(f"{request.POST.get('next') or reverse('admin:hq_sports')}#sport-{sport.slug}")


@require_POST
def library_refresh(request):
    """Refresh GoodTip's fixture library now, off the request (it reads every
    feed — minutes, not a page load)."""
    _guard(request)
    import threading

    from django.core.management import call_command
    from django.db import connections

    def work():
        try:
            call_command("refresh_library")
        finally:
            connections.close_all()

    threading.Thread(target=work, daemon=True, name="library-refresh").start()
    messages.success(request, "Refreshing the fixture library for every competition. "
                              "It takes a few minutes — reload to watch each one come in.")
    return redirect(reverse("admin:hq_sports"))
