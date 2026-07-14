"""Metadata engine helpers: read workspace-defined custom fields and coerce
posted values by type. Values are stored on each record's `custom` JSON dict,
keyed by the field's `key`.
"""
from .models import CustomField


def get_fields(workspace, object_type):
    return list(CustomField.objects.filter(workspace=workspace, object_type=object_type))


def coerce(field, raw):
    ft = field.field_type
    if ft == "checkbox":
        return bool(raw)
    if raw in (None, ""):
        return None
    if ft == "number":
        try:
            number = float(raw)
            return int(number) if number.is_integer() else number
        except (TypeError, ValueError):
            return None
    return str(raw)


def read_from_post(post, fields):
    """Collect {key: value} for the given field defs from POST data."""
    data = {}
    for field in fields:
        name = "cf_" + field.key
        if field.field_type == "checkbox":
            data[field.key] = name in post
        else:
            value = coerce(field, post.get(name))
            if value is not None:
                data[field.key] = value
    return data


def with_values(fields, instance):
    """Pair each field def with the instance's current value (for rendering)."""
    custom = (getattr(instance, "custom", None) or {}) if instance else {}
    return [{"field": field, "value": custom.get(field.key)} for field in fields]
