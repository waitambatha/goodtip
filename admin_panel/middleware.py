"""Applies an admin's page edits to the response on its way out.

Runs last in the chain so it sees the finished HTML, including anything the
other middleware added. It does nothing at all — not even a database query —
for a response that is not one of the registered pages, which is every request
to an API endpoint, a partial, a redirect, a file, or the admin itself.
"""
import logging

from django.db.models import F
from django.http import HttpResponsePermanentRedirect, HttpResponseRedirect
from django.template.loader import render_to_string
from django.utils import timezone

from . import pages, seo as seo_engine
from .models import PageEdit, PageSeo, Redirect
from .pagetext import rewrite


logger = logging.getLogger(__name__)

# Set on the request by the editor's own view helper; read here to decide
# whether to tag the blocks up for editing.
EDIT_PARAM = "gt_edit"


def _wants_edit_mode(request) -> bool:
    """Edit mode is superuser-only, and asked for explicitly in the URL.

    Deliberately not "on for every superuser": the client is a superuser
    reading their own site most of the time, and a page that quietly turns
    into a text editor under them is worse than one that needs a click.
    """
    if request.GET.get(EDIT_PARAM) != "1":
        return False
    user = getattr(request, "user", None)
    return bool(user and user.is_authenticated and user.is_superuser)


class PageEditMiddleware:
    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        response = self.get_response(request)

        page = pages.page_for_request(request)
        if page is None:
            return response
        if response.status_code != 200 or response.streaming:
            return response
        if not response.get("Content-Type", "").startswith("text/html"):
            return response

        edit_mode = _wants_edit_mode(request)
        rows = list(PageEdit.objects.filter(page=page.key))
        seo = PageSeo.objects.filter(page=page.key).first()

        # A page given a new address has ONE address. Reached at the built-in
        # one, it says so permanently and sends the reader on, so the renamed
        # page does not sit on the web at two URLs competing with each other
        # for the same ranking — which is the thing a rename is meant to fix.
        #
        # Checked here, after the view, because which page this is only becomes
        # knowable once routing has happened. It costs a wasted render on the
        # old address, which is a handful of requests from stale links, and
        # nothing at all on the new one.
        moved = _moved_to(request, seo)
        if moved:
            return HttpResponsePermanentRedirect(moved)

        if not rows and not edit_mode and seo is None:
            return response

        edits = {}
        for row in rows:
            replacement = row.replacement
            # An image edit with neither a file nor a description is not an
            # edit any more — but one carrying only a description is: a
            # picture can be right and its alt text wrong, and fixing that
            # must not require replacing the picture.
            if row.kind == PageEdit.KIND_IMAGE and not replacement and not row.image_alt:
                continue
            edits[row.block_key] = (row.kind, replacement, row.image_alt)

        try:
            html = response.content.decode(response.charset)
            new_html, applied = rewrite(html, edits, edit_mode=edit_mode)
        except Exception:
            # A page that cannot be parsed is served exactly as it was. Losing
            # the edits on one page is recoverable; a 500 on the landing page
            # because of a stray tag is not.
            logger.exception("Page edit rewrite failed for %s", page.key)
            return response

        _stamp(rows, applied)

        # SEO settings after the wording edits, on the same string. Its own
        # try/except for the same reason as above and one more: this half is
        # regex over the <head> and the other is a parse of the <body>, so a
        # failure in one has no business taking the other's work with it.
        if seo is not None:
            try:
                new_html = seo_engine.apply_to_html(new_html, seo, request=request)
            except Exception:
                logger.exception("SEO rewrite failed for %s", page.key)

        if edit_mode:
            new_html = _inject_editor(request, page, new_html)

        response.content = new_html.encode(response.charset)
        if response.has_header("Content-Length"):
            response["Content-Length"] = str(len(response.content))
        return response


def _moved_to(request, seo):
    """The address this page should be served at, if it is not this one.

    Only ever redirects a plain GET: a POST answered with a 301 is re-sent as a
    GET by every browser, so a form posted to the old address would come back
    as an empty page load and lose what was typed.
    """
    if seo is None or not seo.path_override:
        return ""
    if request.method != "GET":
        return ""
    wanted = "/" + seo.path_override.strip("/") + "/"
    if request.path.rstrip("/") == wanted.rstrip("/"):
        return ""
    query = request.META.get("QUERY_STRING", "")
    return f"{wanted}?{query}" if query else wanted


def _stamp(rows, applied):
    """Record which edits actually matched something on this render.

    This is what lets the manage page tell an admin "this edit is no longer
    showing, because the wording it replaced has changed" instead of listing it
    as live and leaving them to wonder why the page looks untouched.
    """
    now = timezone.now()
    hit = [r.pk for r in rows if r.block_key in applied and r.last_applied_at is None]
    missed = [r.pk for r in rows if r.block_key not in applied and r.last_applied_at is not None]
    if hit:
        PageEdit.objects.filter(pk__in=hit).update(last_applied_at=now)
    if missed:
        PageEdit.objects.filter(pk__in=missed).update(last_applied_at=None)


def _inject_editor(request, page, html: str) -> str:
    """Put the editing bar, its toolbar and its scripts on the page.

    Injected rather than added to a base template because no page was prepared
    for this: the feature's whole premise is that any registered page becomes
    editable without being touched. It goes in immediately before </body> so
    the scripts run after the page they are about to wire up exists.
    """
    chrome = render_to_string(
        "manage/_page_editor.html", {"page": page}, request=request
    )
    lowered = html.lower()
    at = lowered.rfind("</body>")
    if at == -1:
        return html + chrome
    return html[:at] + chrome + html[at:]


class RedirectFallbackMiddleware:
    """What happens to an address Django could not route.

    Two things, in this order:

      1. A page an admin has MOVED here is served (`seo.serve_override`).
      2. Failing that, an address an admin has REDIRECTED is redirected.

    Both only on a 404, which is what makes them safe: a live route always wins,
    neither table is read on a request that resolved, and putting a row in
    either one can never take an existing page off the site. Routing is not
    touched at all, so APPEND_SLASH and every other 404 behave exactly as they
    did — see `seo.serve_override` for why that is worth doing this way.

    See `Redirect` for why this exists instead of `django.contrib.redirects`.

    A redirect pointing at itself is ignored rather than served: a 301 loop is
    cached by the browser and by every proxy in between, which makes it the one
    kind of mistake in this table that an admin cannot fix by editing the row
    they just saved.
    """

    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        response = self.get_response(request)
        if response.status_code != 404:
            return response

        try:
            moved = seo_engine.serve_override(request)
        except Exception:
            # A page that will not render at its new address must not turn a
            # 404 into a 500. It falls through to the redirect table and then
            # to the 404 the request already had.
            logger.exception("Serving a moved page failed for %s", request.path)
            moved = None
        if moved is not None:
            return moved

        path = Redirect.normalise(request.path)
        if not path:
            return response
        # Both spellings. A link in the wild is as likely to be missing the
        # trailing slash as to have it, and an SEO team typing one form should
        # not have to know which one the crawler will use.
        alternatives = [path]
        alternatives.append(path[:-1] if path.endswith("/") else path + "/")
        row = (
            Redirect.objects.filter(old_path__in=alternatives)
            .order_by("old_path")
            .first()
        )
        if row is None or not row.new_path:
            return response
        target = row.new_path.strip()
        if Redirect.normalise(target) == path and "://" not in target:
            return response

        Redirect.objects.filter(pk=row.pk).update(
            hits=F("hits") + 1, last_hit_at=timezone.now(),
        )
        return (
            HttpResponsePermanentRedirect(target) if row.is_permanent
            else HttpResponseRedirect(target)
        )
