"""Custom dashboard cards — the safe metric engine.

Everything a card can measure is whitelisted here: which objects, which numeric
fields (sum/avg), which grouping dimensions and which filters. A card's stored
config is only ever *looked up* in these tables — never turned into a raw query —
so a card can never reach a field or object it shouldn't.
"""
from django.db.models import Avg, Count, Sum
from django.db.models.functions import TruncMonth
from django.utils import timezone

_MONTHS = ["jan", "fev", "mar", "abr", "mai", "jun", "jul", "ago", "set", "out", "nov", "dez"]
_PALETTE = ["#2563eb", "#059669", "#d97706", "#dc2626", "#7c3aed", "#0891b2",
            "#db2777", "#65a30d", "#0f766e", "#9333ea", "#ea580c", "#4f46e5"]

PERIOD_CHOICES = [("", "Todo período"), ("7", "Últimos 7 dias"), ("30", "Últimos 30 dias"),
                  ("90", "Últimos 90 dias"), ("365", "Último ano")]


def catalog():
    from modules.crm.models import Activity, Company, Contact, Deal
    return {
        "deal": {
            "model": Deal, "label": "Negócios",
            "number_fields": {"value": "Valor"},
            "money_fields": {"value"},
            "groups": {
                "stage": ("Etapa", "choice", "stage", dict(Deal.STAGE_CHOICES)),
                "stage_kind": ("Situação", "map", "stage_kind", {"open": "Aberto", "won": "Ganho", "lost": "Perdido"}),
                "owner": ("Responsável", "fk", "owner__username", None),
                "month": ("Mês (criação)", "date", "created_at", None),
            },
            "filters": {"period": "created_at", "stage": ("stage", dict(Deal.STAGE_CHOICES))},
        },
        "contact": {
            "model": Contact, "label": "Contatos",
            "number_fields": {"score": "Score"},
            "money_fields": set(),
            "groups": {
                "stage": ("Estágio", "choice", "stage", dict(Contact.STAGE_CHOICES)),
                "state": ("UF", "field", "state", None),
                "owner": ("Responsável", "fk", "owner__username", None),
                "month": ("Mês (criação)", "date", "created_at", None),
            },
            "filters": {"period": "created_at", "stage": ("stage", dict(Contact.STAGE_CHOICES))},
        },
        "company": {
            "model": Company, "label": "Empresas",
            "number_fields": {"employees": "Funcionários", "score": "Score"},
            "money_fields": set(),
            "groups": {
                "industry": ("Segmento", "field", "industry", None),
                "city": ("Cidade", "field", "city", None),
                "month": ("Mês (criação)", "date", "created_at", None),
            },
            "filters": {"period": "created_at"},
        },
        "task": {
            "model": Activity, "label": "Tarefas", "base": {"kind": "task"},
            "number_fields": {},
            "money_fields": set(),
            "groups": {
                "done": ("Status", "bool", "done", {True: "Concluída", False: "Pendente"}),
                "author": ("Autor", "fk", "author__username", None),
                "month": ("Mês (criação)", "date", "created_at", None),
            },
            "filters": {"period": "created_at", "done": "done"},
        },
    }


def card_options():
    """A JSON-serialisable description of the catalog for the editor UI."""
    out = {}
    for obj, spec in catalog().items():
        stage = spec["filters"].get("stage")
        out[obj] = {
            "label": spec["label"],
            "number_fields": [{"key": k, "label": v} for k, v in spec["number_fields"].items()],
            "groups": [{"key": k, "label": g[0]} for k, g in spec["groups"].items()],
            "stage_options": [{"key": k, "label": v} for k, v in (stage[1].items() if stage else [])],
            "has_done": "done" in spec["filters"],
        }
    return out


def compute_card(card, now=None):
    """Return the data to render a card: {kind, ...}. Best-effort — never raises."""
    try:
        now = now or timezone.now()
        spec = catalog().get(card.object_type)
        if spec is None:
            return {"kind": "error"}
        qs = spec["model"].all_objects.filter(workspace=card.workspace)
        for key, val in spec.get("base", {}).items():
            qs = qs.filter(**{key: val})
        qs = _apply_filters(qs, spec, card.filters or {}, now)
        is_money = card.metric in ("sum", "avg") and card.value_field in spec["money_fields"]

        if card.chart == "kpi":
            return {"kind": "kpi", "value": _aggregate(qs, card, spec), "money": is_money}
        if card.chart == "list":
            return {"kind": "list", "rows": [str(o) for o in qs.order_by("-created_at")[:8]]}
        series = _series(qs, card, spec)
        result = {"kind": card.chart, "series": series, "money": is_money,
                  "max": max([s["value"] for s in series] + [1]),
                  "total": sum(s["value"] for s in series)}
        if card.chart in ("bar", "pie"):
            for i, s in enumerate(series):
                s["color"] = _PALETTE[i % len(_PALETTE)]
        if card.chart == "pie":
            _add_pie(result)
        elif card.chart == "line":
            _add_line(result)
        return result
    except Exception:
        return {"kind": "error"}


def _add_pie(result):
    total = result["total"] or 1
    acc, stops = 0.0, []
    for s in result["series"]:
        frac = s["value"] / total * 100
        stops.append(f"{s['color']} {acc:.2f}% {acc + frac:.2f}%")
        s["pct"] = round(frac)
        acc += frac
    result["gradient"] = "conic-gradient(" + ", ".join(stops) + ")" if stops else "conic-gradient(#e7e9ee 0 100%)"


def _add_line(result):
    series, mx = result["series"], result["max"]
    n, w, h = len(series), 300, 90
    pts = []
    for i, s in enumerate(series):
        x = 0 if n <= 1 else round(i / (n - 1) * w, 1)
        y = round(h - (s["value"] / mx) * h, 1)
        pts.append(f"{x},{y}")
    result["points"] = " ".join(pts)
    result["area"] = (f"0,{h} " + " ".join(pts) + f" {w if n > 1 else 0},{h}") if pts else ""
    result["viewbox"] = f"0 0 {w} {h}"


def _aggregate(qs, card, spec):
    if card.metric == "count":
        return qs.count()
    field = card.value_field if card.value_field in spec["number_fields"] else None
    if not field:
        return qs.count()
    agg = Sum(field) if card.metric == "sum" else Avg(field)
    return round(float(qs.aggregate(v=agg)["v"] or 0))


def _apply_filters(qs, spec, filters, now):
    fspec = spec["filters"]
    if "period" in fspec and filters.get("period"):
        try:
            days = int(filters["period"])
        except (TypeError, ValueError):
            days = 0
        if days > 0:
            qs = qs.filter(**{fspec["period"] + "__gte": now - timezone.timedelta(days=days)})
    if "stage" in fspec and filters.get("stage"):
        field, choices = fspec["stage"]
        if filters["stage"] in choices:
            qs = qs.filter(**{field: filters["stage"]})
    if "done" in fspec and filters.get("done") in ("0", "1"):
        qs = qs.filter(**{fspec["done"]: filters["done"] == "1"})
    return qs


def _series(qs, card, spec):
    gspec = spec["groups"].get(card.group_by)
    if gspec is None:
        return [{"label": "Total", "value": _aggregate(qs, card, spec)}]
    _label, kind, field, mapping = gspec
    if kind == "date":
        qs = qs.annotate(_bucket=TruncMonth(field))
        key = "_bucket"
    else:
        key = field
    if card.metric == "count":
        agg = Count("id")
    else:
        vf = card.value_field if card.value_field in spec["number_fields"] else None
        agg = Count("id") if not vf else (Sum(vf) if card.metric == "sum" else Avg(vf))
    rows = qs.values(key).annotate(_v=agg).order_by(key)[:24]
    return [{"label": _fmt_label(r[key], kind, mapping), "value": round(float(r["_v"] or 0))} for r in rows]


def _fmt_label(raw, kind, mapping):
    if raw is None or raw == "":
        return "—"
    if kind == "date":
        try:
            return f"{_MONTHS[raw.month - 1]}/{str(raw.year)[2:]}"
        except Exception:
            return str(raw)
    if mapping is not None:
        return str(mapping.get(raw, raw))
    return str(raw)
