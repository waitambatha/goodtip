"""Template tags that read an editable slot on the public site.

Every tag falls back to the default declared in admin_panel/site_blocks.py, so
a template using them renders identically on a database where nobody has
edited anything. That is what lets the public pages be converted to editable
slots one section at a time without a content migration.

    {% load site_content %}
    <h1>{% site_rich "home.hero.title" %}</h1>
    <p>{% site_text "home.hero.sub" %}</p>
    <div style="background-image:url('{% site_img "home.hero.shot1" %}')"></div>
    <video data-src="{% site_video "home.hero.clip" %}"
           poster="{% site_poster "home.hero.clip" %}"></video>

Escaping: text and rich values are marked safe. They are written only by
superusers through the Site content editor, which runs every submission
through admin_panel.sanitize.sanitize_editor_html (scripts, style blocks,
inline event handlers and javascript: URLs removed) before it is stored — the
same treatment the news story editor gets. Marking them safe is what makes the
entities and small tags the copy actually uses (&middot;, &rarr;, <em>,
<strong>, links) render as intended rather than as literal source. Where a
value lands inside an HTML attribute instead, use {% site_attr %}, which
escapes.
"""
from django import template
from django.templatetags.static import static
from django.utils.html import escape
from django.utils.safestring import mark_safe

from admin_panel.site_blocks import BLOCKS

register = template.Library()


def _row(key):
    from admin_panel.models import SiteContent

    return SiteContent.map().get(key)


def _default(key):
    block = BLOCKS.get(key)
    return block.default if block else ""


def _copy(key):
    """The stored override for a text/rich slot, or the template default.

    An override that is blank counts as no override — clearing a field in the
    editor is how you put the original copy back, so an empty string must
    never reach the page.
    """
    row = _row(key)
    if row:
        value = (row.html or row.text or "").strip()
        if value:
            return value
    return _default(key)


@register.simple_tag
def site_text(key):
    return mark_safe(_copy(key))


# Rich and plain differ only in the editor they get; at render time a slot is
# a slot. Kept as two names so a template reads as a description of the page.
@register.simple_tag
def site_rich(key):
    return mark_safe(_copy(key))


@register.simple_tag
def site_attr(key):
    """The same value, escaped — for use inside an HTML attribute."""
    return escape(_copy(key))


@register.simple_tag
def site_img(key, fallback=""):
    """URL for an image slot: the upload if there is one, else the static default."""
    row = _row(key)
    if row and row.image:
        return row.image.url
    path = fallback or _default(key)
    return static(path) if path else ""


@register.simple_tag
def site_video(key, fallback=""):
    row = _row(key)
    if row and row.video:
        return row.video.url
    path = fallback or _default(key)
    return static(path) if path else ""


@register.simple_tag
def site_poster(key, fallback=""):
    """Poster frame for a video slot.

    With no explicit fallback the default is derived from the clip's own
    filename — clip.mp4 -> clip-poster.jpg — which is the convention the
    static/video/ directory already follows, so an unedited slot finds the
    poster that has always shipped with the clip.
    """
    row = _row(key)
    if row and row.video_poster:
        return row.video_poster.url
    if fallback:
        return static(fallback)
    clip = _default(key)
    if not clip:
        return ""
    stem = clip.rsplit(".", 1)[0]
    return static(f"{stem}-poster.jpg")


@register.simple_tag
def site_alt(key, fallback=""):
    """Alt text for an image slot, escaped for attribute use."""
    row = _row(key)
    if row and row.alt_text:
        return escape(row.alt_text)
    return escape(fallback)
