from datetime import timedelta

from django.conf import settings
from django.contrib.admin.models import LogEntry
from django.db import models
from django.utils import timezone


class LoginEvent(models.Model):
    """One row per login attempt — success or failure.

    Django's own `last_login` field on the User model is overwritten on every
    sign-in, so there was never a history to look back on, only the most
    recent timestamp. This is the ledger: who signed in, when, from where, and
    whether an attempt against a real or made-up email even succeeded.
    """

    user = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.SET_NULL,
        null=True, blank=True, related_name="login_events",
    )
    # Kept even if `user` is later deleted, and it's the only identity we have
    # at all for a failed attempt against an email with no account.
    email = models.EmailField(blank=True)
    success = models.BooleanField(default=True)
    ip_address = models.GenericIPAddressField(null=True, blank=True)
    user_agent = models.CharField(max_length=300, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["-created_at"]
        indexes = [
            models.Index(fields=["user", "-created_at"]),
            models.Index(fields=["success", "-created_at"]),
        ]

    def __str__(self):
        state = "ok" if self.success else "failed"
        return f"{self.email or 'unknown'} ({state}) @ {self.created_at:%Y-%m-%d %H:%M}"


class StressTestRun(models.Model):
    """The result of a load/stress test run against this app.

    GoodTip has no built-in load generator on purpose — this just gives
    whatever tool is run separately (locust, k6, ab, a hand-rolled script...)
    a place to land its summary, either pasted in here via the admin "Add"
    form or piped in through `manage.py record_stress_test_run`.
    """

    label = models.CharField(max_length=120)
    target = models.CharField(
        max_length=200, blank=True,
        help_text="What was hit — a URL, a view name, a script name.",
    )
    started_at = models.DateTimeField()
    finished_at = models.DateTimeField(null=True, blank=True)
    total_requests = models.PositiveIntegerField(default=0)
    success_count = models.PositiveIntegerField(default=0)
    failure_count = models.PositiveIntegerField(default=0)
    avg_response_ms = models.FloatField(null=True, blank=True)
    p95_response_ms = models.FloatField(null=True, blank=True)
    max_response_ms = models.FloatField(null=True, blank=True)
    requests_per_sec = models.FloatField(null=True, blank=True)
    notes = models.TextField(blank=True)
    # The tool's full output, if it has more to say than the summary fields
    # above capture — kept as-is rather than losing detail to a fixed schema.
    raw_results = models.JSONField(default=dict, blank=True)
    created_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.SET_NULL,
        null=True, blank=True, related_name="stress_test_runs",
    )
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["-started_at"]

    def __str__(self):
        return f"{self.label} @ {self.started_at:%Y-%m-%d %H:%M}"


class AuditLog(LogEntry):
    """django.contrib.admin's LogEntry, under the name people look for.

    A proxy rather than a second table: the rows are Django's own, written on
    every admin save and delete, and duplicating them would mean two records of
    the same event that can disagree. All this buys is the label — "Log entries"
    is what Django calls it, "Audit log" is what somebody scanning the menu for
    it is actually looking for.
    """

    class Meta:
        proxy = True
        verbose_name = "audit log entry"
        verbose_name_plural = "Audit log"


# ---------------------------------------------------------------------------
# Delegated administration
#
# One person owned this system and held every power in it. They want help —
# somebody to write the blog, somebody to answer enquiries — without handing
# over the ability to delete every member. These five models are that: who may
# do what, how they were let in, what they did that needs looking at, and a
# record of all of it afterwards.
# ---------------------------------------------------------------------------
class AdminAccess(models.Model):
    """What one administrator is allowed to do.

    Sits beside the user rather than on it because it is not a property of
    being a person, it is a role that can be granted and taken away — and
    because `is_superuser` still means what Django means by it, so an account
    with full access here also carries the flag and every existing check keeps
    working untouched.

    FULL ACCESS is deliberately not "every capability ticked". It is its own
    flag, because the set of capabilities changes with the code: a full-access
    admin should gain a new power the day it ships, not the day somebody
    remembers to re-tick their boxes. It also carries the two things that
    cannot be delegated at all — creating administrators, and approving other
    people's changes.
    """

    user = models.OneToOneField(
        settings.AUTH_USER_MODEL, on_delete=models.CASCADE,
        related_name="admin_access",
    )
    is_full_access = models.BooleanField(
        default=False,
        help_text="Every power, including creating admins and approving their work.",
    )
    # Null for the founding admin, who was made with createsuperuser and has
    # nobody above them.
    created_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, blank=True,
        related_name="admins_created",
    )
    created_at = models.DateTimeField(default=timezone.now)
    # Switched off rather than deleted: an admin who has approved things should
    # still be nameable in the record of those approvals.
    is_active = models.BooleanField(default=True)
    note = models.CharField(
        max_length=200, blank=True,
        help_text="What this person is here to do. Shown in the admin list.",
    )

    class Meta:
        verbose_name_plural = "admin access"
        ordering = ["-is_full_access", "user__display_name"]

    def __str__(self):
        return f"{self.user} ({'full access' if self.is_full_access else 'restricted'})"

    # -- what they can do ---------------------------------------------------

    def capability_keys(self) -> set:
        return {g.capability for g in self.grants.all()}

    def can(self, capability: str) -> bool:
        if not self.is_active:
            return False
        if self.is_full_access:
            return True
        return self.grants.filter(capability=capability).exists()

    def needs_approval_for(self, capability: str) -> bool:
        """Whether doing this raises a change request instead of doing it.

        Full access never needs approval — there is nobody above them, and a
        queue only they can clear is a queue that never clears.
        """
        if self.is_full_access:
            return False
        return self.grants.filter(
            capability=capability, requires_approval=True
        ).exists()

    @property
    def summary(self) -> str:
        if self.is_full_access:
            return "Full access"
        n = self.grants.count()
        r = self.grants.filter(requires_approval=True).count()
        if not n:
            return "No access yet"
        bits = f"{n} area{'s' if n != 1 else ''}"
        return f"{bits}, {r} needing approval" if r else bits


class AdminGrant(models.Model):
    """One capability held by one administrator.

    A row rather than a JSON blob on AdminAccess so that "who can email the
    members?" is a query rather than a scan, and so that granting and revoking
    are ordinary writes that the audit log can describe precisely.
    """

    access = models.ForeignKey(AdminAccess, on_delete=models.CASCADE, related_name="grants")
    capability = models.CharField(max_length=40)
    # The whole point of the feature: hold the power, but every use of it waits
    # for a full-access administrator to look at it first.
    requires_approval = models.BooleanField(default=False)
    granted_at = models.DateTimeField(default=timezone.now)

    class Meta:
        ordering = ["capability"]
        constraints = [
            models.UniqueConstraint(
                fields=["access", "capability"], name="uniq_admin_grant",
            ),
        ]

    def __str__(self):
        return f"{self.capability}{' (reviewed)' if self.requires_approval else ''}"

    @property
    def label(self) -> str:
        from . import capabilities

        return capabilities.label(self.capability)


class AdminInvite(models.Model):
    """The one-time code that turns a new administrator's account on.

    Nobody sets somebody else's password. The account is created inactive with
    an unusable password, and this is the only way into it: a link plus a code
    emailed to the address, which the person exchanges for a password they
    choose themselves. So the person who created the account never knows the
    credential, and an invite sent to a mistyped address grants nothing.

    Only the hash of the code is kept, for the same reason LoginCode keeps only
    a hash — a database read must not be convertible into somebody else's
    administrator session.
    """

    TTL = timedelta(days=3)
    MAX_ATTEMPTS = 6
    CODE_LENGTH = 8

    access = models.ForeignKey(
        AdminAccess, on_delete=models.CASCADE, related_name="invites",
    )
    # In the URL. Long and random: it identifies the invite before any code has
    # been typed, so it must not be guessable.
    token = models.CharField(max_length=64, unique=True)
    code_hash = models.CharField(max_length=255)
    created_at = models.DateTimeField(default=timezone.now)
    expires_at = models.DateTimeField()
    attempts = models.PositiveSmallIntegerField(default=0)
    consumed_at = models.DateTimeField(null=True, blank=True)
    sent_to = models.EmailField()
    created_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, blank=True,
        related_name="admin_invites_sent",
    )

    class Meta:
        ordering = ["-created_at"]

    def __str__(self):
        return f"invite for {self.sent_to}"

    @classmethod
    def issue(cls, access, by_user=None):
        """A fresh invite, returning the row and the plaintext code to email."""
        import secrets

        from django.contrib.auth.hashers import make_password

        cls.objects.filter(access=access, consumed_at__isnull=True).update(
            consumed_at=timezone.now()
        )
        # Letters and digits, no look-alikes: this gets read off a screen and
        # typed by hand, and 0/O and 1/l are how that goes wrong.
        alphabet = "ABCDEFGHJKLMNPQRSTUVWXYZ23456789"
        code = "".join(secrets.choice(alphabet) for _ in range(cls.CODE_LENGTH))
        row = cls.objects.create(
            access=access,
            token=secrets.token_urlsafe(32),
            code_hash=make_password(code),
            expires_at=timezone.now() + cls.TTL,
            sent_to=access.user.email,
            created_by=by_user,
        )
        return row, code

    @property
    def is_usable(self) -> bool:
        return (
            self.consumed_at is None
            and self.attempts < self.MAX_ATTEMPTS
            and timezone.now() < self.expires_at
        )

    def verify(self, code: str) -> bool:
        from django.contrib.auth.hashers import check_password

        self.attempts += 1
        ok = check_password((code or "").strip().upper(), self.code_hash)
        self.save(update_fields=["attempts"])
        return ok

    def consume(self):
        self.consumed_at = timezone.now()
        self.save(update_fields=["consumed_at"])


class ChangeRequest(models.Model):
    """Something a restricted administrator did that has to be looked at first.

    HOW THE CHANGE IS CAPTURED
    --------------------------
    Not as a per-model draft. There is no `NewsPostDraft`, no `PendingPageEdit`
    and no serialiser per screen — that is one new model and one new form for
    every action that could ever need reviewing, and it goes stale the moment
    a screen gains a field.

    Instead the POST itself is held. When a reviewed administrator submits a
    form, the middleware stops the request before the view runs and stores what
    they sent: the path, the fields and any files. Approving it replays exactly
    that submission through exactly that view, as that user. So the outcome is
    identical to what would have happened without review, the reviewer sees the
    real thing rather than a summary somebody remembered to write, and adding a
    reviewable screen costs nothing.

    WHAT THE REVIEWER CAN DO
    ------------------------
    Approve it, decline it with a reason, or edit the fields and approve what
    they edited — which is the common case for writing, where the answer is
    rarely yes or no but "yes, with the second paragraph fixed". All three send
    the author what happened and why, because a queue that only ever says no
    teaches nobody anything.
    """

    PENDING = "pending"
    APPROVED = "approved"
    AMENDED = "amended"
    DECLINED = "declined"
    STATUS_CHOICES = [
        (PENDING, "Waiting for review"),
        (APPROVED, "Approved"),
        (AMENDED, "Approved with changes"),
        (DECLINED, "Declined"),
    ]

    requested_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.CASCADE,
        related_name="change_requests",
    )
    capability = models.CharField(max_length=40)
    # A sentence a person can read in a list without opening it: "New story:
    # Finals race tightens". Built when the request is captured, because that
    # is the only moment the intent is knowable.
    summary = models.CharField(max_length=200)

    # The submission, held verbatim.
    path = models.CharField(max_length=300)
    post_data = models.JSONField(default=dict, blank=True)
    # Screens that post JSON rather than a form — the page editor sends the
    # blocks it changed as one document — have nothing in request.POST, so the
    # raw body is kept instead and replayed with its own content type.
    raw_body = models.TextField(blank=True)
    content_type = models.CharField(max_length=100, blank=True)
    # What the reviewer changed before approving, if anything. Kept separate
    # from post_data so the original submission is never overwritten and the
    # author can be shown precisely what was altered.
    amended_data = models.JSONField(null=True, blank=True)

    status = models.CharField(max_length=10, choices=STATUS_CHOICES, default=PENDING)
    created_at = models.DateTimeField(default=timezone.now)
    reviewed_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, blank=True,
        related_name="change_reviews",
    )
    reviewed_at = models.DateTimeField(null=True, blank=True)
    feedback = models.TextField(
        blank=True,
        help_text="What the reviewer said. Sent to the author either way.",
    )
    applied_at = models.DateTimeField(null=True, blank=True)
    # Set when a replay raised. An approved request that failed to apply is a
    # thing somebody has to know about; silently marking it done is worse than
    # showing the error.
    apply_error = models.TextField(blank=True)

    class Meta:
        ordering = ["-created_at"]
        indexes = [
            models.Index(fields=["status", "-created_at"]),
            models.Index(fields=["requested_by", "-created_at"]),
        ]

    def __str__(self):
        return self.summary or f"change #{self.pk}"

    @property
    def is_pending(self) -> bool:
        return self.status == self.PENDING

    @property
    def was_accepted(self) -> bool:
        return self.status in (self.APPROVED, self.AMENDED)

    @property
    def capability_label(self) -> str:
        from . import capabilities

        return capabilities.label(self.capability)

    @property
    def is_json(self) -> bool:
        return self.content_type.startswith("application/json")

    def data_to_apply(self) -> dict:
        return self.amended_data if self.amended_data is not None else self.post_data

    def body_to_apply(self) -> str:
        """The JSON body to replay, with the reviewer's edits folded in."""
        if not self.is_json:
            return ""
        if self.amended_data is None:
            return self.raw_body
        import json as _json

        return _json.dumps(self.amended_data)

    def changed_fields(self) -> list:
        """The fields the reviewer altered: (name, before, after).

        Shown to the author, so they can see what "approved with changes"
        actually changed rather than being told it happened.
        """
        if self.amended_data is None:
            return []
        out = []
        for key in sorted(set(self.post_data) | set(self.amended_data)):
            before = self.post_data.get(key)
            after = self.amended_data.get(key)
            if before != after:
                out.append((key, before, after))
        return out


class ChangeRequestFile(models.Model):
    """A file that came with a held submission.

    Uploads cannot live in the JSON, and they cannot be left in the browser
    either — the person has closed the tab by the time anybody reviews it. So
    the file is stored on approval's behalf and put back into request.FILES
    when the submission is replayed.
    """

    request = models.ForeignKey(
        ChangeRequest, on_delete=models.CASCADE, related_name="files",
    )
    field_name = models.CharField(max_length=100)
    original_name = models.CharField(max_length=255)
    content_type = models.CharField(max_length=100, blank=True)
    upload = models.FileField(upload_to="change_requests/")

    def __str__(self):
        return f"{self.field_name}: {self.original_name}"


class AdminTask(models.Model):
    """A job one administrator has asked another to do.

    The client runs GoodTip with their partner and wants to be able to say
    "write something about the AFLW finals" without it being an email that gets
    lost. It lives beside the work rather than in an inbox, and the person it
    was given to sees it the moment they sign in.
    """

    OPEN = "open"
    DONE = "done"
    CANCELLED = "cancelled"
    STATUS_CHOICES = [(OPEN, "To do"), (DONE, "Done"), (CANCELLED, "Cancelled")]

    assigned_to = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name="admin_tasks",
    )
    assigned_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, blank=True,
        related_name="admin_tasks_given",
    )
    title = models.CharField(max_length=160)
    detail = models.TextField(blank=True)
    status = models.CharField(max_length=10, choices=STATUS_CHOICES, default=OPEN)
    created_at = models.DateTimeField(default=timezone.now)
    completed_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        ordering = ["status", "-created_at"]

    def __str__(self):
        return self.title


class AdminAuditEvent(models.Model):
    """What administrators did to each other and to each other's work.

    Separate from LoginEvent (who signed in) and from Django's own LogEntry
    (which rows changed) because neither answers the question this feature
    creates: who gave whom which powers, who approved what, and why something
    was turned down. The client asked for exactly that trail, and it is the
    only record that survives a grant being revoked or a change request being
    deleted — so the detail is denormalised into JSON rather than pointing at
    rows that may not be there later.
    """

    ADMIN_CREATED = "admin_created"
    ACCESS_CHANGED = "access_changed"
    ADMIN_SUSPENDED = "admin_suspended"
    ADMIN_RESTORED = "admin_restored"
    INVITE_SENT = "invite_sent"
    INVITE_ACCEPTED = "invite_accepted"
    CHANGE_REQUESTED = "change_requested"
    CHANGE_APPROVED = "change_approved"
    CHANGE_AMENDED = "change_amended"
    CHANGE_DECLINED = "change_declined"
    TASK_ASSIGNED = "task_assigned"
    TASK_COMPLETED = "task_completed"

    ACTION_CHOICES = [
        (ADMIN_CREATED, "Created an administrator"),
        (ACCESS_CHANGED, "Changed what an administrator can do"),
        (ADMIN_SUSPENDED, "Suspended an administrator"),
        (ADMIN_RESTORED, "Restored an administrator"),
        (INVITE_SENT, "Sent an invitation"),
        (INVITE_ACCEPTED, "Accepted an invitation"),
        (CHANGE_REQUESTED, "Submitted work for review"),
        (CHANGE_APPROVED, "Approved a change"),
        (CHANGE_AMENDED, "Approved a change with edits"),
        (CHANGE_DECLINED, "Declined a change"),
        (TASK_ASSIGNED, "Assigned a task"),
        (TASK_COMPLETED, "Completed a task"),
    ]

    actor = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, blank=True,
        related_name="admin_audit_events",
    )
    action = models.CharField(max_length=24, choices=ACTION_CHOICES)
    # Who it was done to, when that is a person.
    subject = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, blank=True,
        related_name="admin_audit_subject_of",
    )
    # Names kept as text, not FKs: this has to still read correctly after the
    # account it describes is gone.
    actor_name = models.CharField(max_length=160, blank=True)
    subject_name = models.CharField(max_length=160, blank=True)
    summary = models.CharField(max_length=300, blank=True)
    detail = models.JSONField(default=dict, blank=True)
    created_at = models.DateTimeField(default=timezone.now)

    class Meta:
        ordering = ["-created_at"]
        indexes = [
            models.Index(fields=["-created_at"]),
            models.Index(fields=["action", "-created_at"]),
            models.Index(fields=["subject", "-created_at"]),
        ]

    def __str__(self):
        return f"{self.actor_name or 'someone'} — {self.get_action_display()}"

    @classmethod
    def record(cls, *, actor=None, action, subject=None, summary="", **detail):
        return cls.objects.create(
            actor=actor,
            actor_name=str(actor) if actor else "",
            action=action,
            subject=subject,
            subject_name=str(subject) if subject else "",
            summary=summary[:300],
            detail=detail or {},
        )
