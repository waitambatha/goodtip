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


class PageText(models.Model):
    """One editable piece of copy on a public page.

    WHY THIS EXISTS. The client read the site, wanted a word changed, and had
    to ask us — which meant a developer, a commit, and a deploy, to alter a
    sentence. That is the wrong shape for copy. It is the right shape for
    layout, and the distinction is what this model is built around: the
    TEMPLATE still owns the structure and the original words, and this table
    only ever holds an override.

    Consequences of that choice, all deliberate:

    * An empty table renders the site exactly as it is today. Nothing has to
      be seeded, and a lost database costs no copy.
    * Deleting a row is "put it back how it was", which is the undo people
      actually ask for.
    * A slot that is removed from a template simply stops being read. No
      orphan cleanup, no broken page.

    The value is stored as plain text and escaped on output. Letting the
    client paste HTML here would hand anyone with staff access a stored-XSS
    primitive on the public site, in exchange for formatting that the layout
    already provides.
    """

    page = models.CharField(max_length=40)
    # Dotted, e.g. "hero.title". Namespacing by hand rather than by nesting
    # tables: the editor groups on the prefix, and a two-level model would
    # have bought nothing except joins.
    key = models.CharField(max_length=80)
    value = models.TextField(blank=True)
    updated_at = models.DateTimeField(auto_now=True)
    updated_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.SET_NULL,
        related_name="page_texts_edited", null=True, blank=True,
    )

    class Meta:
        ordering = ["page", "key"]
        constraints = [
            models.UniqueConstraint(fields=["page", "key"], name="uniq_page_text_slot"),
        ]
        verbose_name = "page text"
        verbose_name_plural = "page text"

    def __str__(self):
        return f"{self.page}.{self.key}"


class PageMedia(models.Model):
    """An image or video the client put on a public page, or wants gone.

    Two jobs in one table, which is why `slot` may be blank:

    * A slot-filling upload REPLACES the picture a template already points at
      — same position, same crop, different photograph.
    * A slot-less upload is a file in the library, uploaded so it can be
      pointed at from somewhere else.

    Removing a template's own built-in image is done with `is_hidden` rather
    than by deleting anything, because there is nothing to delete: the
    original lives in static files, not here. Hiding it is the only honest
    representation of "take that picture off the page".
    """

    KIND_IMAGE = "image"
    KIND_VIDEO = "video"
    KIND_CHOICES = [(KIND_IMAGE, "Image"), (KIND_VIDEO, "Video")]

    page = models.CharField(max_length=40)
    slot = models.CharField(max_length=80, blank=True)
    kind = models.CharField(max_length=6, choices=KIND_CHOICES, default=KIND_IMAGE)
    file = models.FileField(upload_to="pages/")
    # Not optional in spirit even though it is in the schema: an empty alt on
    # a decorative image is a real answer, but an empty alt on a photograph
    # that carries meaning is a page that excludes people.
    alt = models.CharField(max_length=200, blank=True)
    is_hidden = models.BooleanField(default=False)
    sort_order = models.PositiveIntegerField(default=0)
    uploaded_at = models.DateTimeField(auto_now_add=True)
    uploaded_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.SET_NULL,
        related_name="page_media_uploaded", null=True, blank=True,
    )

    class Meta:
        ordering = ["page", "slot", "sort_order", "-uploaded_at"]
        verbose_name = "page media"
        verbose_name_plural = "page media"

    def __str__(self):
        return f"{self.page}/{self.slot or 'library'} — {self.file.name}"

    @property
    def is_video(self) -> bool:
        return self.kind == self.KIND_VIDEO
