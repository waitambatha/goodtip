"""Template tags that let the client edit copy without touching layout.

THE SHAPE OF THIS, AND WHY.

    {% ptext "home" "hero.title" %}Tip footy. Raise for good.{% endptext %}

The default lives in the template, between the tags. That is the whole design
in one line, and every property worth having falls out of it:

* The page renders correctly with an empty database. There is no seeding step
  and no "content not found" state to design.
* The original words stay in version control, next to the markup they were
  written for, where a developer reading the template can see them.
* Reverting is deleting a row.
* The editor can discover every editable slot on a page, and what it says by
  default, by reading the template source — no registry to keep in step.

Output is escaped. The client edits sentences, not markup; letting HTML
through would hand anyone with staff access stored XSS on the public site in
exchange for formatting the layout already does.
"""
from __future__ import annotations

from django import template
from django.utils.html import escape
from django.utils.safestring import mark_safe

register = template.Library()


def _text_overrides(context) -> dict:
    """Every override for the page being rendered, fetched once.

    Cached on the context, so a page with forty slots is one query rather than
    forty. `render_context` is per-render, which is what we want — a long-lived
    process must not serve yesterday's copy.
    """
    cache = context.render_context.get(_text_overrides)
    if cache is None:
        cache = {}
        context.render_context[_text_overrides] = cache
    return cache


class PTextNode(template.Node):
    def __init__(self, page, key, nodelist):
        self.page, self.key, self.nodelist = page, key, nodelist

    def render(self, context):
        from admin_panel.models import PageText

        page = self.page.resolve(context)
        key = self.key.resolve(context)
        cache = _text_overrides(context)
        if page not in cache:
            cache[page] = dict(
                PageText.objects.filter(page=page).values_list("key", "value")
            )
        value = cache[page].get(key)
        if value is None or value == "":
            # Blank is not an override. Somebody clearing a field means "put
            # it back", not "leave a gap on the live site" — and an empty
            # heading is indistinguishable from a broken page.
            return self.nodelist.render(context)
        # Newlines survive as line breaks: the editor is a textarea, and a
        # paragraph typed with a blank line in it should arrive looking like
        # one rather than as a single run-on sentence.
        return mark_safe(escape(value).replace("\n", "<br>"))


@register.tag("ptext")
def do_ptext(parser, token):
    bits = token.split_contents()
    if len(bits) != 3:
        raise template.TemplateSyntaxError(
            '{% ptext "page" "key" %}default copy{% endptext %}'
        )
    nodelist = parser.parse(("endptext",))
    parser.delete_first_token()
    return PTextNode(parser.compile_filter(bits[1]), parser.compile_filter(bits[2]), nodelist)


class PImageNode(template.Node):
    def __init__(self, page, slot, nodelist):
        self.page, self.slot, self.nodelist = page, slot, nodelist

    def render(self, context):
        from admin_panel.models import PageMedia

        page = self.page.resolve(context)
        slot = self.slot.resolve(context)
        row = (
            PageMedia.objects
            .filter(page=page, slot=slot, kind=PageMedia.KIND_IMAGE)
            .order_by("sort_order", "-uploaded_at")
            .first()
        )
        if row is None:
            return self.nodelist.render(context)     # the template's own image
        if row.is_hidden:
            # "Take that picture off the page." Renders nothing at all rather
            # than an empty <img>, which would leave a hole the size of the
            # photograph and read as a failed load.
            return ""
        return mark_safe(escape(row.file.url))


@register.tag("pimage")
def do_pimage(parser, token):
    """{% pimage "home" "hero" %}{% static 'img/hero.jpg' %}{% endpimage %}

    Emits a URL, not an <img>. The template keeps its own tag — with its
    sizes, its loading attribute and its classes — because those are layout,
    and layout is not what the client is editing.
    """
    bits = token.split_contents()
    if len(bits) != 3:
        raise template.TemplateSyntaxError(
            '{% pimage "page" "slot" %}fallback url{% endpimage %}'
        )
    nodelist = parser.parse(("endpimage",))
    parser.delete_first_token()
    return PImageNode(parser.compile_filter(bits[1]), parser.compile_filter(bits[2]), nodelist)
