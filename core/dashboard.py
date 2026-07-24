"""Configurable dashboard — the catalog of widgets and how a workspace's saved
layout resolves against it.

`Workspace.dashboard_layout` is an ordered list of enabled widget keys. It's
merged with the catalog on every read so newly-shipped widgets show up for
existing workspaces and removed/renamed keys are ignored — the saved value is
never the single source of truth for what *can* exist.
"""

# key, label, description (shown in the settings picker). Order here is the
# default layout for a workspace that hasn't customised anything.
DASHBOARD_WIDGETS = [
    ("kpis", "Indicadores", "Pipeline aberto, ganho, ticket médio e conversão."),
    ("funnel", "Funil de vendas", "Valor por etapa da pipeline selecionada."),
    ("contacts", "Contatos por estágio", "Como seus contatos se distribuem."),
    ("tasks", "Tarefas pendentes", "As próximas tarefas em aberto."),
    ("recent_deals", "Negócios recentes", "Últimos negócios criados."),
    ("companies", "Empresas recentes", "Últimas empresas cadastradas."),
]

DASHBOARD_KEYS = [key for key, _, _ in DASHBOARD_WIDGETS]
_LABELS = {key: label for key, label, _ in DASHBOARD_WIDGETS}
_DESCS = {key: desc for key, _, desc in DASHBOARD_WIDGETS}


def resolve_layout(ws):
    """Return an ordered list of {key, label, description, on} for the picker,
    honouring the saved order first, then appending any not-yet-listed widgets
    (off by default so new widgets don't silently rearrange an existing panel).
    """
    saved = getattr(ws, "dashboard_layout", None)
    if not isinstance(saved, list):
        saved = []
    saved = [k for k in saved if k in _LABELS]  # drop unknown/removed keys
    rows = [{"key": k, "label": _LABELS[k], "description": _DESCS[k], "on": True} for k in saved]
    for key in DASHBOARD_KEYS:
        if key not in saved:
            on = not saved  # first-run (nothing saved) shows everything
            rows.append({"key": key, "label": _LABELS[key], "description": _DESCS[key], "on": on})
    return rows


def enabled_keys(ws):
    """Ordered list of widget keys the dashboard should actually render."""
    return [row["key"] for row in resolve_layout(ws) if row["on"]]
