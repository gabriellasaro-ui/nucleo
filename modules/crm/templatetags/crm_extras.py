import re

from django import template
from django.utils.html import escape
from django.utils.safestring import mark_safe

register = template.Library()

_MENTION_RE = re.compile(r"@([\w.\-]+)")


@register.filter
def highlight_mentions(text):
    """Escape user text, wrap @mentions in a highlight span, keep line breaks."""
    if not text:
        return ""
    escaped = escape(text)
    escaped = _MENTION_RE.sub(r'<span class="mention">@\1</span>', escaped)
    return mark_safe(escaped.replace("\n", "<br>"))
