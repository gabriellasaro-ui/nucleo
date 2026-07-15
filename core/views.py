import json
import re

from django.contrib import messages
from django.contrib.auth import get_user_model
from django.contrib.auth.decorators import login_required
from django.core.exceptions import ValidationError
from django.core.validators import validate_email
from django.db import transaction
from django.db.models import Q, Sum
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse
from django.utils.crypto import get_random_string
from django.utils.text import slugify
from django_tenants.utils import get_public_schema_name, schema_context

from modules.crm.models import Company, Contact, Deal

from .forms import WorkspaceForm
from .models import EVENT_CHOICES, Automation, CustomField, Domain, Membership
from .rbac import require_role


# Categorical palette for the contacts donut (validated: dataviz validator,
# light mode — CVD ΔE 39.3). Fixed order; never cycled.
_CONTACT_COLORS = {"lead": "#2563eb", "qualificado": "#d97706", "cliente": "#059669", "inativo": "#7c3aed"}
_FUNNEL_STAGES = ["novo", "qualificado", "proposta", "negociacao", "ganho"]
_AUTOMATION_IDEAS = [
    {
        "name": "Lead novo entra no comercial",
        "summary": "Quando um contato nasce como Lead, cria um negócio na pipeline principal, coluna Novo, e abre uma tarefa no próprio negócio.",
        "trigger": "contact_created",
        "condition_contact_stage": "lead",
        "action1_type": "create_deal",
        "action1_deal_stage": "novo",
        "action1_text": "",
        "action2_type": "create_task",
        "action2_text": "Fazer primeiro atendimento do lead.",
        "action2_due_days": "0",
    },
    {
        "name": "Contato qualificado vira oportunidade",
        "summary": "Quando o contato muda para Qualificado, cria um negócio direto na coluna Qualificado e já agenda a próxima ação.",
        "trigger": "contact_stage_changed",
        "condition_contact_stage": "qualificado",
        "action1_type": "create_deal",
        "action1_deal_stage": "qualificado",
        "action1_text": "",
        "action2_type": "create_task",
        "action2_text": "Definir proposta inicial e responsável pelo atendimento.",
        "action2_due_days": "1",
    },
    {
        "name": "Negócio ganho atualiza contato",
        "summary": "Quando o negócio vai para Ganho, o contato ligado vira Cliente e uma tarefa de passagem interna é criada.",
        "trigger": "deal_stage_changed",
        "condition_deal_stage": "ganho",
        "action1_type": "set_contact_stage",
        "action1_contact_stage": "cliente",
        "action2_type": "create_task",
        "action2_text": "Fazer passagem interna do cliente e registrar próximos responsáveis.",
        "action2_due_days": "0",
    },
    {
        "name": "Negócio perdido congela o contato",
        "summary": "Quando um negócio vai para Perdido, o contato ligado sai do fluxo ativo e recebe uma nota automática.",
        "trigger": "deal_stage_changed",
        "condition_deal_stage": "perdido",
        "action1_type": "set_contact_stage",
        "action1_contact_stage": "inativo",
        "action2_type": "create_note",
        "action2_text": "Contato marcado como inativo por perda do negócio.",
    },
]


@login_required
def dashboard(request):
    ws = request.workspace
    deals = Deal.objects.filter(workspace=ws)

    open_value = deals.filter(stage__in=Deal.OPEN_STAGES).aggregate(v=Sum("value"))["v"] or 0
    won = deals.filter(stage="ganho")
    won_value = won.aggregate(v=Sum("value"))["v"] or 0
    won_count = won.count()
    lost_count = deals.filter(stage="perdido").count()
    avg_ticket = (won_value / won_count) if won_count else 0
    conversion = (won_count / (won_count + lost_count) * 100) if (won_count + lost_count) else 0

    # Funnel — value per stage (single-hue bars; identity is on the axis label)
    stage_labels = dict(Deal.STAGE_CHOICES)
    funnel = []
    for stage in _FUNNEL_STAGES:
        qs = deals.filter(stage=stage)
        funnel.append({"label": stage_labels[stage], "count": qs.count(),
                       "value": qs.aggregate(v=Sum("value"))["v"] or 0})
    max_value = max([row["value"] for row in funnel] + [1])
    for row in funnel:
        row["pct"] = round(row["value"] / max_value * 100)

    # Contacts by stage — donut (composition)
    contacts = Contact.objects.filter(workspace=ws)
    total_contacts = contacts.count()
    segments, stops, acc = [], [], 0.0
    for key, label in Contact.STAGE_CHOICES:
        count = contacts.filter(stage=key).count()
        if not count:
            continue
        frac = count / total_contacts * 100
        color = _CONTACT_COLORS.get(key, "#94a3b8")
        segments.append({"label": label, "count": count, "pct": round(frac), "color": color})
        stops.append(f"{color} {acc:.2f}% {acc + frac:.2f}%")
        acc += frac
    donut_gradient = "conic-gradient(" + ", ".join(stops) + ")" if stops else "conic-gradient(#e7e9ee 0% 100%)"

    context = {
        "page_title": "Dashboard",
        "breadcrumb": ["Workspace", "Dashboard"],
        "kpis": [
            {"label": "Pipeline aberto", "value": f"R$ {open_value:,.0f}", "accent": True},
            {"label": "Ganho", "value": f"R$ {won_value:,.0f}"},
            {"label": "Ticket médio", "value": f"R$ {avg_ticket:,.0f}"},
            {"label": "Taxa de conversão", "value": f"{conversion:.0f}%"},
        ],
        "funnel": funnel,
        "donut_segments": segments,
        "donut_gradient": donut_gradient,
        "total_contacts": total_contacts,
        "recent_companies": Company.objects.filter(workspace=ws).order_by("-created_at")[:5],
    }
    return render(request, "core/dashboard.html", context)


# --------------------------------------------------------------------------- #
# Workspaces / onboarding
# --------------------------------------------------------------------------- #
@login_required
def workspace_new(request):
    form = WorkspaceForm(request.POST or None)
    if request.method == "POST" and form.is_valid():
        with schema_context(get_public_schema_name()):
            workspace = form.save()  # creates the tenant's Postgres schema
            Domain.objects.get_or_create(
                domain=f"{workspace.schema_name}.localhost", tenant=workspace,
                defaults={"is_primary": True},
            )
            Membership.objects.create(
                user=request.user, workspace=workspace, role=Membership.ROLE_OWNER
            )
        request.session["workspace_id"] = workspace.pk
        messages.success(request, f"Workspace “{workspace.name}” criado.")
        return redirect("dashboard")
    return render(request, "core/workspace_new.html", {
        "form": form,
        "page_title": "Novo workspace",
        "has_any": bool(getattr(request, "memberships", [])),
    })


@login_required
def workspace_switch(request, pk):
    if request.method == "POST" and Membership.objects.filter(
        user=request.user, workspace_id=pk
    ).exists():
        request.session["workspace_id"] = pk
    return redirect("dashboard")


# --------------------------------------------------------------------------- #
# Member management (admin+)
# --------------------------------------------------------------------------- #
@login_required
@require_role("admin")
def members(request):
    memberships = request.workspace.memberships.select_related("user").all()
    return render(request, "core/members.html", {
        "page_title": "Membros",
        "breadcrumb": ["Configurações", "Membros"],
        "memberships": memberships,
        "roles": Membership.ROLE_CHOICES,
    })


@login_required
@require_role("admin")
def member_add(request):
    if request.method == "POST":
        email = request.POST.get("email", "").strip().lower()
        name = request.POST.get("name", "").strip()
        legacy_identifier = request.POST.get("identifier", "").strip()
        if legacy_identifier and not email and not name:
            if "@" in legacy_identifier:
                email = legacy_identifier.lower()
            else:
                name = legacy_identifier
        role = request.POST.get("role", Membership.ROLE_MEMBER)
        if role not in dict(Membership.ROLE_CHOICES):
            role = Membership.ROLE_MEMBER

        if not email:
            messages.error(request, "Informe o e-mail do membro.")
            return redirect("members")
        try:
            validate_email(email)
        except ValidationError:
            messages.error(request, "Informe um e-mail válido.")
            return redirect("members")

        with schema_context(get_public_schema_name()), transaction.atomic():
            User = get_user_model()
            user = User.objects.filter(Q(email__iexact=email) | Q(username__iexact=email)).first()
            created_user = user is None
            temporary_password = ""
            if created_user:
                temporary_password = _temporary_password()
                user = User(
                    username=_available_username(User, email),
                    email=email,
                    first_name=name[:150],
                )
                user.set_password(temporary_password)
                user.save()
            elif name and not user.get_full_name():
                user.first_name = name[:150]
                user.save(update_fields=["first_name"])

            membership, created_membership = Membership.objects.get_or_create(
                user=user,
                workspace=request.workspace,
                defaults={"role": role},
            )

        if not created_membership:
            messages.warning(request, f"{user.get_username()} já é membro deste workspace.")
        elif created_user:
            messages.success(
                request,
                f"Conta criada e adicionada como {dict(Membership.ROLE_CHOICES)[role]}. "
                f"Login: {user.get_username()} | Senha temporária: {temporary_password}",
            )
        else:
            messages.success(request, f"{user.get_username()} adicionado como {dict(Membership.ROLE_CHOICES)[role]}.")
    return redirect("members")


@login_required
@require_role("admin")
def member_update(request, pk):
    if request.method == "POST":
        m = get_object_or_404(Membership, pk=pk, workspace=request.workspace)
        role = request.POST.get("role")
        if role not in dict(Membership.ROLE_CHOICES):
            messages.error(request, "Papel inválido.")
        elif m.role == Membership.ROLE_OWNER and role != Membership.ROLE_OWNER and _owner_count(request) <= 1:
            messages.error(request, "Não é possível rebaixar o único proprietário.")
        else:
            m.role = role
            m.save(update_fields=["role"])
            messages.success(request, "Papel atualizado.")
    return redirect("members")


@login_required
@require_role("admin")
def member_remove(request, pk):
    if request.method == "POST":
        m = get_object_or_404(Membership, pk=pk, workspace=request.workspace)
        if m.role == Membership.ROLE_OWNER and _owner_count(request) <= 1:
            messages.error(request, "Não é possível remover o único proprietário.")
        else:
            name = m.user.get_username()
            m.delete()
            messages.success(request, f"{name} removido do workspace.")
    return redirect("members")


def _owner_count(request):
    return request.workspace.memberships.filter(role=Membership.ROLE_OWNER).count()


def _temporary_password():
    return get_random_string(
        12,
        allowed_chars="ABCDEFGHJKLMNPQRSTUVWXYZabcdefghijkmnopqrstuvwxyz23456789",
    )


def _available_username(User, email):
    max_length = User._meta.get_field("username").max_length
    base = email[:max_length] or "usuario"
    candidate = base
    suffix = 2
    while User.objects.filter(username__iexact=candidate).exists():
        marker = f".{suffix}"
        candidate = f"{base[:max_length - len(marker)]}{marker}"
        suffix += 1
    return candidate


# --------------------------------------------------------------------------- #
# Metadata engine: custom fields (admin+)
# --------------------------------------------------------------------------- #
@login_required
@require_role("admin")
def custom_fields(request):
    ws = request.workspace
    groups = [
        {
            "type": obj_type,
            "label": label,
            "fields": CustomField.objects.filter(workspace=ws, object_type=obj_type),
        }
        for obj_type, label in CustomField.OBJECT_CHOICES
    ]
    return render(request, "core/custom_fields.html", {
        "page_title": "Campos personalizados",
        "breadcrumb": ["Configurações", "Campos personalizados"],
        "groups": groups,
        "type_choices": CustomField.TYPE_CHOICES,
        "object_choices": CustomField.OBJECT_CHOICES,
    })


@login_required
@require_role("admin")
def custom_field_add(request):
    if request.method == "POST":
        ws = request.workspace
        label = request.POST.get("label", "").strip()
        object_type = request.POST.get("object_type")
        field_type = request.POST.get("field_type", "text")
        required = request.POST.get("required") == "on"
        options = [o.strip() for o in request.POST.get("options", "").split(",") if o.strip()]
        if not label:
            messages.error(request, "Informe um rótulo para o campo.")
        elif object_type not in dict(CustomField.OBJECT_CHOICES) or field_type not in dict(CustomField.TYPE_CHOICES):
            messages.error(request, "Objeto ou tipo inválido.")
        elif field_type == "select" and not options:
            messages.error(request, "Para “Seleção”, informe as opções separadas por vírgula.")
        else:
            CustomField.objects.create(
                workspace=ws,
                object_type=object_type,
                key=_unique_key(ws, object_type, label),
                label=label,
                field_type=field_type,
                options=options if field_type == "select" else [],
                required=required,
                order=CustomField.objects.filter(workspace=ws, object_type=object_type).count(),
            )
            messages.success(request, f"Campo “{label}” criado.")
    return redirect("custom_fields")


@login_required
@require_role("admin")
def custom_field_update(request, pk):
    if request.method == "POST":
        field = get_object_or_404(CustomField, pk=pk, workspace=request.workspace)
        label = request.POST.get("label", "").strip()
        if label:
            field.label = label
        field.required = request.POST.get("required") == "on"
        if field.field_type == "select":
            options = [o.strip() for o in request.POST.get("options", "").split(",") if o.strip()]
            if options:
                field.options = options
        field.save()
        messages.success(request, f"Campo “{field.label}” atualizado.")
    return redirect("custom_fields")


@login_required
@require_role("admin")
def custom_field_delete(request, pk):
    if request.method == "POST":
        get_object_or_404(CustomField, pk=pk, workspace=request.workspace).delete()
        messages.success(request, "Campo removido.")
    return redirect("custom_fields")


def _unique_key(ws, object_type, label):
    base = slugify(label).replace("-", "_")[:60] or "campo"
    existing = set(
        CustomField.objects.filter(workspace=ws, object_type=object_type).values_list("key", flat=True)
    )
    key, i = base, 2
    while key in existing:
        key = f"{base}_{i}"
        i += 1
    return key


# --------------------------------------------------------------------------- #
# Automations (admin+)
# --------------------------------------------------------------------------- #
@login_required
@require_role("admin")
def automations(request):
    ws = request.workspace
    selected_automation = None
    flow_id = request.GET.get("flow", "").strip()
    if flow_id.isdigit():
        selected_automation = ws.automations.filter(pk=int(flow_id)).first()
    return render(request, "core/automations.html", {
        "page_title": "Automações",
        "breadcrumb": ["Configurações", "Automações"],
        "automations": ws.automations.all(),
        "selected_automation": selected_automation,
        "initial_canvas": selected_automation.canvas if selected_automation else {},
        "recent_events": ws.events.all()[:10],
        "triggers": EVENT_CHOICES,
        "actions": Automation.ACTION_CHOICES,
        "deal_stages": Deal.STAGE_CHOICES,
        "contact_stages": Contact.STAGE_CHOICES,
    })


@login_required
@require_role("admin")
def automation_add(request):
    if request.method == "POST":
        ws = request.workspace
        name = request.POST.get("name", "").strip()
        trigger = request.POST.get("trigger")
        automation_id = request.POST.get("automation_id", "").strip()
        existing = ws.automations.filter(pk=int(automation_id)).first() if automation_id.isdigit() else None
        canvas = _parse_automation_canvas(request)
        condition_stage = _automation_condition_stage(request, trigger, canvas)
        actions = _order_actions_by_canvas(_parse_automation_actions(request), canvas)
        first = actions[0] if actions else {}
        if not name:
            messages.error(request, "Informe o nome da automação.")
        elif trigger not in dict(EVENT_CHOICES):
            messages.error(request, "Gatilho inválido.")
        elif not actions:
            messages.error(request, "Configure pelo menos uma ação no fluxo.")
        else:
            defaults = {
                "name": name,
                "trigger": trigger,
                "condition_stage": condition_stage,
                "action": first.get("type", "create_task"),
                "action_text": first.get("text", ""),
                "action_due_days": first.get("due_days", 0),
                "conditions": {"stage": condition_stage} if condition_stage else {},
                "actions": actions,
                "canvas": canvas,
                "active": True,
            }
            if existing:
                for field, value in defaults.items():
                    setattr(existing, field, value)
                existing.save(update_fields=list(defaults))
                auto = existing
            else:
                auto = Automation.objects.create(workspace=ws, **defaults)
            messages.success(request, "Fluxo salvo.")
            return redirect(f"{reverse('automations')}?flow={auto.pk}")
            messages.success(request, f"Automação “{name}” criada.")
    return redirect("automations")


def _automation_condition_stage(request, trigger, canvas=None):
    for node in (canvas or {}).get("nodes", []):
        if node.get("type") not in {"filter", "condition"}:
            continue
        stage = node.get("data", {}).get("condition_stage", "")
        if stage:
            return stage
    if trigger in {"contact_created", "contact_stage_changed"}:
        return request.POST.get("condition_contact_stage", "").strip()
    if trigger in {"deal_created", "deal_stage_changed"}:
        return request.POST.get("condition_deal_stage", "").strip()
    return ""


def _parse_automation_actions(request):
    valid_actions = dict(Automation.ACTION_CHOICES)
    actions = []
    indexes = sorted({
        int(match.group(1))
        for key in request.POST
        for match in [re.match(r"action(\d+)_type$", key)]
        if match
    })
    for i in indexes:
        action_type = request.POST.get(f"action{i}_type", "").strip()
        if not action_type:
            continue
        if action_type not in valid_actions:
            continue
        action = {"_index": i, "type": action_type}
        text = request.POST.get(f"action{i}_text", "").strip()
        if text:
            action["text"] = text
        pipeline = request.POST.get(f"action{i}_pipeline", "").strip()
        if pipeline:
            action["pipeline"] = pipeline
        if action_type in {"create_deal", "move_deal"}:
            stage = request.POST.get(f"action{i}_deal_stage", "").strip()
            if stage:
                action["deal_stage"] = stage
        if action_type == "set_contact_stage":
            stage = request.POST.get(f"action{i}_contact_stage", "").strip()
            if stage:
                action["contact_stage"] = stage
        if action_type == "create_task":
            try:
                action["due_days"] = max(0, int(request.POST.get(f"action{i}_due_days") or 0))
            except ValueError:
                action["due_days"] = 0
        actions.append(action)
    return actions


def _parse_automation_canvas(request):
    raw = request.POST.get("canvas", "").strip()
    if not raw:
        return {}
    try:
        data = json.loads(raw)
    except json.JSONDecodeError:
        return {}
    if not isinstance(data, dict):
        return {}

    nodes = []
    node_ids = set()
    for node in data.get("nodes", [])[:80]:
        if not isinstance(node, dict):
            continue
        node_id = str(node.get("id", ""))[:80]
        node_type = str(node.get("type", ""))[:30]
        if not node_id or node_id in node_ids or node_type not in {"trigger", "filter", "condition", "action"}:
            continue
        node_ids.add(node_id)
        nodes.append({
            "id": node_id,
            "type": node_type,
            "x": _canvas_int(node.get("x"), 0, 20000),
            "y": _canvas_int(node.get("y"), 0, 20000),
            "data": _clean_canvas_data(node.get("data", {})),
        })

    edges = []
    seen_edges = set()
    for edge in data.get("edges", [])[:160]:
        if not isinstance(edge, dict):
            continue
        source = str(edge.get("from", ""))[:80]
        target = str(edge.get("to", ""))[:80]
        key = (source, target)
        if not source or not target or source == target or source not in node_ids or target not in node_ids:
            continue
        if key in seen_edges:
            continue
        seen_edges.add(key)
        edges.append({"from": source, "to": target})

    viewport = data.get("viewport", {})
    if not isinstance(viewport, dict):
        viewport = {}

    return {
        "version": 1,
        "nodes": nodes,
        "edges": edges,
        "viewport": {
            "scroll_left": _canvas_int(viewport.get("scroll_left"), 0, 20000),
            "scroll_top": _canvas_int(viewport.get("scroll_top"), 0, 20000),
            "zoom": _canvas_float(viewport.get("zoom"), 0.55, 1.75, 1),
        },
    }


def _clean_canvas_data(data):
    if not isinstance(data, dict):
        return {}
    allowed = {
        "trigger",
        "condition_kind",
        "condition_stage",
        "action_index",
        "action_type",
        "pipeline",
        "deal_stage",
        "contact_stage",
        "text",
        "due_days",
    }
    clean = {}
    for key, value in data.items():
        if key not in allowed:
            continue
        if key in {"action_index", "due_days"}:
            clean[key] = _canvas_int(value, 0, 3650)
        else:
            clean[key] = str(value)[:500]
    return clean


def _canvas_int(value, min_value, max_value):
    try:
        number = int(float(value))
    except (TypeError, ValueError):
        return min_value
    return max(min_value, min(max_value, number))


def _canvas_float(value, min_value, max_value, default):
    try:
        number = float(value)
    except (TypeError, ValueError):
        return default
    return round(max(min_value, min(max_value, number)), 2)


def _order_actions_by_canvas(actions, canvas):
    by_index = {action.get("_index"): action for action in actions}
    ordered_indexes = []
    nodes = {node.get("id"): node for node in canvas.get("nodes", [])}
    outgoing = {}
    for edge in canvas.get("edges", []):
        outgoing.setdefault(edge.get("from"), edge.get("to"))

    trigger_nodes = [node_id for node_id, node in nodes.items() if node.get("type") == "trigger"]
    current = trigger_nodes[0] if trigger_nodes else "entry"
    seen = set()
    for _ in range(len(nodes) + 1):
        current = outgoing.get(current)
        if not current or current in seen:
            break
        seen.add(current)
        node = nodes.get(current, {})
        if node.get("type") == "action":
            index = node.get("data", {}).get("action_index")
            if index in by_index:
                ordered_indexes.append(index)

    ordered = [by_index[index] for index in ordered_indexes]
    ordered.extend(action for action in actions if action.get("_index") not in ordered_indexes)
    for action in ordered:
        action.pop("_index", None)
    return ordered


@login_required
@require_role("admin")
def automation_toggle(request, pk):
    if request.method == "POST":
        auto = get_object_or_404(Automation, pk=pk, workspace=request.workspace)
        auto.active = not auto.active
        auto.save(update_fields=["active"])
        messages.success(request, f"Automação {'ativada' if auto.active else 'pausada'}.")
    return redirect("automations")


@login_required
@require_role("admin")
def automation_delete(request, pk):
    if request.method == "POST":
        get_object_or_404(Automation, pk=pk, workspace=request.workspace).delete()
        messages.success(request, "Automação removida.")
    return redirect("automations")


# --------------------------------------------------------------------------- #
# Appearance / branding (admin+)
# --------------------------------------------------------------------------- #
_COLOR_RE = re.compile(r"^#[0-9a-fA-F]{6}$")


@login_required
@require_role("admin")
def appearance(request):
    ws = request.workspace
    if request.method == "POST":
        if "remove_logo" in request.POST:
            if ws.logo:
                ws.logo.delete(save=False)
                ws.logo = None
                ws.save(update_fields=["logo"])
                messages.success(request, "Logo removida.")
            return redirect("appearance")
        color = request.POST.get("brand_color", "").strip()
        if _COLOR_RE.match(color):
            ws.brand_color = color
        else:
            messages.error(request, "Cor inválida.")
        upload = request.FILES.get("logo")
        if upload:
            ws.logo = upload
        ws.save()
        messages.success(request, "Aparência atualizada.")
        return redirect("appearance")
    return render(request, "core/appearance.html", {
        "page_title": "Aparência",
        "breadcrumb": ["Configurações", "Aparência"],
        "presets": ["#2563eb", "#7c3aed", "#059669", "#dc2626", "#d97706", "#0891b2", "#db2777", "#0f172a"],
    })
