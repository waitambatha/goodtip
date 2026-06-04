from django import template

from ..team_colors import get_team_colors, team_initials


register = template.Library()


@register.simple_tag
def team_colors(slug: str) -> dict:
    return get_team_colors(slug)


@register.filter
def initials(value, max_chars: int = 3) -> str:
    try:
        max_chars = int(max_chars)
    except (TypeError, ValueError):
        max_chars = 3
    return team_initials(str(value), max_chars=max_chars)
