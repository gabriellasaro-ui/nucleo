from django import template

register = template.Library()


@register.filter
def dict_get(value, key):
    """Look up a dict value by a variable key (Django can't do d[var] inline)."""
    if isinstance(value, dict):
        return value.get(key)
    return None
