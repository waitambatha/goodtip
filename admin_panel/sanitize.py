"""Defence-in-depth cleanup for HTML that comes out of the story editor.

The editor (templates/manage/news_editor.html + static/js/gt-news-editor.js)
is only reachable by superusers, so this isn't the primary control — it's a
second layer in case an account is compromised or a future caller feeds this
something less trusted.
"""
import re

_SCRIPT_STYLE_RE = re.compile(r"<(script|style)\b[^>]*>.*?</\1>", re.IGNORECASE | re.DOTALL)
_EVENT_ATTR_RE = re.compile(r'\s+on\w+\s*=\s*("[^"]*"|\'[^\']*\'|[^\s>]+)', re.IGNORECASE)
_JS_URL_RE = re.compile(r'((?:href|src)\s*=\s*)(["\'])\s*javascript:[^"\']*\2', re.IGNORECASE)


def sanitize_editor_html(value: str) -> str:
    if not value:
        return value
    value = _SCRIPT_STYLE_RE.sub("", value)
    value = _EVENT_ATTR_RE.sub("", value)
    value = _JS_URL_RE.sub(lambda m: m.group(1) + m.group(2) + m.group(2), value)
    return value
