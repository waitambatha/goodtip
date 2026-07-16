from django.contrib import admin

from .models import NewsPost


@admin.register(NewsPost)
class NewsPostAdmin(admin.ModelAdmin):
    list_display = ("title", "tag", "is_published", "published_at", "created_by")
    list_filter = ("tag", "is_published")
    search_fields = ("title", "excerpt", "body")
