"""Holding a restricted administrator's work until somebody approves it.

WHAT THIS DOES
--------------
A screen declares which capability it belongs to. When somebody submits a form
on it, `guard()` decides one of three things:

  * they do not hold the capability      -> refuse
  * they hold it outright                -> let the view run
  * they hold it "needs approval"        -> capture the submission, don't run

Capturing means storing the POST fields and any uploaded files against a
ChangeRequest. Approving replays that exact submission through that exact
view, as the person who made it, so what finally happens is what would have
happened without review — no second code path that can drift from the first.

WHY REPLAY RATHER THAN DRAFT
----------------------------
The alternative is a draft model per reviewable thing: a pending post, a
pending page edit, a pending reply, each with its own fields, its own form and
its own way of going stale when the real screen gains a field. Replay has one
mechanism for all of them and gains new reviewable screens for free. What it
costs is that a held submission is only as replayable as the view is
idempotent, which is why `apply()` records what went wrong instead of assuming
it worked.
"""
import json
import logging

from django.core.files.base import ContentFile
from django.utils import timezone

from . import access
from .models import AdminAuditEvent, ChangeRequest, ChangeRequestFile


logger = logging.getLogger(__name__)

# Fields never worth storing: Django's own plumbing, and anything that would
# put a credential in the database.
SKIP_FIELDS = {"csrfmiddlewaretoken"}
SENSITIVE = ("password", "token", "secret")


class NotAllowed(Exception):
    """They do not hold this capability at all."""


class HeldForReview(Exception):
    """Captured. The view must not run.

    Carries the request so the caller can tell the person what happened and
    where to look for it.
    """

    def __init__(self, change_request):
        self.change_request = change_request
        super().__init__("held for review")


def _clean_post(request) -> dict:
    out = {}
    for key in request.POST:
        if key in SKIP_FIELDS or any(s in key.lower() for s in SENSITIVE):
            continue
        values = request.POST.getlist(key)
        out[key] = values if len(values) > 1 else values[0]
    return out


def capture(request, capability: str, summary: str) -> ChangeRequest:
    """Store this submission for review instead of acting on it."""
    ctype = (request.content_type or "").split(";")[0]
    is_json = ctype == "application/json"
    raw, parsed = "", {}
    if is_json:
        # request.POST is empty for a JSON body, so keep the document itself.
        # Parsed as well as raw: the reviewer edits the parsed form, and the
        # raw copy is what gets replayed when they change nothing.
        try:
            raw = request.body.decode("utf-8")
            parsed = json.loads(raw)
            if not isinstance(parsed, dict):
                parsed = {"_": parsed}
        except (ValueError, UnicodeDecodeError):
            raw, parsed = "", {}

    cr = ChangeRequest.objects.create(
        requested_by=request.user,
        capability=capability,
        summary=summary[:200],
        path=request.get_full_path()[:300],
        post_data=parsed if is_json else _clean_post(request),
        raw_body=raw,
        content_type=ctype[:100],
    )
    for field, files in request.FILES.lists():
        for f in files:
            row = ChangeRequestFile(
                request=cr,
                field_name=field,
                original_name=getattr(f, "name", "")[:255],
                content_type=getattr(f, "content_type", "") or "",
            )
            f.seek(0)
            row.upload.save(getattr(f, "name", "upload"), ContentFile(f.read()), save=False)
            row.save()

    AdminAuditEvent.record(
        actor=request.user,
        action=AdminAuditEvent.CHANGE_REQUESTED,
        summary=summary,
        capability=capability,
        change_request=cr.pk,
    )
    from .notify import notify_reviewers_of_new_request

    notify_reviewers_of_new_request(cr)
    return cr


def guard(request, capability: str, summary_fn=None):
    """The one call a reviewable screen makes before it acts.

    `summary_fn` builds the one-line description of what is being submitted; it
    only runs when the submission is actually going to be held, because it
    reads the POST and there is no point doing that on the common path.
    """
    user = request.user
    if not access.can(user, capability):
        raise NotAllowed(capability)
    if request.method != "POST":
        return
    # A replay is the approved submission finally being carried out. It must
    # not be captured a second time.
    if getattr(request, "_replaying_change", None) is not None:
        return
    if not access.needs_approval(user, capability):
        return
    summary = summary_fn(request) if summary_fn else capability
    raise HeldForReview(capture(request, capability, summary))


def apply(change_request, *, reviewer):
    """Replay an approved submission through the view it was aimed at.

    Resolved from the stored path rather than from a recorded view name,
    because the path is what the person actually submitted to and a view name
    stored alongside it is a second thing that can disagree with it.
    """
    from django.core.files.uploadedfile import SimpleUploadedFile
    from django.test import RequestFactory
    from django.urls import resolve

    cr = change_request
    data = dict(cr.data_to_apply())
    path = cr.path.split("?")[0]

    files = {}
    for row in cr.files.all():
        row.upload.open("rb")
        files[row.field_name] = SimpleUploadedFile(
            row.original_name or "upload", row.upload.read(),
            content_type=row.content_type or "application/octet-stream",
        )

    factory = RequestFactory()
    if cr.is_json:
        req = factory.post(
            path, data=cr.body_to_apply(), content_type=cr.content_type or "application/json",
        )
    else:
        payload = dict(data)
        payload.update(files)
        req = factory.post(path, data=payload)
    req.user = cr.requested_by
    # Read by guard(): this submission has already been through review, so it
    # runs rather than being captured again.
    req._replaying_change = cr

    # Every admin view is wrapped in csrf_protect by AdminSite.admin_view, and
    # this request was built in-process rather than posted by a browser, so it
    # carries no token and would be refused. Skipping the check is correct
    # here and only here: the token WAS verified when the submission was
    # originally captured, and there is no cross-site anything in replaying a
    # row out of our own database. This is the same flag Django's own test
    # client sets for the same reason.
    req._dont_enforce_csrf_checks = True

    # The replayed view may call messages.success(); give it somewhere to go
    # that is not the reviewer's own screen.
    from django.contrib.messages.storage.fallback import FallbackStorage
    from django.contrib.sessions.backends.db import SessionStore

    req.session = SessionStore()
    req._messages = FallbackStorage(req)

    try:
        match = resolve(path)
        response = match.func(req, *match.args, **match.kwargs)
        status = getattr(response, "status_code", 0)
        if status >= 400:
            raise RuntimeError(f"the screen answered {status}")
    except Exception as exc:
        logger.exception("Replaying change request %s failed", cr.pk)
        cr.apply_error = f"{type(exc).__name__}: {exc}"[:2000]
        cr.save(update_fields=["apply_error"])
        return False

    cr.applied_at = timezone.now()
    cr.apply_error = ""
    cr.save(update_fields=["applied_at", "apply_error"])
    return True


def decide(change_request, *, reviewer, outcome, feedback="", amended=None):
    """Approve, amend or decline — and tell the author which, and why."""
    cr = change_request
    cr.reviewed_by = reviewer
    cr.reviewed_at = timezone.now()
    cr.feedback = feedback or ""
    if amended is not None:
        cr.amended_data = amended
    cr.status = outcome
    cr.save()

    action = {
        ChangeRequest.APPROVED: AdminAuditEvent.CHANGE_APPROVED,
        ChangeRequest.AMENDED: AdminAuditEvent.CHANGE_AMENDED,
        ChangeRequest.DECLINED: AdminAuditEvent.CHANGE_DECLINED,
    }[outcome]

    applied = False
    if cr.was_accepted:
        applied = apply(cr, reviewer=reviewer)

    AdminAuditEvent.record(
        actor=reviewer,
        action=action,
        subject=cr.requested_by,
        summary=cr.summary,
        capability=cr.capability,
        change_request=cr.pk,
        feedback=feedback,
        applied=applied,
        changed_fields=[f[0] for f in cr.changed_fields()],
    )
    from .notify import notify_author_of_decision

    notify_author_of_decision(cr)
    return applied
