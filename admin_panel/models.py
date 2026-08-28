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
    # Rich-formatted teaser, same split as title/title_html: `excerpt` keeps the
    # plain-text version because the teaser is reused where markup cannot go —
    # the OG/meta description, the announcement email, and the `truncatechars`
    # cards on the dashboard and news list.
    excerpt_html = models.TextField(blank=True)
    body = models.TextField(blank=True, help_text="Rich HTML from the story editor.")
    image = models.ImageField(upload_to="news/", blank=True, null=True)
    # Deprecated. Every story now lives at its own auto-generated /news/<slug>/
    # URL, so "read the original elsewhere" had nowhere sensible to point, and
    # where a story came from is recorded in `sources` instead. The column stays
    # so the URLs already saved against old posts are not destroyed.
    link_url = models.URLField(blank=True, help_text="Deprecated, use `sources`.")
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

    def get_absolute_url(self) -> str:
        from django.urls import reverse

        return reverse("news_detail", args=[self.slug])

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


class SiteContent(models.Model):
    """One editable slot on a public page — the super admin's override of it.

    The public templates are the source of truth for *what slots exist* and
    what they say by default; this table only holds the edits. That split is
    deliberate:

    * A fresh database renders the real site, not a page full of empty
      placeholders — so staging, a new dev checkout and a restored backup all
      look right without a fixture to load first.
    * Copy that has never been edited stays in version control where it can be
      reviewed in a diff, and an edit that turns out badly can be undone by
      deleting one row ("Reset to default" in the editor) rather than by
      remembering the old wording.

    The slots themselves are declared in admin_panel/site_blocks.py, which is
    what the editor renders and what the {% site_text %} family reads defaults
    from. A key with no row here has simply never been edited.
    """

    KIND_TEXT = "text"
    KIND_RICH = "rich"
    KIND_IMAGE = "image"
    KIND_VIDEO = "video"

    key = models.CharField(max_length=140, unique=True)
    # Plain text, and rich HTML, kept in separate columns rather than one:
    # switching a slot's kind in site_blocks.py must not silently reinterpret
    # stored markup as text (or worse, the other way round).
    text = models.TextField(blank=True)
    html = models.TextField(blank=True)
    image = models.ImageField(upload_to="site/", blank=True, null=True)
    video = models.FileField(upload_to="site/", blank=True, null=True)
    # Still frame shown before the clip plays (and instead of it on a metered
    # connection or with reduced motion on) — see static/js/gt-video.js.
    video_poster = models.ImageField(upload_to="site/", blank=True, null=True)
    alt_text = models.CharField(max_length=300, blank=True)

    updated_at = models.DateTimeField(auto_now=True)
    updated_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, blank=True,
        related_name="site_content_edits",
    )

    class Meta:
        ordering = ["key"]
        verbose_name = "site content block"
        verbose_name_plural = "site content"

    def __str__(self):
        return self.key

    @property
    def is_empty(self) -> bool:
        """True when nothing is actually overridden.

        The editor saves a row per submitted form, so a slot cleared back to
        blank leaves an empty row behind; treating that as "no override" is
        what makes clearing a field fall back to the template default instead
        of publishing an empty string to the live site.
        """
        return not (
            self.text or self.html or self.image or self.video
            or self.video_poster or self.alt_text
        )

    # -- cache -------------------------------------------------------------
    # The public home page reads ~40 slots. One query per slot per request is
    # the obvious way to make a CMS slower than the hard-coded page it
    # replaced, so the whole (small) table is loaded once and cached, and the
    # cache is dropped whenever a row changes.
    CACHE_KEY = "site_content_map_v1"

    @classmethod
    def map(cls):
        from django.core.cache import cache

        cached = cache.get(cls.CACHE_KEY)
        if cached is None:
            cached = {obj.key: obj for obj in cls.objects.all()}
            cache.set(cls.CACHE_KEY, cached, 60 * 60)
        return cached

    @classmethod
    def bust(cls):
        from django.core.cache import cache

        cache.delete(cls.CACHE_KEY)

    def save(self, *args, **kwargs):
        super().save(*args, **kwargs)
        self.bust()

    def delete(self, *args, **kwargs):
        super().delete(*args, **kwargs)
        self.bust()
