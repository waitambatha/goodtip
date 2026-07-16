from django.conf import settings
from django.db import models
from django.utils import timezone


class NewsPost(models.Model):
    """News / blog posts shown on the member dashboard.

    Authored only by the platform super admin (is_superuser) from the manage
    area — group admins never see the authoring UI.
    """

    TAG_CHOICES = [
        ("AFL", "AFL"),
        ("AFLW", "AFLW"),
        ("NRL", "NRL"),
        ("NRLW", "NRLW"),
        ("NEWS", "News"),
        ("BLOG", "Blog"),
    ]

    title = models.CharField(max_length=200)
    tag = models.CharField(max_length=10, choices=TAG_CHOICES, default="NEWS")
    excerpt = models.TextField(blank=True, help_text="Short teaser shown under the headline.")
    body = models.TextField(blank=True)
    image = models.ImageField(upload_to="news/", blank=True, null=True)
    link_url = models.URLField(blank=True, help_text="Optional: send readers to a full story elsewhere.")
    is_published = models.BooleanField(default=True)
    published_at = models.DateTimeField(default=timezone.now)
    created_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, blank=True,
        related_name="news_posts",
    )

    class Meta:
        ordering = ["-published_at"]

    def __str__(self):
        return self.title
