from django.contrib.auth.models import AbstractUser, BaseUserManager
from django.db import models


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

    USERNAME_FIELD = "email"
    REQUIRED_FIELDS = ["display_name"]

    objects = UserManager()

    def __str__(self):
        return self.display_name or self.email


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
