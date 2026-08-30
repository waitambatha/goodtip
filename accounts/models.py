import secrets
from datetime import timedelta

from django.contrib.auth.hashers import check_password, make_password
from django.contrib.auth.models import AbstractUser, BaseUserManager
from django.db import models
from django.utils import timezone


class UserManager(BaseUserManager):
    use_in_migrations = True

    def _create_user(self, email, password, **extra_fields):
        if not email:
            raise ValueError("Email is required")
        email = self.normalize_email(email)
        extra_fields.setdefault("username", email)
        user = self.model(email=email, **extra_fields)
        user.set_password(password)
        user.save(using=self._db)
        return user

    def create_user(self, email, password=None, **extra_fields):
        extra_fields.setdefault("is_staff", False)
        extra_fields.setdefault("is_superuser", False)
        return self._create_user(email, password, **extra_fields)

    def create_superuser(self, email, password=None, **extra_fields):
        extra_fields.setdefault("is_staff", True)
        extra_fields.setdefault("is_superuser", True)
        if extra_fields.get("is_staff") is not True:
            raise ValueError("Superuser must have is_staff=True.")
        if extra_fields.get("is_superuser") is not True:
            raise ValueError("Superuser must have is_superuser=True.")
        return self._create_user(email, password, **extra_fields)


class User(AbstractUser):
    email = models.EmailField(unique=True)
    display_name = models.CharField(max_length=100)
    avatar = models.ImageField(upload_to="avatars/", blank=True)
    # Two-step verification on sign-in. On by default because the accounts hold
    # a group's money decisions, and off is a choice someone makes knowingly in
    # settings rather than a default nobody notices. Signup verification is
    # separate and always runs — that one proves the address exists at all.
    two_factor_enabled = models.BooleanField(default=True)
    email_verified_at = models.DateTimeField(null=True, blank=True)

    # WHETHER A TIP CARRIES TO YOUR OTHER ROOMS.
    #
    # Someone in a work comp, a mates' group and a family comp was tipping the
    # same eight games three times over — the picks are identical, only the
    # ladder differs. Carrying them is the obvious fix and the wrong default:
    # a tip is a commitment in each room it lands in, and quietly writing one
    # into a room somebody did not have in mind is not a convenience.
    #
    # So it is asked, once, with the option to stop being asked. ASK is the
    # default and only ever surfaces for people who are in more than one room
    # at all — which is nearly nobody today, and the point is that it stays
    # invisible to them.
    CARRY_ASK = "ask"
    CARRY_ALL = "all"
    CARRY_NONE = "none"
    CARRY_CHOICES = [
        (CARRY_ASK, "Ask me each time"),
        (CARRY_ALL, "Always carry my tips to every group I'm in"),
        (CARRY_NONE, "Only ever tip the group I'm in"),
    ]
    tip_carry_mode = models.CharField(
        max_length=5, choices=CARRY_CHOICES, default=CARRY_ASK,
    )

    # WHEN THE FIRST-VISIT WALKTHROUGH WAS PUT AWAY.
    #
    # Stamped when the member skips it or reaches the end — not when it is
    # rendered. Rendering is not seeing: a page opened and abandoned, or one
    # whose script never ran, would otherwise burn the single chance to
    # explain the app, silently and permanently.
    #
    # A column rather than localStorage because "first visit" ought to mean
    # the person's first visit, not this browser's. Somebody who signs up on
    # a laptop and opens the app on their phone that evening has already had
    # the tour.
    onboarding_seen_at = models.DateTimeField(null=True, blank=True)

    # WHICH PAGES' WALKTHROUGHS HAVE BEEN PUT AWAY, by tour key.
    #
    # The one flag above could only ever answer "has this person had THE
    # tour", which was true while there was exactly one, on the dashboard.
    # Every private page now introduces itself the first time it is opened,
    # so the question became "has this person had the tour FOR THIS PAGE" —
    # a set, not a boolean.
    #
    # A JSON list on the row rather than a table of (user, key) because this
    # is read on every private page render and the user row is already
    # loaded: a set membership test against a column costs nothing, where a
    # second table costs a query per page — and against the server database
    # that is a ~274ms round-trip for a fact that is almost always "yes,
    # already seen, show nothing".
    #
    # Keys are the tour names in accounts/onboarding.py, not URL paths, so a
    # route can be renamed without re-showing a tour somebody has finished.
    onboarding_pages_seen = models.JSONField(default=list, blank=True)

    def has_seen_tour(self, key: str) -> bool:
        return key in (self.onboarding_pages_seen or [])

    def mark_tour_seen(self, key: str) -> bool:
        """Record that a page's walkthrough is done. True if this changed anything.

        Idempotent, like the flag it replaces: a bell that has been rung does
        not need ringing twice, and the endpoint behind this is fired by both
        "Skip" and "Got it".
        """
        seen = list(self.onboarding_pages_seen or [])
        if key in seen:
            return False
        seen.append(key)
        self.onboarding_pages_seen = seen
        self.save(update_fields=["onboarding_pages_seen"])
        return True

    USERNAME_FIELD = "email"
    REQUIRED_FIELDS = ["display_name"]

    objects = UserManager()

    def __str__(self):
        return self.display_name or self.email


class LoginCode(models.Model):
    """A one-time 6-digit code emailed to prove someone holds the address.

    The code itself is never stored — only a hash — so a database read can't
    be turned into a working sign-in. Six digits is 1,000,000 possibilities,
    which is only safe because of the three limits enforced together here:
    a short lifetime, a hard attempt cap, and single use.
    """

    PURPOSE_LOGIN = "login"
    PURPOSE_SIGNUP = "signup"
    # The Django admin's own gate. Separate from PURPOSE_LOGIN on purpose: a
    # code emailed to get into the member app must not also open the control
    # plane, and issuing burns any earlier unused code for the SAME purpose
    # only — so being asked to verify for /admin/ cannot invalidate a sign-in
    # code the same person is part-way through using.
    PURPOSE_ADMIN = "admin"
    PURPOSE_CHOICES = [
        (PURPOSE_LOGIN, "Sign-in verification"),
        (PURPOSE_SIGNUP, "Email verification at signup"),
        (PURPOSE_ADMIN, "Admin access verification"),
    ]

    TTL = timedelta(minutes=10)
    MAX_ATTEMPTS = 5
    # Long enough that a mistyped digit isn't a lockout, short enough that a
    # stolen code is worthless by the time anyone gets to it.

    # How many digits a code has. Named rather than left as a literal in three
    # places, because three things have to agree on it and used not to: the
    # generator below, the form's "exactly six digits" rule, and the sentence
    # on the verify page that tells the member what to expect. That sentence
    # printed the *form field's* max_length instead and so advertised a
    # 16-digit code. The verify page's auto-submit now needs the number too.
    CODE_LENGTH = 6

    user = models.ForeignKey(
        "accounts.User", on_delete=models.CASCADE, related_name="login_codes"
    )
    code_hash = models.CharField(max_length=255)
    purpose = models.CharField(max_length=10, choices=PURPOSE_CHOICES, default=PURPOSE_LOGIN)
    created_at = models.DateTimeField(auto_now_add=True)
    expires_at = models.DateTimeField()
    attempts = models.PositiveSmallIntegerField(default=0)
    consumed_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        ordering = ["-created_at"]
        indexes = [models.Index(fields=["user", "purpose", "consumed_at"])]

    def __str__(self):
        return f"{self.get_purpose_display()} for {self.user.email}"

    @classmethod
    def issue(cls, user, purpose=PURPOSE_LOGIN) -> tuple["LoginCode", str]:
        """Create a fresh code, returning the row and the plaintext to email.

        Any earlier unused code for the same purpose is burned first, so
        requesting a new one can't leave two valid codes in the wild.
        """
        cls.objects.filter(
            user=user, purpose=purpose, consumed_at__isnull=True
        ).update(consumed_at=timezone.now())
        # secrets, not random — this is a credential.
        code = f"{secrets.randbelow(10 ** cls.CODE_LENGTH):0{cls.CODE_LENGTH}d}"
        row = cls.objects.create(
            user=user,
            code_hash=make_password(code),
            purpose=purpose,
            expires_at=timezone.now() + cls.TTL,
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
        """Check a submitted code, counting the attempt either way.

        The attempt is recorded before the comparison so a caller that crashes
        mid-check can't be used to get free guesses.
        """
        if not self.is_usable:
            return False
        self.attempts += 1
        self.save(update_fields=["attempts"])
        if not check_password((code or "").strip(), self.code_hash):
            return False
        self.consumed_at = timezone.now()
        self.save(update_fields=["consumed_at"])
        return True


class LaunchSignup(models.Model):
    """A pre-launch 'lock in my spot' lead from the coming-soon page.

    Stored, not mailed: the launch announcement goes out as a single batch
    when sign-ups open, so all we need here is the list.
    """

    PLATFORM_CHOICES = [
        ("footytips", "footytips (ESPN)"),
        ("afl", "Official AFL Tipping"),
        ("nrl", "Official NRL Tipping"),
        ("supercoach", "SuperCoach Tips"),
        ("itipfooty", "iTipFooty"),
        ("other", "Another platform"),
        ("none", "We don't run a tipping comp yet"),
        ("na", "Prefer not to say"),
    ]

    name = models.CharField(max_length=120)
    email = models.EmailField(unique=True)
    current_platform = models.CharField(
        max_length=20, choices=PLATFORM_CHOICES, blank=True
    )
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["-created_at"]

    def __str__(self):
        return f"{self.name} <{self.email}>"


class BossInvite(models.Model):
    """One "tell the boss" note, and what became of it.

    The send used to be fire-and-forget: an email left the building and the
    member never learned whether their boss opened it, joined, or ignored it.
    That is the whole point of the feature — you are asking a colleague to start
    something — so the note is now a tracked object with three states the sender
    can watch move.

    Matched to the boss by email address rather than by a token in a link. A
    boss who ignores the link and signs up from the homepage a week later should
    still close the loop, and the address is the only thing both sides agree on.
    """

    STATUS_SENT = "sent"
    STATUS_JOINED = "joined"
    STATUS_COMPLETE = "complete"
    STATUS_CHOICES = [
        (STATUS_SENT, "Note sent"),
        (STATUS_JOINED, "Boss signed up"),
        (STATUS_COMPLETE, "Group created"),
    ]

    sender = models.ForeignKey(
        "accounts.User", on_delete=models.CASCADE, related_name="boss_invites",
    )
    boss_name = models.CharField(max_length=80, blank=True)
    boss_email = models.EmailField()
    # Stored so the sender can see exactly what was sent on their behalf —
    # a note written in your name that you cannot read is a strange thing to ask
    # someone to trust.
    subject = models.CharField(max_length=200, blank=True)
    body_preview = models.TextField(blank=True)

    status = models.CharField(max_length=10, choices=STATUS_CHOICES, default=STATUS_SENT)
    sent_at = models.DateTimeField(default=timezone.now)
    joined_at = models.DateTimeField(null=True, blank=True)
    completed_at = models.DateTimeField(null=True, blank=True)

    boss_user = models.ForeignKey(
        "accounts.User", on_delete=models.SET_NULL, null=True, blank=True,
        related_name="boss_invites_received",
    )
    org = models.ForeignKey(
        "orgs.Organisation", on_delete=models.SET_NULL, null=True, blank=True,
        related_name="from_boss_invites",
    )

    class Meta:
        ordering = ["-sent_at"]
        indexes = [models.Index(fields=["boss_email", "status"])]

    def __str__(self):
        return f"{self.sender} → {self.boss_email} ({self.status})"

    @property
    def step(self) -> int:
        """1-3, for the progress rail."""
        return {self.STATUS_SENT: 1, self.STATUS_JOINED: 2, self.STATUS_COMPLETE: 3}[self.status]
