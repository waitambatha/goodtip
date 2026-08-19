from django.conf import settings
from django.db import models
from django.utils import timezone
from django.utils.text import slugify


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
    # Rich-formatted headline (bold/colour/font size etc) from the story editor.
    # `title` stays the plain-text version — derived from this on save — and is
    # what's used for the URL slug, page <title>, email subject and OG tags.
    title_html = models.TextField(blank=True)
    slug = models.SlugField(max_length=220, unique=True, blank=True)
    tag = models.CharField(max_length=10, choices=TAG_CHOICES, default="NEWS")
    excerpt = models.TextField(blank=True, help_text="Short teaser shown under the headline.")
    body = models.TextField(blank=True, help_text="Rich HTML from the story editor.")
    image = models.ImageField(upload_to="news/", blank=True, null=True)
    link_url = models.URLField(blank=True, help_text="Optional: send readers to a full story elsewhere.")
    # Optional citations shown under the story: [{"label": "...", "url": "..."}, ...].
    sources = models.JSONField(default=list, blank=True)
    is_published = models.BooleanField(default=True)
    published_at = models.DateTimeField(default=timezone.now)
    # When this post was emailed to members. Emailing is a deliberate, separate
    # action from publishing — otherwise toggling publish off and on would mail
    # everyone again — and the stamp makes it once-only.
    announced_at = models.DateTimeField(null=True, blank=True)
    created_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, blank=True,
        related_name="news_posts",
    )

    class Meta:
        ordering = ["-published_at"]

    def __str__(self):
        return self.title

    def save(self, *args, **kwargs):
        # Generated once at creation and kept stable after that — a slug that
        # moved every time a headline was tweaked would break every link
        # already shared for this post.
        if not self.slug:
            self.slug = self._make_unique_slug()
        super().save(*args, **kwargs)

    def _make_unique_slug(self):
        base = slugify(self.title)[:200] or "post"
        slug = base
        suffix = 2
        while NewsPost.objects.filter(slug=slug).exclude(pk=self.pk).exists():
            slug = f"{base}-{suffix}"
            suffix += 1
        return slug


class Enquiry(models.Model):
    """A message from the public contact form.

    Before this the form was a mockup — `onsubmit="return false"`, no action,
    no field names, no CSRF — so every enquiry anyone ever typed into it was
    discarded on submit. Nothing reached an inbox and nothing was stored.

    Enquiries are kept here rather than only emailed because an email thread is
    not a record: it lives in one person's mailbox, it cannot be counted, and
    "did anyone answer this?" has no answer. The reply is stored alongside the
    question so the whole exchange is on one page.
    """

    STATUS_NEW = "new"
    STATUS_REPLIED = "replied"
    STATUS_CLOSED = "closed"
    STATUS_CHOICES = [
        (STATUS_NEW, "New"),
        (STATUS_REPLIED, "Replied"),
        (STATUS_CLOSED, "Closed"),
    ]

    # Mirrors the options in the public form's dropdown. Free text rather than
    # choices: the form is marketing copy and its wording will change, and an
    # old enquiry should keep the words the person actually picked.
    name = models.CharField(max_length=120)
    email = models.EmailField()
    organisation = models.CharField(max_length=160, blank=True)
    interest = models.CharField(max_length=120, blank=True)
    message = models.TextField()

    created_at = models.DateTimeField(default=timezone.now)
    # Where they were when they wrote it — the contact form is on five pages and
    # "pricing" versus "how it works" changes what they are likely asking.
    source_page = models.CharField(max_length=200, blank=True)

    status = models.CharField(max_length=10, choices=STATUS_CHOICES, default=STATUS_NEW)
    reply_body = models.TextField(blank=True)
    replied_at = models.DateTimeField(null=True, blank=True)
    replied_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, blank=True,
        related_name="enquiry_replies",
    )

    class Meta:
        ordering = ["-created_at"]
        verbose_name_plural = "enquiries"

    def __str__(self):
        return f"{self.name} <{self.email}>"

    @property
    def is_answered(self) -> bool:
        return self.status == self.STATUS_REPLIED
