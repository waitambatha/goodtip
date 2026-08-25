from django.conf import settings
from django.db import models


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
