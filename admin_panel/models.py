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


class PageEdit(models.Model):
    """One block of wording on one page, rewritten by an admin.

    HOW A BLOCK IS IDENTIFIED
    -------------------------
    Not by position. An edit keyed on "the fourth paragraph" moves onto a
    different paragraph the moment a developer adds one above it, and silently
    rewrites the wrong sentence — which is worse than losing the edit.

    So the key is built from the wording the edit replaced: the tag name plus a
    hash of that element's original inner HTML (see `pagetext.block_key`). An
    edit therefore survives the page being reordered, sections being added
    around it, and the element moving anywhere on the page. What it does not
    survive is somebody changing that original wording in the template — and
    that is the point, because at that moment nobody can honestly say whether
    the admin's rewrite still means what they wanted it to. The edit stops
    applying, the original shows, and the manage page marks it stale rather
    than pretending.

    `original_html` is kept alongside for exactly that: so the manage page can
    show what was replaced, and so a stale edit can still be read back.
    """

    KIND_TEXT = "text"
    KIND_IMAGE = "image"
    KIND_CHOICES = [(KIND_TEXT, "Wording"), (KIND_IMAGE, "Image")]

    # A key from admin_panel.pages.PAGES. Not an FK — the registry is code, not
    # rows, and an edit for a page that has since been retired should sit
    # harmlessly in the table rather than block a deploy.
    page = models.CharField(max_length=40)
    block_key = models.CharField(max_length=64)
    kind = models.CharField(max_length=10, choices=KIND_CHOICES, default=KIND_TEXT)

    # What the admin wants shown. For KIND_TEXT this is the element's new inner
    # HTML; for KIND_IMAGE it is empty and `image` carries the replacement.
    html = models.TextField(blank=True)
    image = models.ImageField(upload_to="page_edits/", blank=True, null=True)

    # What was there before, so the manage page can show the change and so a
    # stale edit is still readable.
    original_html = models.TextField(blank=True)

    # Stamped every time the rewriter actually uses this edit. Null means it
    # has never matched since it was saved — the wording it was made against
    # is gone, and the page is showing the original.
    last_applied_at = models.DateTimeField(null=True, blank=True)

    created_at = models.DateTimeField(default=timezone.now)
    updated_at = models.DateTimeField(auto_now=True)
    updated_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, blank=True,
        related_name="page_edits",
    )

    class Meta:
        ordering = ["page", "block_key"]
        constraints = [
            models.UniqueConstraint(
                fields=["page", "block_key"], name="uniq_page_edit_block",
            ),
        ]
        indexes = [models.Index(fields=["page"])]

    def __str__(self):
        return f"{self.page}:{self.block_key}"

    @property
    def is_live(self) -> bool:
        """Whether this edit is currently showing on the page.

        False means the template's own wording has changed since the edit was
        made, so the key no longer matches anything and readers are seeing the
        original. See the class docstring for why that is the safe answer.
        """
        return self.last_applied_at is not None

    @property
    def replacement(self) -> str:
        if self.kind == self.KIND_IMAGE:
            return self.image.url if self.image else ""
        return self.html
