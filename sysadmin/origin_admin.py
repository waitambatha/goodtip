"""Shared admin wiring for anything carrying goodtip.origin.OriginMixin.

Shows which records are real (from live) and which are testing (made on
staging), and gates deleting a real one behind
sysadmin.access.can_delete_real — see that function and
AdminAccess.can_delete_real for why this is checked on its own rather than
folded into the existing capability/full-access system.

Django's own bulk "Delete selected" action already calls
has_delete_permission(request, obj) for every object it would delete (see
admin.utils.get_deleted_objects), so overriding it here is enough to cover
both the single-record delete button and a multi-select bulk delete —
nothing extra to wire up for the list view's checkboxes.
"""

from django.contrib import admin
from django.utils.html import format_html

from goodtip.origin import ORIGIN_LIVE
from sysadmin import access


class OriginAdminMixin:
    @admin.display(description="Origin")
    def origin_badge(self, obj):
        if obj.origin == ORIGIN_LIVE:
            return format_html('<span style="color:#0F7B3E;font-weight:600;">Real</span>')
        return format_html('<span style="color:#8A6D00;font-weight:600;">Testing</span>')

    def get_list_display(self, request):
        return (*super().get_list_display(request), "origin_badge")

    def get_list_filter(self, request):
        return (*super().get_list_filter(request), "origin")

    def has_delete_permission(self, request, obj=None):
        if obj is not None and obj.origin == ORIGIN_LIVE and not access.can_delete_real(request.user):
            return False
        return super().has_delete_permission(request, obj)
