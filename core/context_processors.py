"""Shared UI context — the navigation model that drives the sidebar and the
⌘K command palette. Modules contribute their own entries here as they land.
"""
import re

from django.urls import reverse

from .rbac import can_edit as _can_edit
from .whatsapp_inbox import whatsapp_unread_count

_HEX_RE = re.compile(r"^#[0-9a-fA-F]{6}$")


def _hex_to_rgb(value):
    value = value.lstrip("#")
    return tuple(int(value[i:i + 2], 16) for i in (0, 2, 4))


def _mix(rgb, other, t):
    return tuple(round(a + (b - a) * t) for a, b in zip(rgb, other))


def _to_hex(rgb):
    return "#%02x%02x%02x" % rgb


def branding(request):
    """Derive a full accent ramp from the workspace brand color and expose it
    as a :root override, so changing one color re-themes the whole app.
    """
    ws = getattr(request, "workspace", None)
    color = getattr(ws, "brand_color", None) or "#2563eb"
    if not _HEX_RE.match(color):
        color = "#2563eb"
    rgb = _hex_to_rgb(color)
    white, black = (255, 255, 255), (0, 0, 0)
    ramp = {
        "--blue-50": _to_hex(_mix(rgb, white, 0.92)),
        "--blue-100": _to_hex(_mix(rgb, white, 0.85)),
        "--blue-200": _to_hex(_mix(rgb, white, 0.72)),
        "--blue-500": _to_hex(_mix(rgb, white, 0.10)),
        "--blue-600": color,
        "--blue-700": _to_hex(_mix(rgb, black, 0.14)),
        "--blue-800": _to_hex(_mix(rgb, black, 0.28)),
        "--ring": f"rgba({rgb[0]}, {rgb[1]}, {rgb[2]}, 0.22)",
    }
    brand_css = ":root{" + "".join(f"{k}:{v};" for k, v in ramp.items()) + "}"
    return {
        "brand_css": brand_css,
        "brand_color": color,
        "brand_logo": getattr(ws, "logo", None),
    }


def _nav_model(request, whatsapp_unread=0):
    sections = [
        {
            "section": "Workspace",
            "items": [
                {"label": "Dashboard", "url_name": "dashboard", "icon": "home", "shortcut": "G D"},
            ],
        },
        {
            "section": "CRM",
            "items": [
                {"label": "Negócios", "url_name": "crm:deal_board", "icon": "briefcase", "shortcut": "G N"},
                {"label": "Empresas", "url_name": "crm:company_list", "icon": "building", "shortcut": "G E"},
                {"label": "Contatos", "url_name": "crm:contact_list", "icon": "users", "shortcut": "G C"},
                {
                    "label": "WhatsApp",
                    "url_name": "whatsapp",
                    "icon": "whatsapp",
                    "shortcut": "G W",
                    "notification_count": whatsapp_unread,
                    "notification_label": "99+" if whatsapp_unread > 99 else str(whatsapp_unread),
                    "notification_url": reverse("whatsapp_unread_count"),
                },
            ],
        },
    ]
    membership = getattr(request, "membership", None)
    if membership is not None and membership.can("admin"):
        sections.append({
            "section": "Configurações",
            "items": [
                {"label": "Aparência", "url_name": "appearance", "icon": "palette", "shortcut": ""},
                {"label": "Membros", "url_name": "members", "icon": "users", "shortcut": ""},
                {"label": "Automações", "url_name": "automations", "icon": "bolt", "shortcut": ""},
                {"label": "Integrações", "url_name": "integrations", "icon": "plug", "shortcut": ""},
            ],
        })
    return sections


def navigation(request):
    sections = []
    palette = []
    unread_count = whatsapp_unread_count(getattr(request, "workspace", None))
    for section in _nav_model(request, unread_count):
        items = []
        for item in section["items"]:
            try:
                url = reverse(item["url_name"])
            except Exception:
                continue
            entry = {**item, "url": url}
            items.append(entry)
            palette.append({**entry, "section": section["section"]})
        if items:
            sections.append({"section": section["section"], "items": items})
    # Map common breadcrumb labels -> their page, so crumbs become clickable.
    breadcrumb_urls = {}
    for section in sections:
        for item in section["items"]:
            breadcrumb_urls[item["label"]] = item["url"]
    for label, name in (("Facebook", "facebook_forms"), ("Formulários", "facebook_forms"),
                        ("Campos personalizados", "custom_fields")):
        try:
            breadcrumb_urls.setdefault(label, reverse(name))
        except Exception:
            pass
    membership = getattr(request, "membership", None)
    return {
        "nav_sections": sections,
        "command_palette": palette,
        "breadcrumb_urls": breadcrumb_urls,
        "can_edit": _can_edit(request),
        "can_admin": bool(membership and membership.can("admin")),
    }
