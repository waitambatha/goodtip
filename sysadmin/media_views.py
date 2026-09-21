"""The media library: every picture and clip, and where each one is shown.

CLIENT, 20 SEP 2026: "we need to add a functionality in the super admin that
will be images and videos — all the images and videos, when clicked, it should
[show] where and where it is being displayed, and have the option of changing
it."

Two screens. The library is a gallery with three filters across it; the detail
screen answers the question the gallery exists to serve. What the answer looks
like depends on which kind of picture it is, and sysadmin/media_library.py has
the long version of why.

GATED ON pages.images, which is "replace the photographs used on any page" —
the capability this screen's whole purpose is to lead somebody to. Anybody who
may swap a picture needs to be able to find out what swapping it would affect,
and anybody who may not has no use for the answer.
"""
from pathlib import Path

from django.contrib import admin, messages
from django.core.management import call_command
from django.http import Http404
from django.shortcuts import redirect, render
from django.urls import reverse
from django.views.decorators.http import require_POST

from . import access, media_library as lib


def _ctx(request, title, **extra):
    ctx = admin.site.each_context(request)
    ctx.update(title=title, **extra)
    return ctx


def _guard(request):
    if not access.can(request.user, "pages.images"):
        raise Http404("No media library here.")


def media_hub(request):
    """The gallery.

    Everything is built on every request, deliberately. The site's pictures are
    sixty-odd files on local disk and the uploads are three figures of rows —
    cheap enough that a cache would be a second thing to be wrong, and this is
    a screen somebody opens BECAUSE they have just changed something and want
    to see it.
    """
    _guard(request)

    everything = lib.site_assets() + lib.uploaded_assets()

    source = request.GET.get("source") or "all"
    kind = request.GET.get("kind") or "all"
    q = (request.GET.get("q") or "").strip()

    shown = everything
    if source in ("site", "upload"):
        shown = [a for a in shown if a.source == source]
    if kind in ("image", "video"):
        shown = [a for a in shown if a.kind == kind]
    if q:
        needle = q.lower()
        shown = [
            a for a in shown
            if needle in a.name.lower()
            or needle in a.subtitle.lower()
            or needle in a.owner_label.lower()
        ]

    return render(request, "admin/hub/media.html", _ctx(
        request, "Images & videos",
        assets=shown,
        totals=lib.counts(everything),
        showing=lib.counts(shown),
        source=source, kind=kind, q=q,
        hub_title="Images & videos",
        hub_lead="Everything the site shows, in one place — the pictures that "
                 "ship with the product and the ones people have uploaded. "
                 "Open any of them to see where it appears.",
        hub_icon="ic-image",
        max_mb=round(MAX_UPLOAD / (1024 * 1024)),
    ))


#: Ceiling on a replacement. Generous for a photograph and nowhere near what a
#: video needs — the site's own clips are not served from static/, so anything
#: approaching this is a mistake rather than a large picture.
MAX_UPLOAD = 12 * 1024 * 1024


@require_POST
def media_replace(request):
    """Overwrite a site picture with an uploaded one.

    CLIENT, 20 SEP 2026: "yes, add the button with the caveat."

    THE CAVEAT IS REAL AND IT IS NOT ABOUT PERMISSIONS. `static/` is inside the
    git checkout, and deploy.sh pulls with `git pull --rebase`. A tracked file
    modified on the server does not get quietly reverted by the next deploy —
    the rebase REFUSES, and deploys stop until somebody logs in and sorts it
    out. So the screen states that in as many words, the administrator has to
    tick that they have read it, and the response says what has to happen next.

    THE SUFFIX CANNOT CHANGE. Every template, stylesheet and Python list that
    names this picture names it with its extension; accepting a .png over a
    .jpg would replace a file nothing references and leave the original in
    place, which looks exactly like the feature silently failing.

    COLLECTSTATIC RUNS AFTERWARDS, and has to. Production serves static through
    a manifest of content-hashed names, so writing the source file alone
    changes nothing a visitor can see — the old hash is still what the pages
    ask for. It is a few seconds on a button somebody presses rarely, which is
    the right place to spend them.
    """
    _guard(request)

    key = request.POST.get("key") or ""
    asset = lib.site_asset(key)
    if asset is None:
        raise Http404("No such picture.")

    back = f"{reverse('admin:hq_media_detail')}?source=site&key={key}"

    if not request.POST.get("understood"):
        messages.error(
            request,
            "Tick the box to say you have read what replacing a shipped "
            "picture does before it will go through.",
        )
        return redirect(back)

    upload = request.FILES.get("replacement")
    if upload is None:
        messages.error(request, "Choose a file to replace it with.")
        return redirect(back)

    want = Path(key).suffix.lower()
    got = Path(upload.name or "").suffix.lower()
    if got != want:
        messages.error(
            request,
            f"It has to be a {want} file. Everything that shows this picture "
            f"names it as {Path(key).name}, so a {got or 'file with no'} "
            "extension would replace something nothing is looking at.",
        )
        return redirect(back)

    if upload.size > MAX_UPLOAD:
        messages.error(
            request,
            f"That is {upload.size // (1024 * 1024)} MB. The ceiling is "
            f"{MAX_UPLOAD // (1024 * 1024)} MB — a picture this size costs "
            "every visitor who loads the page it is on.",
        )
        return redirect(back)

    # Resolved through site_asset above, which refuses anything outside the
    # static root — so this path is known-good rather than assembled here.
    target = (lib._static_root() / key).resolve()
    try:
        with open(target, "wb") as out:
            for chunk in upload.chunks():
                out.write(chunk)
    except OSError as exc:
        messages.error(request, f"Could not write the file: {exc}")
        return redirect(back)

    collected = True
    try:
        call_command("collectstatic", "--noinput", verbosity=0)
    except Exception:  # noqa: BLE001 — the file IS replaced; say so honestly
        collected = False

    lib.log_replacement(request.user, key, upload.name, upload.size)

    if collected:
        messages.success(
            request,
            f"{Path(key).name} has been replaced everywhere it appears. "
            "IT IS NOW AN UNCOMMITTED CHANGE IN THE CHECKOUT: commit it, or "
            "the next deploy will stop on it.",
        )
    else:
        messages.warning(
            request,
            f"{Path(key).name} was written, but the static files could not be "
            "rebuilt — visitors will still be served the old one until "
            "collectstatic runs. Commit the file and deploy.",
        )
    return redirect(back)


def media_detail(request):
    """One picture, and the answer to "where is this being shown?".

    FOR AN UPLOAD the answer is a row, and the way to change it is that row's
    own form — which already exists, already validates the file and already
    writes the audit entry. Sending somebody there beats building a second
    upload box that does three-quarters of the same job.

    FOR A SITE PICTURE the answer is a list of files, because that is where the
    reference actually is. There are two ways to change it and the screen
    offers both, because they are for different jobs: the Pages editor swaps a
    photograph on ONE page and touches nothing in the repository, while Replace
    below overwrites the file itself and changes it everywhere at once. The
    second one edits the codebase, and the screen says so before it will do it.
    """
    _guard(request)

    source = request.GET.get("source") or "site"
    key = request.GET.get("key") or ""

    if source == "upload":
        asset = lib.uploaded_asset(key)
        hits = []
    else:
        source = "site"
        asset = lib.site_asset(key)
        hits = lib.where_used(key) if asset else []

    if asset is None:
        raise Http404("No such picture.")

    return render(request, "admin/hub/media_detail.html", _ctx(
        request, asset.name,
        asset=asset,
        hits=hits,
        hit_cap=lib.MAX_HITS,
        hub_title=asset.name,
        hub_lead=(
            f"Uploaded · {asset.subtitle}" if asset.source == "upload"
            else f"Ships with the product · static/{asset.key}"
        ),
        hub_icon="ic-image",
    ))
