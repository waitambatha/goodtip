from django.contrib import admin

from .models import NewsPost, PageSeo, Redirect


@admin.register(NewsPost)
class NewsPostAdmin(admin.ModelAdmin):
    # `tag` rather than `tags`: it is kept in step with the first of `tags` by
    # NewsPost.save, and a JSONField renders here as a raw list. The real
    # editing surface is the story editor in the HQ — this table is the
    # fallback for a developer, not the screen anybody works in.
    list_display = ("title", "tag", "is_published", "published_at", "created_by")
    list_filter = ("tag", "is_published", "robots_index")
    search_fields = ("title", "excerpt", "body", "meta_title", "meta_description")


@admin.register(PageSeo)
class PageSeoAdmin(admin.ModelAdmin):
    list_display = ("page", "path_override", "meta_title", "robots_index", "updated_at")
    list_filter = ("robots_index", "robots_follow")
    search_fields = ("page", "meta_title", "meta_description")


@admin.register(Redirect)
class RedirectAdmin(admin.ModelAdmin):
    list_display = ("old_path", "new_path", "is_permanent", "hits", "last_hit_at")
    list_filter = ("is_permanent",)
    search_fields = ("old_path", "new_path", "note")
