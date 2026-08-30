"""Applies an admin's page edits to the response on its way out.

Runs last in the chain so it sees the finished HTML, including anything the
other middleware added. It does nothing at all — not even a database query —
for a response that is not one of the registered pages, which is every request
to an API endpoint, a partial, a redirect, a file, or the admin itself.
"""
import logging

from django.template.loader import render_to_string
from django.utils import timezone

from . import pages
from .models import PageEdit
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
        if not rows and not edit_mode:
            return response

        edits = {}
        for row in rows:
            replacement = row.replacement
            # An image edit whose file has gone is not an edit any more.
            if row.kind == PageEdit.KIND_IMAGE and not replacement:
                continue
            edits[row.block_key] = (row.kind, replacement)

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

        if edit_mode:
            new_html = _inject_editor(request, page, new_html)

        response.content = new_html.encode(response.charset)
        if response.has_header("Content-Length"):
            response["Content-Length"] = str(len(response.content))
        return response


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
