from django import template

from core.money import format_money

register = template.Library()


@register.filter
def dict_get(value, key):
    """Look up a dict value by a variable key (Django can't do d[var] inline)."""
    if isinstance(value, dict):
        return value.get(key)
    return None


@register.filter
def money(value, code="BRL"):
    """Format a number as the workspace currency (symbol + locale grouping)."""
    return format_money(value, code or "BRL")
