"""The XML sitemap, built from the same two places everything else reads.

KEPT CURRENT BY CONSTRUCTION
----------------------------
The brief asks for a sitemap "auto generated and kept current as pages are
added". Nothing here is a list of URLs. The pages come from
`admin_panel.pages.public_pages()` — the registry the wording editor and the
SEO editor already work from, so a page added there appears here — and the
stories come from `NewsPost.live`, the same manager the public list uses. There
is no second list to remember to update, which is the only version of "kept
current" that survives contact with a busy month.

WHAT IS LEFT OUT, AND WHY
-------------------------
* Private pages. A sitemap is an invitation to crawl; the dashboard is behind a
  login and listing it would be advertising a wall.
* Anything an admin has set to noindex, or given a canonical pointing
  elsewhere. Asking Google to crawl a page while telling it not to index that
  page is a contradiction, and it is the SEO team's own setting that resolves
  it — see `SeoFieldsMixin.is_indexable`.
* Scheduled stories. `NewsPost.live` excludes them, so a story queued for next
  Tuesday is not announced to crawlers this Tuesday.
"""
from django.contrib.sitemaps import Sitemap
from django.urls import NoReverseMatch, reverse

from admin_panel import pages as page_registry
from admin_panel.models import NewsPost, PageSeo


class _PageSeoLookup:
    """Every per-page SEO row, read once per render rather than once per page."""

    def _seo_by_page(self) -> dict:
        return {row.page: row for row in PageSeo.objects.all()}


class StaticPageSitemap(_PageSeoLookup, Sitemap):
    changefreq = "weekly"

    def items(self):
        seo = self._seo_by_page()
        out = []
        for page in page_registry.public_pages():
            # A page needing an org id has no single public address, so there
            # is nothing to list. None of the public ones do today; the guard
            # is here because the registry is edited by hand and a page that
            # grows an argument must drop out of the sitemap rather than raise
            # NoReverseMatch on a crawler's request.
            if page.needs:
                continue
            row = seo.get(page.key)
            if row is not None and not row.is_indexable:
                continue
            try:
                location = row.path_override if (row and row.path_override) else reverse(page.view_name)
            except NoReverseMatch:
                continue
            out.append((page, location))
        return out

    def location(self, item):
        return item[1]

    def priority(self, item):
        # Sitemap calls this per item when it is a callable. The landing page
        # is the one to crawl first; everything else is level.
        return 1.0 if item[0].key == "home" else 0.7


class NewsSitemap(Sitemap):
    changefreq = "daily"
    priority = 0.6

    def items(self):
        return [p for p in NewsPost.live.all() if p.is_indexable]

    def lastmod(self, item):
        return item.published_at

    def location(self, item):
        return item.get_absolute_url()


SITEMAPS = {"pages": StaticPageSitemap, "news": NewsSitemap}
