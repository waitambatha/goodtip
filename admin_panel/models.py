from django.conf import settings
from django.db import models
from django.utils import timezone
from django.utils.text import slugify


class SeoFieldsMixin(models.Model):
    """The fields an SEO team edits, on anything that has a public URL.

    WHY A MIXIN AND NOT TWO COPIES
    ------------------------------
    A story and a marketing page need exactly the same seven things — a title
    for the tab, a description for the result, what a share preview says, which
    address is the real one, and whether to be indexed at all. Written twice
    they drift, and the half that drifts is always the one nobody is looking
    at. `admin_panel.seo` renders these into a response's <head> without caring
    which model they came from, which is only possible because the names match.

    EVERY FIELD IS AN OVERRIDE, NOT A VALUE.
    ---------------------------------------
    Blank means "work it out", and what it works out is already right for most
    pages: the headline is the meta title, the teaser is the description, the
    featured image is the share image. So an SEO team fills in the handful that
    need to differ from what a reader sees, and leaves the rest alone — rather
    than being handed a form of empty boxes that must all be filled before
    anything has a title at all.
    """

    meta_title = models.CharField(
        max_length=200, blank=True,
        help_text="The browser tab and the blue line in Google. Leave blank to "
                  "use the headline. Around 60 characters before it is cut off.",
    )
    meta_description = models.CharField(
        max_length=320, blank=True,
        help_text="The grey summary under the link in Google. Leave blank to "
                  "use the teaser. Around 155 characters before it is cut off.",
    )
    og_title = models.CharField(
        max_length=200, blank=True,
        help_text="The headline on a Facebook or LinkedIn share card. Leave "
                  "blank to use the meta title.",
    )
    og_description = models.CharField(
        max_length=320, blank=True,
        help_text="The text under it on the share card. Leave blank to use the "
                  "meta description.",
    )
    og_image = models.ImageField(
        upload_to="seo/", blank=True, null=True,
        help_text="The picture on the share card. 1200x630 reads best. Leave "
                  "blank to use the page's own image.",
    )
    canonical_url = models.URLField(
        blank=True,
        help_text="Only when this page duplicates another one. Points search "
                  "engines at the address that should be indexed instead.",
    )
    # Two booleans rather than one "index,follow" string. The string form is
    # what goes in the tag, but it is not what anybody is deciding: "should
    # this be in Google" and "should links from it pass on credit" are separate
    # questions, and a free-text field invites `noindex nofollow` (no comma,
    # silently half-ignored) and `none` (valid, and means both, and nobody
    # knows that).
    robots_index = models.BooleanField(
        default=True, help_text="Uncheck to keep this page out of Google.",
    )
    robots_follow = models.BooleanField(
        default=True, help_text="Uncheck to stop links on this page passing on ranking.",
    )

    class Meta:
        abstract = True

    @property
    def robots_directive(self) -> str:
        """What goes in <meta name="robots">."""
        return ",".join([
            "index" if self.robots_index else "noindex",
            "follow" if self.robots_follow else "nofollow",
        ])

    @property
    def is_indexable(self) -> bool:
        """Whether this belongs in the XML sitemap.

        A canonical pointing somewhere else says "the other address is the real
        one", so listing this one in the sitemap would be asking for exactly
        what the canonical asks against.
        """
        return self.robots_index and not self.canonical_url


class LivePostManager(models.Manager):
    """Published AND due.

    Scheduling only works if there is exactly one definition of "a reader can
    see this", and it is applied everywhere. Before this there were three
    copies of `filter(is_published=True)` — the list, the detail page and the
    dashboard — and adding a fourth condition to two of them would have meant a
    scheduled story that was hidden from the list and served happily to anyone
    who guessed its address.
    """

    def get_queryset(self):
        return super().get_queryset().filter(
            is_published=True, published_at__lte=timezone.now(),
        )


class NewsPost(SeoFieldsMixin, models.Model):
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
    # EDITABLE, but not free-for-all. See `save` for what happens to the old
    # address when this changes.
    slug = models.SlugField(
        max_length=220, unique=True, blank=True,
        help_text="The last part of the story's address. Changing it leaves a "
                  "redirect behind so links already shared keep working.",
    )
    # ONE STORY, SEVERAL CODES.
    #
    # `tag` was a single choice, and a piece about both AFLW and NRLW had to
    # pick one and be wrong on the other — which also meant it was missing from
    # one of the two tag filters readers use to find it. Tags are a list now.
    #
    # `tag` is kept, not dropped: it is a NOT NULL column with data in it, the
    # news list's ?code= filter and a handful of templates still read
    # get_tag_display, and a column removal is the one migration that cannot be
    # walked back on a live database. It is maintained as the FIRST of `tags`
    # (see `save`) so anything still reading it reads the primary tag, and
    # nothing has to be found and changed in the same breath as this feature.
    tag = models.CharField(max_length=10, choices=TAG_CHOICES, default="NEWS")
    tags = models.JSONField(
        default=list, blank=True,
        help_text="Every code this story is about. A piece on AFLW and NRLW "
                  "shows under both.",
    )
    excerpt = models.TextField(blank=True, help_text="Short teaser shown under the headline.")
    # Rich-formatted teaser, same split as title/title_html: `excerpt` keeps the
    # plain-text version because the teaser is reused where markup cannot go —
    # the OG/meta description, the announcement email, and the `truncatechars`
    # cards on the dashboard and news list.
    excerpt_html = models.TextField(blank=True)
    body = models.TextField(blank=True, help_text="Rich HTML from the story editor.")
    image = models.ImageField(upload_to="news/", blank=True, null=True)
    # Read aloud by a screen reader, and shown if the file 404s. Its own field
    # rather than reusing the headline: "what this picture shows" and "what
    # this story is called" are different sentences, and search engines read
    # the first one as a description of the image.
    image_alt = models.CharField(
        max_length=200, blank=True,
        help_text="What the picture shows, for screen readers and search engines.",
    )
    # Deprecated. Every story now lives at its own auto-generated /news/<slug>/
    # URL, so "read the original elsewhere" had nowhere sensible to point, and
    # where a story came from is recorded in `sources` instead. The column stays
    # so the URLs already saved against old posts are not destroyed.
    link_url = models.URLField(blank=True, help_text="Deprecated, use `sources`.")
    # Optional citations shown under the story: [{"label": "...", "url": "..."}, ...].
    sources = models.JSONField(default=list, blank=True)
    is_published = models.BooleanField(default=True)
    # BACKDATING AND SCHEDULING ARE THE SAME FIELD.
    #
    # It was set to `timezone.now()` at creation and never shown, so every
    # story was published "today" whatever it was about — a piece written up
    # from last weekend's games sat above this weekend's on the list. It is
    # editable now, and the readers' queries filter on it (see
    # `PublishedPostManager`), which makes one field do both jobs: a date in
    # the past is a backdated story that sorts where it belongs, and a date in
    # the future is a scheduled one that appears on its own without anybody
    # having to be at a keyboard.
    #
    # No cron. A scheduled post is invisible because every reader-facing query
    # asks for `published_at <= now`, so the moment arrives on its own; a job
    # that flipped a flag could be late, could double-fire, and would be one
    # more thing to install on two servers.
    published_at = models.DateTimeField(
        default=timezone.now,
        help_text="When this story counts as published. A date in the past "
                  "backdates it; a date in the future holds it back until then.",
    )
    # When this post was emailed to members. Emailing is a deliberate, separate
    # action from publishing — otherwise toggling publish off and on would mail
    # everyone again — and the stamp makes it once-only.
    announced_at = models.DateTimeField(null=True, blank=True)
    created_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, blank=True,
        related_name="news_posts",
    )

    objects = models.Manager()
    # Everything a reader is allowed to see, at this moment. Used by every
    # public view and by the sitemap, so "published" means one thing in one
    # place — see `live_only` for why that matters more than it sounds.
    live = LivePostManager()

    class Meta:
        ordering = ["-published_at"]

    def __str__(self):
        return self.title

    def get_absolute_url(self) -> str:
        from django.urls import reverse

        return reverse("news_detail", args=[self.slug])

    # ---- tags -------------------------------------------------------------

    @property
    def tag_list(self) -> list:
        """The codes on this story, cleaned. Never empty.

        Reads through `tags` but falls back to the old single `tag`, because
        rows written before the migration ran — and any written by something
        that still only knows about `tag` — have to keep showing their code.
        """
        chosen = [t for t in (self.tags or []) if t in dict(self.TAG_CHOICES)]
        return chosen or ([self.tag] if self.tag else [])

    @property
    def tag_labels(self) -> list:
        labels = dict(self.TAG_CHOICES)
        return [labels.get(t, t) for t in self.tag_list]

    @property
    def is_scheduled(self) -> bool:
        """Published, but not yet. The editor says so rather than showing a
        green Published dot against something no reader can reach."""
        return self.is_published and self.published_at > timezone.now()

    @property
    def is_live(self) -> bool:
        return self.is_published and self.published_at <= timezone.now()

    #: Match-day photographs the site already ships, split by code. Used when a
    #: story has no picture of its own — see `fallback_scene`.
    _SCENES = {
        "AFL":  ["afl-goal-posts.jpg", "afl-training.jpg", "afl-posts-mcg.jpg",
                 "mcg-match.jpg", "afl-ground.jpg"],
        "NRL":  ["nrl-ground-dusk.jpg", "nrl-player-fence.jpg", "nrl-goal-posts.jpg",
                 "nrl-players-fans.jpg", "nrl-scoreboard.jpg"],
        None:   ["stadium-lights-grass.jpg", "aussie-crowd-flag.jpg",
                 "mcg-stadium.jpg", "stadium-panorama.jpg"],
    }

    @property
    def fallback_scene(self) -> str:
        """A photograph to stand in when the story has none of its own.

        The reader page has always done this — a story with no picture gets the
        site's own match-day shots rather than a flat green panel. The cards on
        the news list did not, so a list of pictureless stories read as a page
        that had failed to load rather than as a list of stories.

        PICKED BY CODE, so an NRL piece does not get a photograph of the MCG:
        AFL and AFLW draw from the AFL set, NRL and NRLW from the league set,
        and anything filed under News alone gets a neutral stadium.

        STABLE PER STORY, because it keys off the primary key rather than
        random. A card that showed a different photograph on every page load
        would read as broken in a different way — and the same story has to
        look the same on the list, on the dashboard and in the "more from
        GoodTip" row at the foot of another story.
        """
        code = (self.tag_list or [None])[0]
        if code in ("AFL", "AFLW"):
            pool = self._SCENES["AFL"]
        elif code in ("NRL", "NRLW"):
            pool = self._SCENES["NRL"]
        else:
            pool = self._SCENES[None]
        return f"img/scenes/{pool[(self.pk or 0) % len(pool)]}"

    def save(self, *args, **kwargs):
        # The slug is generated from the headline only when there isn't one —
        # it is editable after that, but it does not MOVE on its own. A slug
        # that followed every tweak of a headline would break every link
        # already shared, silently, at the moment somebody fixed a typo.
        if not self.slug:
            self.slug = self._make_unique_slug()
        # Keep the retired single-tag column pointing at the primary tag, so
        # anything still reading `tag` or `get_tag_display` reads something
        # true. See the field comment.
        primary = self.tag_list
        if primary:
            self.tag = primary[0]
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
    # Alt text for a KIND_IMAGE replacement. Swapping a photograph used to keep
    # the original's alt attribute, so every replaced picture on the site was
    # described as whatever the picture before it showed — worse than an empty
    # alt, because it reads as correct. Blank leaves the original's alt alone.
    image_alt = models.CharField(
        max_length=200, blank=True,
        help_text="What the new picture shows, for screen readers and search engines.",
    )

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


class PageSeo(SeoFieldsMixin, models.Model):
    """The SEO settings for one page of the site.

    ONE ROW PER PAGE, CREATED ON DEMAND.
    ------------------------------------
    Keyed on a key from `admin_panel.pages` — the same registry the wording
    editor uses, and for the same reason: the pages are code, not rows, so a
    page that is retired leaves its settings sitting harmlessly in the table
    instead of holding up a deploy. A page with no row here is a page with no
    overrides, which is the normal state and costs nothing.

    WHY THE PATH IS HERE
    --------------------
    `path_override` is the "editable URL slug, not permanently locked to the
    page title" of the brief. A marketing page's address is a route in
    `goodtip/urls.py`, so it cannot be a slug field the way a story's is
    without something that knows about these rows — which is what
    `admin_panel.seo.serve_override` is, called from the 404 fallback rather
    than mounted as a URL pattern (see its docstring for why that distinction
    matters more than it looks). Setting one serves the page at the new address
    AND sends the old one there permanently, so nothing that was already linked
    or indexed is dropped on the floor.
    """

    page = models.CharField(max_length=40, unique=True)

    path_override = models.CharField(
        max_length=200, blank=True,
        help_text="Serve this page at a different address, e.g. /why-goodtip/. "
                  "The old address redirects here, so nothing already shared "
                  "breaks. Leave blank to keep the built-in one.",
    )

    updated_at = models.DateTimeField(auto_now=True)
    updated_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, blank=True,
        related_name="page_seo_edits",
    )

    class Meta:
        ordering = ["page"]
        verbose_name = "page SEO"
        verbose_name_plural = "page SEO"

    def __str__(self):
        return f"SEO: {self.page}"

    @property
    def is_customised(self) -> bool:
        """Whether this row is saying anything at all.

        The editor saves a row for a page as soon as it is opened and saved,
        even with every box left empty, so "has a row" is not the same as "has
        settings". The index needs the second one.
        """
        return any([
            self.meta_title, self.meta_description, self.og_title,
            self.og_description, self.og_image, self.canonical_url,
            self.path_override,
        ]) or not self.robots_index or not self.robots_follow


class Redirect(models.Model):
    """An old address, and where it goes now.

    WHY NOT django.contrib.redirects
    --------------------------------
    That app is keyed on `django.contrib.sites`, which this project does not
    install and should not: a SITE_ID is a per-environment constant, and the
    one thing the two environments here must never share is a setting that
    silently makes staging behave as production. It also has no way to answer
    "is this redirect actually being used", which is the question that decides
    whether one can ever be deleted.

    ON A 404, NOT BEFORE IT
    -----------------------
    Matched in `process_response` when the response is a 404, not on the way
    in. A redirect table consulted on every request is a database read on every
    request, including every one of the thousands that resolve perfectly well;
    consulted only when routing has already failed, it costs nothing until it
    is needed. It also means a redirect can never shadow a real page — put one
    in for an address that later becomes a live route, and the live route wins,
    which is the safe direction.
    """

    old_path = models.CharField(
        max_length=200, unique=True, db_index=True,
        help_text="The address that no longer exists, e.g. /old-pricing/. "
                  "Start it with a slash; leave the domain off.",
    )
    new_path = models.CharField(
        max_length=400,
        help_text="Where it should go instead. A path like /pricing/, or a "
                  "full https:// address for somewhere off the site.",
    )
    # 301 tells search engines to move the ranking and stop asking; 302 says
    # this is temporary. The difference is unrecoverable in practice — a 301 is
    # cached hard by browsers — so it is a deliberate choice rather than a
    # default nobody sees.
    is_permanent = models.BooleanField(
        default=True,
        help_text="Permanent (301) moves the search ranking to the new address. "
                  "Uncheck for a temporary (302) move.",
    )
    note = models.CharField(
        max_length=200, blank=True,
        help_text="Why this exists, for whoever finds it in a year.",
    )

    # So a redirect can be retired on evidence rather than on a guess.
    hits = models.PositiveIntegerField(default=0)
    last_hit_at = models.DateTimeField(null=True, blank=True)

    created_at = models.DateTimeField(default=timezone.now)
    created_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, blank=True,
        related_name="redirects",
    )

    class Meta:
        ordering = ["old_path"]

    def __str__(self):
        return f"{self.old_path} → {self.new_path}"

    @property
    def status_code(self) -> int:
        return 301 if self.is_permanent else 302

    @staticmethod
    def normalise(path: str) -> str:
        """A path as it will be compared: leading slash, no query, no host.

        Typed by hand into a form, so "pricing", "/pricing", "/pricing/" and a
        pasted "https://goodtip.com.au/pricing/" all mean the same thing and
        all get typed. Normalising on the way in means the lookup on the way
        out is a single exact match rather than four.
        """
        path = (path or "").strip()
        if not path:
            return ""
        for prefix in ("https://", "http://"):
            if path.lower().startswith(prefix):
                rest = path[len(prefix):]
                path = "/" + rest.partition("/")[2]
                break
        path = path.split("?", 1)[0].split("#", 1)[0]
        if not path.startswith("/"):
            path = "/" + path
        return path
