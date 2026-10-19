"""Keep live and staging's news posts the same, in both directions — client,
15 Oct 2026: a post written on either side should reach the other, right
away, "so we will use the staging again as a defacto backup".

RUNS ON STAGING ONLY, same safety pattern as sync_live_backup (see that
command's own docstring): refuses anywhere else, opens live under its own
alias for one process, and that connection string lives only in this
command's own systemd timer, never in staging's .env.

UNLIKE sync_live_backup, THIS ONE WRITES TO LIVE. That is a materially
bigger risk — a bug here touches the real public site with nobody
watching — which is why it was tested with a single manual run, checked
by hand, before ever going on a timer, same as the account backup was.

MATCHING AND CONFLICTS. The two databases share nothing but a post's own
`slug` (unique on each side, never shared otherwise), so that's the key.
Whichever side's `updated_at` is newer wins and is copied onto the other —
`published_at` can't do that job, it's deliberately backdatable. A post
missing entirely from one side is either genuinely new there, or was
deleted there on purpose: NewsPostTombstone (admin_panel.models) is what
tells the two apart, written by NewsPost's own post_delete signal, so a
deletion never gets silently undone by the next run — see that model's
docstring, and the client's own words: "so we make sure the blogs and
news are safe, kinda like a security measure."

FILES, NOT JUST ROWS. NewsPost.image/og_image and every NewsMedia.file are
on disk under each checkout's own MEDIA_ROOT. Both checkouts sit on the
same server, so copying a post also copies its files directly between the
two paths — no upload, no network transfer, just shutil.
"""

import os
import shutil
from pathlib import Path

import dj_database_url
from django.conf import settings
from django.core.management.base import BaseCommand, CommandError
from django.db import connection, connections, transaction

from accounts.models import User
from admin_panel.models import NewsMedia, NewsPost, NewsPostTombstone

LIVE_ALIAS = "live_news_sync"
PRODUCTION_DB_NAMES = {"goodtip_db"}


class Command(BaseCommand):
    help = "Merge news posts between live and staging, newest edit wins, deletions stay local. Staging only."

    def handle(self, *args, **options):
        self._refuse_unless_staging()
        self._open_live_connection()
        try:
            with transaction.atomic(), transaction.atomic(using=LIVE_ALIAS):
                pushed, pulled, skipped = self._merge()
        finally:
            self._close_live_connection()
        self.stdout.write(f"staging -> live: {pushed}, live -> staging: {pulled}, left deleted: {skipped}")

    # ---- safety ---------------------------------------------------------

    def _refuse_unless_staging(self):
        if not getattr(settings, "IS_STAGING", False):
            raise CommandError(
                "GOODTIP_ENV is not 'staging' — this merges live and staging's "
                "news and will not run anywhere else."
            )
        if connection.settings_dict.get("NAME") in PRODUCTION_DB_NAMES:
            raise CommandError(
                "Refusing: this process's own database is named goodtip_db — "
                "that is live's name."
            )
        if not os.environ.get("LIVE_NEWS_DATABASE_URL"):
            raise CommandError(
                "LIVE_NEWS_DATABASE_URL is not set in this process's environment "
                "— set it on this command's own systemd unit, not in .env."
            )
        if not os.environ.get("LIVE_MEDIA_ROOT"):
            raise CommandError("LIVE_MEDIA_ROOT is not set.")

    def _open_live_connection(self):
        live_url = os.environ["LIVE_NEWS_DATABASE_URL"]
        # dj_database_url.parse(), not .config() — see sync_live_backup's own
        # note on why .config(default=...) silently ignores that default
        # whenever DATABASE_URL is already set, which it always is here.
        config = dj_database_url.parse(live_url, conn_max_age=0)
        # No read-only option this time — this connection has to write.
        config.setdefault("ATOMIC_REQUESTS", False)
        config.setdefault("AUTOCOMMIT", True)
        config.setdefault("CONN_HEALTH_CHECKS", False)
        config.setdefault("TIME_ZONE", None)
        config.setdefault("OPTIONS", {})
        config.setdefault("TEST", {})
        connections.databases[LIVE_ALIAS] = config
        if connections[LIVE_ALIAS].settings_dict.get("NAME") not in PRODUCTION_DB_NAMES:
            self._close_live_connection()
            raise CommandError(
                "LIVE_NEWS_DATABASE_URL does not point at a database named "
                "goodtip_db — refusing, in case this is pointed at staging's "
                "own database by mistake."
            )

    def _close_live_connection(self):
        if LIVE_ALIAS in connections.databases:
            connections[LIVE_ALIAS].close()
            del connections.databases[LIVE_ALIAS]

    # ---- the merge --------------------------------------------------------

    def _merge(self):
        live_root = Path(os.environ["LIVE_MEDIA_ROOT"])
        staging_root = Path(settings.MEDIA_ROOT)

        staging_posts = {p.slug: p for p in NewsPost.objects.all()}
        live_posts = {p.slug: p for p in NewsPost.objects.using(LIVE_ALIAS).all()}
        staging_tombstones = set(NewsPostTombstone.objects.values_list("slug", flat=True))
        live_tombstones = set(
            NewsPostTombstone.objects.using(LIVE_ALIAS).values_list("slug", flat=True)
        )

        pushed = pulled = skipped = 0
        for slug in set(staging_posts) | set(live_posts):
            s = staging_posts.get(slug)
            l = live_posts.get(slug)

            if s and l:
                if s.updated_at > l.updated_at:
                    self._copy_post(s, LIVE_ALIAS, staging_root, live_root)
                    pushed += 1
                elif l.updated_at > s.updated_at:
                    self._copy_post(l, "default", live_root, staging_root)
                    pulled += 1
                # equal: already in step, nothing to do.
            elif s and not l:
                if slug in live_tombstones:
                    # Deleted on live on purpose — stays deleted there.
                    # Staging keeps its copy as the backup; that is the
                    # whole point, not a bug to fix.
                    skipped += 1
                else:
                    self._copy_post(s, LIVE_ALIAS, staging_root, live_root)
                    pushed += 1
            elif l and not s:
                if slug in staging_tombstones:
                    skipped += 1
                else:
                    self._copy_post(l, "default", live_root, staging_root)
                    pulled += 1
        return pushed, pulled, skipped

    # ---- copying one post, with its media and files ------------------------

    def _match_user_by_email(self, email, dest_alias):
        # .only("id") — live's own accounts_user table doesn't carry
        # origin/live_id at all (deliberately: that split is staging-only,
        # see goodtip.origin), and all this needs is something with a valid
        # .pk to assign as created_by. A plain .filter().first() selects
        # every column on the model and breaks the same way
        # sync_live_backup's did before it learned to defer.
        if not email:
            return None
        return User.objects.using(dest_alias).only("id").filter(email=email).first()

    def _copy_file(self, field_file, src_root, dest_root):
        """Copy one FieldFile's bytes across, same relative path either side."""
        if not field_file:
            return
        src_path = src_root / field_file.name
        dest_path = dest_root / field_file.name
        if not src_path.exists():
            return
        dest_path.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(src_path, dest_path)

    def _author_email(self, src):
        """The author's email, read with the same care _match_user_by_email
        takes — `src.created_by.email` would lazy-load the FK with no defer
        at all, selecting origin/live_id on whichever side `src` came from
        and failing on live's own accounts_user exactly the same way."""
        if not src.created_by_id:
            return None
        row = User.objects.using(src._state.db).only("email").filter(pk=src.created_by_id).first()
        return row.email if row else None

    def _copy_post(self, src, dest_alias, src_root, dest_root):
        """Write `src` (from whichever side just won) onto `dest_alias`."""
        created_by = self._match_user_by_email(self._author_email(src), dest_alias)

        for f in (src.image, src.og_image):
            self._copy_file(f, src_root, dest_root)

        dest, _ = NewsPost.objects.using(dest_alias).update_or_create(
            slug=src.slug,
            defaults=dict(
                title=src.title, title_html=src.title_html,
                tag=src.tag, tags=src.tags,
                excerpt=src.excerpt, excerpt_html=src.excerpt_html, body=src.body,
                image=src.image.name if src.image else "",
                image_alt=src.image_alt,
                link_url=src.link_url, sources=src.sources,
                is_published=src.is_published, published_at=src.published_at,
                announced_at=src.announced_at, created_by=created_by,
                meta_title=src.meta_title, meta_description=src.meta_description,
                og_title=src.og_title, og_description=src.og_description,
                og_image=src.og_image.name if src.og_image else "",
                canonical_url=src.canonical_url,
                robots_index=src.robots_index, robots_follow=src.robots_follow,
            ),
        )
        # update_or_create's save() just bumped updated_at to now via
        # auto_now — .update() bypasses that, so both sides end up with the
        # SAME timestamp and the next run sees them as already in step
        # instead of ping-ponging the same post back and forth forever.
        NewsPost.objects.using(dest_alias).filter(pk=dest.pk).update(updated_at=src.updated_at)

        # The media set is replaced wholesale rather than merged row by row —
        # a post's pictures are edited as one unit with the post itself, not
        # independently, so "whichever side won" wins for all of them too.
        NewsMedia.objects.using(dest_alias).filter(post=dest).delete()
        src_media = src._state.db
        for m in NewsMedia.objects.using(src_media).filter(post=src).order_by("order", "pk"):
            self._copy_file(m.file, src_root, dest_root)
            NewsMedia.objects.using(dest_alias).create(
                post=dest, kind=m.kind, file=m.file.name if m.file else "",
                alt=m.alt, order=m.order,
            )
