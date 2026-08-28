"""The Site content editor — the super admin's control over the public pages.

Bolted onto the default admin.site with get_urls() (see sysadmin/admin.py) for
the same reason the system report is: one control plane, and admin_view() gives
the login/staff check, CSRF and never-cache wrapper for free.

Not a ModelAdmin, on purpose. A changelist of 114 rows called
"home.hero.shot4" is a database table, not an editing surface — you cannot see
what you are changing and you cannot find the thing you meant to change. This
renders the slots grouped exactly as the page is laid out, with the real widget
for each kind: a line for a heading, a box for a paragraph, a thumbnail and a
file picker for a photo, a playable preview for a clip.
"""
from django.contrib import messages
from django.http import HttpResponseForbidden
from django.shortcuts import redirect, render
from django.urls import reverse
from django.utils import timezone

from .sanitize import sanitize_editor_html
from .site_blocks import PAGES, IMAGE, RICH, TEXT, VIDEO, page_by_slug


def _guard(request):
    """Site content is platform-level, so it is superuser-only.

    admin_view() already required an active staff user; org admins are staff
    for their own org's models and must not be able to rewrite the front page.
    """
    if not request.user.is_superuser:
        return HttpResponseForbidden()
    return None


def _context(request, **extra):
    from django.contrib.admin.sites import site

    return {**site.each_context(request), **extra}


def index(request):
    denied = _guard(request)
    if denied:
        return denied

    from .models import SiteContent

    overrides = SiteContent.map()
    cards = []
    for page in PAGES:
        keys = [b.key for g in page.groups for b in g.blocks]
        edited = [k for k in keys if k in overrides and not overrides[k].is_empty]
        touched = [overrides[k].updated_at for k in edited]
        cards.append({
            "page": page,
            "total": len(keys),
            "edited": len(edited),
            "last_edited": max(touched) if touched else None,
        })

    return render(request, "admin/site_content/index.html", _context(
        request,
        title="Site content",
        cards=cards,
    ))


def page(request, slug):
    denied = _guard(request)
    if denied:
        return denied

    from .models import SiteContent

    target = page_by_slug(slug)
    if target is None:
        messages.error(request, "That page has no editable content.")
        return redirect("admin:site_content")

    if request.method == "POST":
        changed, reset = _save(request, target)
        SiteContent.bust()
        if changed or reset:
            bits = []
            if changed:
                bits.append(f"{changed} block{'' if changed == 1 else 's'} updated")
            if reset:
                bits.append(f"{reset} put back to the default")
            messages.success(request, f"{target.label} saved — {', and '.join(bits)}.")
        else:
            messages.info(request, "Nothing changed.")
        return redirect("admin:site_content_page", slug=slug)

    overrides = SiteContent.map()
    groups = []
    for group in target.groups:
        rendered = []
        for block in group.blocks:
            row = overrides.get(block.key)
            has_override = bool(row and not row.is_empty)
            rendered.append({
                "block": block,
                "row": row,
                "is_edited": has_override,
                # What the field shows: the override if there is one, otherwise
                # the live default — so the editor is always a picture of what
                # the page currently says, and clearing a field is a visible,
                # obvious way to put the original back.
                "value": (row.html or row.text) if has_override else block.default,
                "is_text": block.kind == TEXT,
                "is_rich": block.kind == RICH,
                "is_image": block.kind == IMAGE,
                "is_video": block.kind == VIDEO,
            })
        groups.append({"group": group, "blocks": rendered})

    edited_count = sum(1 for g in groups for b in g["blocks"] if b["is_edited"])

    return render(request, "admin/site_content/page.html", _context(
        request,
        title=f"Site content: {target.label}",
        page=target,
        groups=groups,
        edited_count=edited_count,
        total_count=target.block_count,
        preview_url=_preview_url(target),
    ))


def _preview_url(target):
    from django.urls import NoReverseMatch

    try:
        return reverse(target.url_name)
    except NoReverseMatch:
        return "/"


def _save(request, target):
    """Apply one submitted form. Returns (updated, reset-to-default) counts."""
    from .models import SiteContent

    changed = reset = 0

    for group in target.groups:
        for block in group.blocks:
            key = block.key
            row = SiteContent.objects.filter(key=key).first()
            wants_reset = request.POST.get(f"reset_{key}") == "1"

            if block.kind in (TEXT, RICH):
                submitted = (request.POST.get(f"f_{key}") or "").strip()
                if block.kind == RICH:
                    submitted = sanitize_editor_html(submitted)
                # Blank, or byte-identical to the shipped default, means "no
                # override" — so the row goes away rather than pinning today's
                # wording in the database forever.
                if wants_reset or not submitted or submitted == block.default.strip():
                    if row:
                        row.delete()
                        reset += 1
                    continue
                if row and (row.html or row.text) == submitted:
                    continue
                row = row or SiteContent(key=key)
                if block.kind == RICH:
                    row.html, row.text = submitted, ""
                else:
                    row.text, row.html = submitted, ""
                row.updated_by = request.user
                row.save()
                changed += 1
                continue

            # --- media -------------------------------------------------------
            upload = request.FILES.get(f"file_{key}")
            poster = request.FILES.get(f"poster_{key}") if block.kind == VIDEO else None
            alt = (request.POST.get(f"alt_{key}") or "").strip()

            if wants_reset:
                if row:
                    # Delete the stored files too. A reset that leaves the old
                    # upload on disk means MEDIA_ROOT grows forever and a file
                    # someone explicitly removed is still fetchable by URL.
                    _drop_files(row)
                    row.delete()
                    reset += 1
                continue

            if not upload and not poster:
                # Alt text on its own is a real edit — you can describe the
                # photo that ships with the page without replacing it — so it
                # gets a row of its own rather than being dropped for want of
                # an upload to attach to.
                if alt == (row.alt_text if row else ""):
                    continue

            row = row or SiteContent(key=key)
            if upload:
                if block.kind == IMAGE:
                    row.image = upload
                else:
                    row.video = upload
            if poster:
                row.video_poster = poster
            row.alt_text = alt
            row.updated_by = request.user
            row.save()
            changed += 1

    if changed or reset:
        request.session["site_content_saved_at"] = timezone.now().isoformat()
    return changed, reset


def _drop_files(row):
    for field in ("image", "video", "video_poster"):
        stored = getattr(row, field, None)
        if stored:
            try:
                stored.delete(save=False)
            except Exception:
                # A missing file on disk must not block the reset — the point
                # of the action is to stop serving it either way.
                pass
