import json
import json
import re

from django.conf import settings
from django.contrib import messages
from django.contrib.auth import get_user_model
from django.contrib.auth.decorators import login_required
from django.core.exceptions import ValidationError
from django.core.validators import validate_email
from django.http import HttpResponse, JsonResponse
from django.db import transaction
from django.db.models import Q, Sum
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse
from django.utils.crypto import get_random_string
from django.utils.text import slugify
from django.views.decorators.csrf import csrf_exempt
from django.views.decorators.http import require_POST
from django_tenants.utils import get_public_schema_name, schema_context, tenant_context

from modules.crm.models import Company, Contact, Deal, Pipeline

from core.events import run_automation_for_event, CONDITION_OPERATORS
from core.tenancy import clear_current_workspace, set_current_workspace

from .forms import WorkspaceForm
from .models import EVENT_CHOICES, Automation, AutomationRun, CustomField, Domain, Event, IntegrationConnection, Membership
from .rbac import require_role


# Categorical palette for the contacts donut (validated: dataviz validator,
# light mode — CVD ΔE 39.3). Fixed order; never cycled.
_CONTACT_COLORS = {"lead": "#2563eb", "qualificado": "#d97706", "cliente": "#059669", "inativo": "#7c3aed"}
_FUNNEL_STAGES = ["novo", "qualificado", "proposta", "negociacao", "ganho"]
_INTEGRATION_CATALOG = [
    {
        "provider": "facebook",
        "name": "Facebook Lead Ads",
        "summary": "Captura leads de campanhas e envia para automacoes.",
        "icon": "users",
        "available": True,
    },
    {
        "provider": "instagram",
        "name": "Instagram",
        "summary": "Base para mensagens, formularios e origem de leads.",
        "icon": "command",
        "available": False,
    },
    {
        "provider": "forms",
        "name": "Formulários (site/LP)",
        "summary": "Formulário próprio no seu site ou landing page — respostas viram lead na automação.",
        "icon": "text",
        "available": False,
    },
    {
        "provider": "webhook",
        "name": "Webhooks",
        "summary": "Receba eventos externos e use no canvas de automacao.",
        "icon": "bolt",
        "available": True,
    },
    {
        "provider": "api",
        "name": "API / HTTP",
        "summary": "Envie dados para outras plataformas via HTTP request.",
        "icon": "command",
        "available": False,
    },
    {
        "provider": "whatsapp",
        "name": "WhatsApp",
        "summary": "Canal de atendimento para receber conversas e acionar fluxos.",
        "icon": "phone",
        "available": False,
    },
]
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
    pipelines = list(Pipeline.objects.filter(workspace=ws).order_by("order", "id"))

    # Which pipeline is the dashboard looking at? ?pipeline=<pk>, else the
    # default (or the first one). Everything below is scoped to it.
    current = None
    if pipelines:
        requested = request.GET.get("pipeline")
        if requested:
            current = next((p for p in pipelines if str(p.pk) == requested), None)
        if current is None:
            current = next((p for p in pipelines if p.is_default), pipelines[0])

    deals = Deal.objects.filter(workspace=ws)
    if current is not None:
        deals = deals.filter(pipeline=current)

    # KPIs roll up on the denormalized stage_kind, so custom stages still count
    # as open / won / lost without hardcoding stage keys.
    open_value = deals.filter(stage_kind="open").aggregate(v=Sum("value"))["v"] or 0
    won = deals.filter(stage_kind="won")
    won_value = won.aggregate(v=Sum("value"))["v"] or 0
    won_count = won.count()
    lost_count = deals.filter(stage_kind="lost").count()
    avg_ticket = (won_value / won_count) if won_count else 0
    conversion = (won_count / (won_count + lost_count) * 100) if (won_count + lost_count) else 0

    # Funnel — value per stage of the selected pipeline, in board order, painted
    # with each stage's own colour. Lost/disqualified stages aren't part of it.
    funnel = []
    if current is not None:
        stages = current.stages.exclude(kind__in=["lost", "disqualified"]).order_by("order", "id")
        for stage in stages:
            qs = deals.filter(stage=stage.key)
            funnel.append({"label": stage.name, "color": stage.color, "count": qs.count(),
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
        "pipelines": pipelines,
        "current_pipeline": current,
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
        "page_title": "Campos do CRM",
        "breadcrumb": ["Configurações", "Campos do CRM"],
        "groups": groups,
        "type_choices": CustomField.TYPE_CHOICES,
        "object_choices": CustomField.OBJECT_CHOICES,
    })


def _custom_field_label(object_type):
    return dict(CustomField.OBJECT_CHOICES).get(object_type, "Registros")


def _custom_field_modal_context(request, object_type):
    if object_type not in dict(CustomField.OBJECT_CHOICES):
        object_type = "deal"
    return {
        "object_type": object_type,
        "object_label": _custom_field_label(object_type),
        "fields": CustomField.objects.filter(workspace=request.workspace, object_type=object_type),
        "type_choices": CustomField.TYPE_CHOICES,
    }


def _custom_field_modal_response(request, object_type, message=None, kind="success"):
    resp = render(request, "core/partials/object_fields_modal.html", _custom_field_modal_context(request, object_type))
    events = {"nucleo:dataChanged": True}
    if object_type == "deal":
        events["nucleo:dealsBoard"] = True
    if message:
        events["nucleo:toast"] = {"text": message, "kind": kind}
    resp["HX-Trigger"] = json.dumps(events)
    return resp


@login_required
@require_role("admin")
def custom_fields_object(request, object_type):
    return render(request, "core/partials/object_fields_modal.html", _custom_field_modal_context(request, object_type))


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
        error = ""
        if not label:
            error = "Informe um rótulo para o campo."
        elif object_type not in dict(CustomField.OBJECT_CHOICES) or field_type not in dict(CustomField.TYPE_CHOICES):
            error = "Objeto ou tipo inválido."
        elif field_type == "select" and not options:
            error = "Para “Seleção”, informe as opções separadas por vírgula."
        if error:
            if request.POST.get("return_modal") == "1":
                return _custom_field_modal_response(request, object_type, error, "error")
            messages.error(request, error)
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
            if request.POST.get("return_modal") == "1":
                return _custom_field_modal_response(request, object_type, f"Campo “{label}” criado.")
            messages.success(request, f"Campo “{label}” criado.")
    return redirect("custom_fields")


@login_required
@require_role("admin")
def custom_field_update(request, pk):
    if request.method == "POST":
        field = get_object_or_404(CustomField, pk=pk, workspace=request.workspace)
        object_type = field.object_type
        label = request.POST.get("label", "").strip()
        if label:
            field.label = label
        field.required = request.POST.get("required") == "on"
        if field.field_type == "select":
            options = [o.strip() for o in request.POST.get("options", "").split(",") if o.strip()]
            if options:
                field.options = options
        field.save()
        if request.POST.get("return_modal") == "1":
            return _custom_field_modal_response(request, object_type, f"Campo “{field.label}” atualizado.")
        messages.success(request, f"Campo “{field.label}” atualizado.")
    return redirect("custom_fields")


@login_required
@require_role("admin")
def custom_field_delete(request, pk):
    if request.method == "POST":
        field = get_object_or_404(CustomField, pk=pk, workspace=request.workspace)
        object_type = field.object_type
        field.delete()
        if request.POST.get("return_modal") == "1":
            return _custom_field_modal_response(request, object_type, "Campo removido.")
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
def _automation_pipelines(ws):
    pipelines = list(Pipeline.objects.filter(workspace=ws).prefetch_related("stages"))
    if not pipelines:
        pipe = Pipeline.objects.create(workspace=ws, name="Vendas", is_default=True)
        pipe.ensure_stages()
        pipelines = [pipe]
    return pipelines


def _automation_custom_fields(ws):
    return list(CustomField.objects.filter(workspace=ws))


def _condition_field_catalog(ws):
    """Fields a condition rule can test (native + workspace custom fields)."""
    fields = [
        {"value": "stage", "label": "Etapa / coluna"},
        {"value": "stage_kind", "label": "Situação (aberto/ganho/perdido)"},
        {"value": "value", "label": "Valor do negócio"},
        {"value": "score", "label": "Score"},
        {"value": "owner", "label": "Responsável"},
        {"value": "tag", "label": "Etiqueta"},
        {"value": "email", "label": "E-mail (contato)"},
        {"value": "phone", "label": "Telefone (contato)"},
    ]
    obj_labels = dict(CustomField.OBJECT_CHOICES)
    for f in CustomField.objects.filter(workspace=ws):
        fields.append({
            "value": f"custom:{f.object_type}:{f.key}",
            "label": f"{f.label} · {obj_labels.get(f.object_type, f.object_type)}",
        })
    return fields


def _editor_trigger_choices(selected_trigger=None):
    """Triggers offered in the canvas. "Lead do Facebook" is owned by the
    Formulários screen (one flow per form, created there) — hide it here so nobody
    builds a parallel FB flow that would double-create. Keep it only when editing a
    flow that already uses it, so its trigger still renders."""
    return [c for c in EVENT_CHOICES if c[0] != "facebook_lead" or c[0] == selected_trigger]


def _automation_editor_context(request, selected_automation=None, **extra):
    ws = request.workspace
    fb_conn = IntegrationConnection.objects.filter(workspace=ws, provider="facebook", status="connected").first()
    selected_trigger = selected_automation.trigger if selected_automation else None
    trigger_choices = _editor_trigger_choices(selected_trigger)
    context = {
        "page_title": selected_automation.name if selected_automation else "Nova automação",
        "breadcrumb": ["Configurações", "Automações", "Canvas"],
        "automations": ws.automations.all(),
        "selected_automation": selected_automation,
        "automation_name": selected_automation.name if selected_automation else "",
        "automation_icon": selected_automation.icon if selected_automation else "bolt",
        "automation_icons": Automation.ICON_CHOICES,
        "initial_canvas": selected_automation.canvas if selected_automation else {},
        "triggers": trigger_choices,
        "actions": Automation.ACTION_CHOICES,
        "deal_stages": Deal.STAGE_CHOICES,
        "contact_stages": Contact.STAGE_CHOICES,
        "pipelines": _automation_pipelines(ws),
        "automation_custom_fields": _automation_custom_fields(ws),
        "condition_fields": _condition_field_catalog(ws),
        "condition_operators": CONDITION_OPERATORS,
        "facebook_forms": (fb_conn.config or {}).get("forms", []) if fb_conn else [],
    }
    context.update(extra)
    return context


@login_required
@require_role("admin")
def automations(request):
    ws = request.workspace
    return render(request, "core/automations.html", {
        "page_title": "Automações",
        "breadcrumb": ["Configurações", "Automações"],
        "automations": ws.automations.all(),
        "recent_runs": AutomationRun.objects.filter(workspace=ws).select_related("automation")[:8],
    })


@login_required
@require_role("admin")
def automation_editor(request, pk=None):
    ws = request.workspace
    selected_automation = None
    if pk is not None:
        selected_automation = get_object_or_404(Automation, pk=pk, workspace=ws)
    return render(request, "core/automation_editor.html", _automation_editor_context(request, selected_automation))


@login_required
@require_role("admin")
def automation_add(request):
    if request.method == "POST":
        ws = request.workspace
        name = request.POST.get("name", "").strip()
        icon = _automation_icon(request.POST.get("icon", "bolt"))
        trigger = request.POST.get("trigger")
        automation_id = request.POST.get("automation_id", "").strip()
        existing = ws.automations.filter(pk=int(automation_id)).first() if automation_id.isdigit() else None
        canvas = _parse_automation_canvas(request)
        canvas = _ensure_trigger_config(canvas, trigger)
        condition_stage = _automation_condition_stage(request, trigger, canvas)
        actions = _order_actions_by_canvas(_parse_automation_actions(request), canvas)
        first = actions[0] if actions else {}
        save_mode = request.POST.get("save_mode", "publish")
        if save_mode == "simulate":
            result = _simulate_automation(canvas, actions, trigger)
            messages.info(request, "Simulação gerada sem publicar o fluxo.")
            return render(request, "core/automation_editor.html", _automation_editor_context(
                request,
                existing,
                automation_name=name,
                automation_icon=icon,
                initial_canvas=canvas,
                test_result=result,
            ))
        if not name:
            messages.error(request, "Informe o nome da automação.")
        elif trigger not in dict(EVENT_CHOICES):
            messages.error(request, "Gatilho inválido.")
        elif trigger == "facebook_lead" and not (existing and existing.trigger == "facebook_lead"):
            # FB flows are born in Integrações → Formulários (one per form). The
            # canvas only edits an already-generated one — never creates a parallel.
            messages.error(request, "Fluxos do Facebook são criados em Integrações → Facebook → Formulários.")
        elif not actions:
            messages.error(request, "Configure pelo menos uma ação no fluxo.")
        else:
            defaults = {
                "name": name,
                "icon": icon,
                "trigger": trigger,
                "condition_stage": condition_stage,
                "action": first.get("type", "create_task"),
                "action_text": first.get("text", ""),
                "action_due_days": first.get("due_days", 0),
                "conditions": {"stage": condition_stage} if condition_stage else {},
                "actions": actions,
                "canvas": canvas,
                "active": save_mode == "publish",
            }
            if existing:
                for field, value in defaults.items():
                    setattr(existing, field, value)
                existing.save(update_fields=list(defaults))
                auto = existing
            else:
                auto = Automation.objects.create(workspace=ws, **defaults)
            messages.success(request, "Fluxo publicado." if save_mode == "publish" else "Rascunho salvo.")
            return redirect("automation_edit", pk=auto.pk)
        return render(request, "core/automation_editor.html", _automation_editor_context(
            request,
            existing,
            automation_name=name,
            automation_icon=icon,
            initial_canvas=canvas,
        ))
    return redirect("automations")


def _automation_icon(value):
    valid = {key for key, _ in Automation.ICON_CHOICES}
    return value if value in valid else "bolt"


def _ensure_trigger_config(canvas, trigger):
    if not isinstance(canvas, dict) or not canvas.get("nodes"):
        return canvas
    trigger_node = next((node for node in canvas.get("nodes", []) if node.get("type") == "trigger"), None)
    if not trigger_node:
        return canvas
    data = trigger_node.setdefault("data", {})
    data["trigger"] = trigger or data.get("trigger", "")
    if trigger == "schedule_interval":
        amount = _canvas_int(data.get("trigger_interval_amount") or data.get("trigger_interval_minutes") or 1, 1, 10080)
        unit = data.get("trigger_interval_unit") or "hours"
        if unit not in {"minutes", "hours", "days"}:
            unit = "hours"
        data["trigger_interval_amount"] = amount
        data["trigger_interval_unit"] = unit
        data["trigger_interval_minutes"] = amount * {"minutes": 1, "hours": 60, "days": 1440}[unit]
    if trigger == "webhook_received" and not data.get("webhook_key"):
        data["webhook_key"] = "wh_" + get_random_string(24).lower()
    return canvas


def _automation_condition_stage(request, trigger, canvas=None):
    for node in (canvas or {}).get("nodes", []):
        if node.get("type") not in {"filter", "condition", "switch"}:
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
        if action_type == "create_contact":
            for field in ("contact_first_name", "contact_last_name", "contact_email", "contact_phone", "contact_job_title", "contact_stage"):
                value = request.POST.get(f"action{i}_{field}", "").strip()
                if value:
                    action[field] = value
        if action_type == "create_company":
            for field in ("company_name", "company_domain", "company_industry", "company_city"):
                value = request.POST.get(f"action{i}_{field}", "").strip()
                if value:
                    action[field] = value
        if action_type == "create_task":
            try:
                action["due_days"] = max(0, int(request.POST.get(f"action{i}_due_days") or 0))
            except ValueError:
                action["due_days"] = 0
        if action_type == "set_custom_field":
            custom_key = request.POST.get(f"action{i}_custom_key", "").strip()
            custom_value = request.POST.get(f"action{i}_custom_value", "").strip()
            if custom_key:
                action["custom_key"] = custom_key
                action["custom_value"] = custom_value
        if action_type == "delay":
            amount = _canvas_int(
                request.POST.get(f"action{i}_delay_amount") or request.POST.get(f"action{i}_delay_minutes"),
                0,
                3650,
            )
            unit = request.POST.get(f"action{i}_delay_unit", "minutes").strip()
            if unit not in {"minutes", "hours", "days"}:
                unit = "minutes"
            action["delay_amount"] = amount
            action["delay_unit"] = unit
            action["delay_until"] = request.POST.get(f"action{i}_delay_until", "").strip()
            action["delay_minutes"] = amount * {"minutes": 1, "hours": 60, "days": 1440}[unit]
        if action_type == "send_webhook":
            webhook_url = request.POST.get(f"action{i}_webhook_url", "").strip()
            if webhook_url:
                action["webhook_url"] = webhook_url
        if action_type == "http_request":
            method = request.POST.get(f"action{i}_http_method", "POST").strip().upper()
            action["http_method"] = method if method in {"GET", "POST", "PUT", "PATCH", "DELETE"} else "POST"
            action["http_url"] = request.POST.get(f"action{i}_http_url", "").strip()
            action["http_auth_type"] = request.POST.get(f"action{i}_http_auth_type", "none").strip()
            if action["http_auth_type"] not in {"none", "bearer", "basic"}:
                action["http_auth_type"] = "none"
            action["http_auth_token"] = request.POST.get(f"action{i}_http_auth_token", "").strip()
            action["http_username"] = request.POST.get(f"action{i}_http_username", "").strip()
            action["http_password"] = request.POST.get(f"action{i}_http_password", "").strip()
            action["http_send_query"] = "1" if request.POST.get(f"action{i}_http_send_query") else ""
            action["http_query"] = (
                request.POST.get(f"action{i}_http_query", "").strip()
                or _http_query_from_pairs(request, i)
            )
            action["http_send_headers"] = "1" if request.POST.get(f"action{i}_http_send_headers") else ""
            action["http_headers"] = request.POST.get(f"action{i}_http_headers", "").strip()
            action["http_content_type"] = request.POST.get(f"action{i}_http_content_type", "json").strip()
            if action["http_content_type"] not in {"json", "text"}:
                action["http_content_type"] = "json"
            action["http_send_body"] = "1" if request.POST.get(f"action{i}_http_send_body") else ""
            action["http_body"] = request.POST.get(f"action{i}_http_body", "").strip()
            try:
                action["http_timeout"] = max(1, int(request.POST.get(f"action{i}_http_timeout") or 10))
            except ValueError:
                action["http_timeout"] = 10
        actions.append(action)
    return actions


def _http_query_from_pairs(request, index):
    query = {}
    for row in range(1, 6):
        key = request.POST.get(f"action{index}_http_query_name_{row}", "").strip()
        if not key:
            continue
        query[key] = request.POST.get(f"action{index}_http_query_value_{row}", "")
    return json.dumps(query) if query else ""


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
        if not node_id or node_id in node_ids or node_type not in {"trigger", "filter", "condition", "switch", "merge", "loop", "rule", "action"}:
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
        branch = str(edge.get("branch", ""))
        if branch not in ("true", "false"):
            branch = ""
        key = (source, target, branch)
        if not source or not target or source == target or source not in node_ids or target not in node_ids:
            continue
        if key in seen_edges:
            continue
        seen_edges.add(key)
        edge_clean = {"from": source, "to": target}
        if branch:
            edge_clean["branch"] = branch
        edges.append(edge_clean)

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


def _clean_condition(value):
    """Validate a rich condition {match, rules:[{field,op,value}]} from the canvas."""
    if not isinstance(value, dict):
        return None
    match = "any" if str(value.get("match")) == "any" else "all"
    rules = []
    for rule in (value.get("rules") or [])[:20]:
        if not isinstance(rule, dict):
            continue
        field = str(rule.get("field", ""))[:80]
        if not field:
            continue
        rules.append({
            "field": field,
            "op": str(rule.get("op", "eq"))[:20],
            "value": str(rule.get("value", ""))[:300],
        })
    if not rules:
        return None
    return {"match": match, "rules": rules}


def _clean_canvas_data(data):
    if not isinstance(data, dict):
        return {}
    allowed = {
        "trigger",
        "trigger_interval_amount",
        "trigger_interval_unit",
        "trigger_interval_minutes",
        "trigger_form_id",
        "webhook_key",
        "condition_kind",
        "condition_stage",
        "condition_custom_key",
        "condition_custom_value",
        "condition",
        "merge_mode",
        "loop_mode",
        "loop_count",
        "rule_type",
        "rule_days",
        "action_index",
        "action_type",
        "pipeline",
        "deal_stage",
        "contact_stage",
        "contact_first_name",
        "contact_last_name",
        "contact_email",
        "contact_phone",
        "contact_job_title",
        "company_name",
        "company_domain",
        "company_industry",
        "company_city",
        "custom_key",
        "custom_value",
        "delay_amount",
        "delay_unit",
        "delay_until",
        "delay_minutes",
        "webhook_url",
        "http_method",
        "http_url",
        "http_auth_type",
        "http_auth_token",
        "http_username",
        "http_password",
        "http_send_query",
        "http_query",
        "http_send_headers",
        "http_headers",
        "http_content_type",
        "http_send_body",
        "http_body",
        "http_timeout",
        "text",
        "due_days",
    }
    clean = {}
    for key, value in data.items():
        if key not in allowed:
            continue
        if key == "condition":
            cleaned = _clean_condition(value)
            if cleaned:
                clean[key] = cleaned
        elif key == "loop_count":
            clean[key] = _canvas_int(value, 1, 10000)
        elif key in {"action_index", "due_days", "delay_amount", "delay_minutes", "rule_days", "http_timeout", "trigger_interval_amount"}:
            clean[key] = _canvas_int(value, 0, 3650)
        elif key == "trigger_interval_minutes":
            clean[key] = _canvas_int(value, 1, 10080)
        elif key == "trigger_interval_unit":
            clean[key] = str(value) if str(value) in {"minutes", "hours", "days"} else "hours"
        elif key == "delay_unit":
            clean[key] = str(value) if str(value) in {"minutes", "hours", "days"} else "minutes"
        elif key == "loop_mode":
            clean[key] = str(value) if str(value) in {"for_each", "batch", "repeat_times"} else "for_each"
        elif key in {"http_send_query", "http_send_headers", "http_send_body"}:
            clean[key] = "1" if str(value).lower() in {"1", "true", "on", "yes"} else ""
        elif key in {"http_auth_type", "http_content_type"}:
            clean[key] = str(value)[:40]
        elif key == "webhook_key":
            clean[key] = re.sub(r"[^a-zA-Z0-9_-]", "", str(value))[:80]
        elif key in {"http_headers", "http_body", "http_query"}:
            clean[key] = str(value)[:5000]
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


def _simulate_automation(canvas, actions, trigger):
    nodes = {node.get("id"): node for node in canvas.get("nodes", [])}
    outgoing = {}
    incoming_count = {}
    for edge in canvas.get("edges", []):
        source = edge.get("from")
        target = edge.get("to")
        outgoing.setdefault(source, []).append(target)
        incoming_count[target] = incoming_count.get(target, 0) + 1
    trigger_nodes = [node_id for node_id, node in nodes.items() if node.get("type") == "trigger"]
    current = trigger_nodes[0] if trigger_nodes else "entry"
    trigger_data = nodes.get(current, {}).get("data", {}) if current in nodes else {}
    trigger_message = dict(EVENT_CHOICES).get(trigger, "Escolha um gatilho para publicar.")
    if trigger == "schedule_interval":
        trigger_message = _schedule_summary(trigger_data)
    elif trigger == "webhook_received":
        webhook_key = trigger_data.get("webhook_key") or "sera gerado ao salvar"
        trigger_message = f"Receber POST no webhook {webhook_key}."
    steps = [{
        "label": "Entrada",
        "status": "ok" if trigger else "warning",
        "message": trigger_message,
    }]
    action_labels = dict(Automation.ACTION_CHOICES)
    queue = [current]
    edge_visits = {}
    merge_arrivals = {}
    released_merges = set()
    max_steps = max(20, len(nodes) * 12)
    step_count = 0

    while queue and step_count < max_steps:
        current = queue.pop(0)
        next_nodes = [node_id for node_id in outgoing.get(current, []) if node_id in nodes]
        if not next_nodes:
            continue
        for node_id in next_nodes:
            visit_key = (current, node_id)
            edge_visits[visit_key] = edge_visits.get(visit_key, 0) + 1
            if edge_visits[visit_key] > 30:
                steps.append({"label": "Loop", "status": "error", "message": "O canvas tem um loop nesta rota."})
                queue = []
                break
            step_count += 1
            if step_count >= max_steps:
                steps.append({"label": "Loop", "status": "error", "message": "Limite da simulacao atingido."})
                queue = []
                break
            node = nodes[node_id]
            data = node.get("data") or {}
            node_type = node.get("type")
            if node_type in {"filter", "condition", "switch"}:
                msg = "Condicao configurada."
                if data.get("condition_custom_key"):
                    msg = f"Campo {data.get('condition_custom_key')} deve ser {data.get('condition_custom_value') or 'preenchido'}."
                elif data.get("condition_stage"):
                    msg = f"Etapa/estagio deve ser {data.get('condition_stage')}."
                label = {"condition": "If / condicao", "switch": "Roteador"}.get(node_type, "Filtro")
                steps.append({"label": label, "status": "ok", "message": msg})
                queue.append(node_id)
            elif node_type == "merge":
                merge_arrivals[node_id] = merge_arrivals.get(node_id, 0) + 1
                expected = max(1, incoming_count.get(node_id, 1))
                if data.get("merge_mode") == "wait_all" and merge_arrivals[node_id] < expected:
                    steps.append({"label": "Mesclar", "status": "ok", "message": f"Aguardando entradas {merge_arrivals[node_id]}/{expected}."})
                    continue
                if node_id in released_merges:
                    continue
                released_merges.add(node_id)
                steps.append({"label": "Mesclar", "status": "ok", "message": "Entradas unidas para continuar o fluxo."})
                queue.append(node_id)
            elif node_type == "loop":
                mode = data.get("loop_mode") or "for_each"
                count = data.get("loop_count") or 1
                labels = {
                    "for_each": "Executar para cada item recebido.",
                    "batch": f"Processar em lotes de {count}.",
                    "repeat_times": f"Repetir os proximos passos {count} vez(es).",
                }
                steps.append({"label": "Loop", "status": "ok", "message": labels.get(mode, "Loop configurado.")})
                repeat = _canvas_int(count, 1, 30) if mode == "repeat_times" else 1
                for _ in range(repeat):
                    queue.append(node_id)
            elif node_type == "rule":
                rule = data.get("rule_type") or "once_per_record"
                steps.append({"label": "Regra", "status": "ok", "message": _rule_summary(rule, data)})
                queue.append(node_id)
            elif node_type == "action":
                action_type = data.get("action_type")
                status = "ok"
                msg = action_labels.get(action_type, "Acao nao configurada.")
                if action_type == "delay":
                    status = "warning"
                    msg = f"{_delay_summary(data)} Requer worker/cron para retomar depois."
                elif action_type == "send_webhook":
                    msg = f"Enviar webhook para {data.get('webhook_url') or 'URL nao informada'}."
                    if not data.get("webhook_url"):
                        status = "warning"
                elif action_type == "http_request":
                    method = data.get("http_method") or "POST"
                    auth = data.get("http_auth_type") or "none"
                    auth_msg = " sem autenticacao" if auth == "none" else f" com auth {auth}"
                    msg = f"HTTP {method} para {data.get('http_url') or 'URL nao informada'}{auth_msg}."
                    if not data.get("http_url"):
                        status = "warning"
                elif action_type == "set_custom_field":
                    msg = f"Atualizar {data.get('custom_key') or 'campo'} para {data.get('custom_value') or 'valor vazio'}."
                elif action_type == "create_contact":
                    msg = f"Criar contato {data.get('contact_first_name') or 'sem nome'}."
                elif action_type == "create_company":
                    msg = f"Criar empresa {data.get('company_name') or 'sem nome'}."
                steps.append({"label": action_labels.get(action_type, "Acao"), "status": status, "message": msg})
                queue.append(node_id)

    if not any(step.get("label") in action_labels.values() for step in steps) and not actions:
        steps.append({"label": "Acoes", "status": "warning", "message": "Nenhuma acao configurada nesta rota."})
    return steps

    for _ in range(len(nodes) + 1):
        next_nodes = [node_id for node_id in outgoing.get(current, []) if node_id in nodes]
        if not next_nodes:
            break
        current = next_nodes[0]
        if current in seen:
            steps.append({"label": "Loop", "status": "error", "message": "O canvas tem um loop nesta rota."})
            break
        seen.add(current)
        node = nodes[current]
        data = node.get("data") or {}
        node_type = node.get("type")
        if node_type in {"filter", "condition", "switch"}:
            msg = "Condição configurada."
            if data.get("condition_custom_key"):
                msg = f"Campo {data.get('condition_custom_key')} deve ser {data.get('condition_custom_value') or 'preenchido'}."
            elif data.get("condition_stage"):
                msg = f"Etapa/estágio deve ser {data.get('condition_stage')}."
            label = {"condition": "If / condição", "switch": "Roteador"}.get(node_type, "Filtro")
            steps.append({"label": label, "status": "ok", "message": msg})
        elif node_type == "merge":
            steps.append({"label": "Mesclar", "status": "ok", "message": "Entradas unidas para continuar o fluxo."})
        elif node_type == "loop":
            mode = data.get("loop_mode") or "for_each"
            count = data.get("loop_count") or 1
            labels = {
                "for_each": "Executar para cada item recebido.",
                "batch": f"Processar em lotes de {count}.",
                "repeat_times": f"Repetir os proximos passos {count} vez(es).",
            }
            steps.append({"label": "Loop", "status": "ok", "message": labels.get(mode, "Loop configurado.")})
        elif node_type == "rule":
            rule = data.get("rule_type") or "once_per_record"
            steps.append({"label": "Regra", "status": "ok", "message": _rule_summary(rule, data)})
        elif node_type == "action":
            action_type = data.get("action_type")
            status = "ok"
            msg = action_labels.get(action_type, "Ação não configurada.")
            if action_type == "delay":
                status = "warning"
                msg = f"{_delay_summary(data)} Requer worker/cron para retomar depois."
            elif action_type == "send_webhook":
                msg = f"Enviar webhook para {data.get('webhook_url') or 'URL não informada'}."
                if not data.get("webhook_url"):
                    status = "warning"
            elif action_type == "http_request":
                method = data.get("http_method") or "POST"
                auth = data.get("http_auth_type") or "none"
                auth_msg = " sem autenticacao" if auth == "none" else f" com auth {auth}"
                msg = f"HTTP {method} para {data.get('http_url') or 'URL nao informada'}{auth_msg}."
                if not data.get("http_url"):
                    status = "warning"
            elif action_type == "set_custom_field":
                msg = f"Atualizar {data.get('custom_key') or 'campo'} para {data.get('custom_value') or 'valor vazio'}."
            steps.append({"label": action_labels.get(action_type, "Ação"), "status": status, "message": msg})

    if not any(step.get("label") in action_labels.values() for step in steps) and not actions:
        steps.append({"label": "Ações", "status": "warning", "message": "Nenhuma ação configurada nesta rota."})
    return steps


def _delay_summary(data):
    if data.get("delay_until"):
        return f"Aguardar ate {data.get('delay_until')}."
    amount = data.get("delay_amount") or data.get("delay_minutes") or 0
    unit = {
        "minutes": "minuto(s)",
        "hours": "hora(s)",
        "days": "dia(s)",
    }.get(data.get("delay_unit"), "minuto(s)")
    return f"Aguardar {amount} {unit}."


def _schedule_summary(data):
    amount = data.get("trigger_interval_amount") or data.get("trigger_interval_minutes") or 1
    unit = {
        "minutes": "minuto(s)",
        "hours": "hora(s)",
        "days": "dia(s)",
    }.get(data.get("trigger_interval_unit"), "hora(s)")
    return f"Rodar a cada {amount} {unit}."


def _rule_summary(rule, data):
    if rule == "once_per_record":
        return "O mesmo registro só entra uma vez neste fluxo."
    if rule == "cooldown_days":
        return f"Bloquear repetição por {data.get('rule_days') or 0} dia(s)."
    if rule == "stop_if_open_deal":
        return "Parar se o contato já tiver negócio aberto."
    return "Regra configurada."


@login_required
def whatsapp(request):
    connection = IntegrationConnection.objects.filter(workspace=request.workspace, provider="whatsapp").first()
    if request.method == "POST":
        membership = getattr(request, "membership", None)
        if not (membership and membership.can("admin")):
            messages.error(request, "Voce nao tem permissao para configurar o WhatsApp.")
            return redirect("whatsapp")
        display_name = request.POST.get("display_name", "").strip()
        phone_number = request.POST.get("phone_number", "").strip()
        default_owner = request.POST.get("default_owner", "").strip()
        connection, _ = IntegrationConnection.objects.get_or_create(
            workspace=request.workspace,
            provider="whatsapp",
            defaults={"name": "WhatsApp"},
        )
        connection.name = "WhatsApp"
        connection.status = "connected" if phone_number or display_name else "disconnected"
        connection.config = {
            **(connection.config or {}),
            "display_name": display_name,
            "phone_number": phone_number,
            "default_owner": default_owner,
            "source": "whatsapp_tab",
            "updated_by": request.user.get_username(),
        }
        connection.save(update_fields=["name", "status", "config", "updated_at"])
        messages.success(request, "Canal do WhatsApp salvo.")
        return redirect("whatsapp")

    config = connection.config if connection else {}
    return render(request, "core/whatsapp.html", {
        "page_title": "WhatsApp",
        "breadcrumb": ["CRM", "WhatsApp"],
        "connection": connection,
        "whatsapp_config": config,
        "whatsapp_connected": bool(connection and connection.status == "connected"),
    })


@login_required
@require_role("admin")
def integrations(request):
    ws = request.workspace
    connections = {item.provider: item for item in IntegrationConnection.objects.filter(workspace=ws)}
    cards = []
    for item in _INTEGRATION_CATALOG:
        connection = connections.get(item["provider"])
        cards.append({
            **item,
            "connection": connection,
            "connected": bool(connection and connection.status == "connected"),
        })
    return render(request, "core/integrations.html", {
        "page_title": "Integrações",
        "breadcrumb": ["Configurações", "Integrações"],
        "integration_cards": cards,
    })


@login_required
@require_role("admin")
@require_POST
def integration_connect(request):
    provider = request.POST.get("provider", "").strip()
    catalog = {item["provider"]: item for item in _INTEGRATION_CATALOG}
    item = catalog.get(provider)
    if not item:
        messages.error(request, "Integração inválida.")
        return redirect("integrations")
    if not item.get("available"):
        messages.info(request, "Integração preparada, mas ainda não disponível para conectar.")
        return redirect("integrations")
    connection, _ = IntegrationConnection.objects.get_or_create(
        workspace=request.workspace,
        provider=provider,
        defaults={"name": item["name"]},
    )
    connection.name = item["name"]
    connection.status = "connected"
    connection.config = {
        **(connection.config or {}),
        "connected_by": request.user.get_username(),
        "source": "manual",
    }
    connection.save(update_fields=["name", "status", "config", "updated_at"])
    messages.success(request, f"{item['name']} conectada.")
    return redirect("integrations")


@login_required
@require_role("admin")
@require_POST
def integration_disconnect(request):
    provider = request.POST.get("provider", "").strip()
    connection = IntegrationConnection.objects.filter(workspace=request.workspace, provider=provider).first()
    if connection:
        connection.status = "disconnected"
        # Fully clear the stored page/token so it doesn't keep showing as connected
        # and a fresh "Conectar página" starts clean.
        connection.config = {}
        connection.save(update_fields=["status", "config", "updated_at"])
        messages.success(request, f"{connection.name} desconectada.")
    return redirect("integrations")


# --------------------------------------------------------------------------- #
# Facebook Lead Ads — OAuth "connect page" flow (like Kommo).
#   connect -> Facebook consent -> callback lists the user's Pages ->
#   pick a Page -> store its token + subscribe it to leadgen webhooks.
# The Graph calls need a real Meta app (App ID/Secret) + public HTTPS callback,
# so they can't be exercised locally — the flow degrades gracefully without them.
# --------------------------------------------------------------------------- #
FB_SCOPES = "pages_show_list,pages_read_engagement,pages_manage_metadata,leads_retrieval"


def _facebook_redirect_uri(request):
    return request.build_absolute_uri(reverse("facebook_callback"))


def _fb_graph(path, params, method="GET"):
    import urllib.parse
    import urllib.request

    base = f"https://graph.facebook.com/{settings.FACEBOOK_GRAPH_VERSION}/{path}"
    if method == "POST":
        req = urllib.request.Request(base, data=urllib.parse.urlencode(params).encode(), method="POST")
    else:
        req = urllib.request.Request(base + "?" + urllib.parse.urlencode(params))
    with urllib.request.urlopen(req, timeout=10) as resp:
        return json.loads(resp.read().decode("utf-8"))


@login_required
@require_role("admin")
def facebook_connect(request):
    if not settings.FACEBOOK_APP_ID or not settings.FACEBOOK_APP_SECRET:
        messages.error(request, "Configure FACEBOOK_APP_ID e FACEBOOK_APP_SECRET no ambiente para conectar o Facebook.")
        return redirect("integrations")
    import urllib.parse

    state = get_random_string(24)
    request.session["fb_oauth_state"] = state
    params = urllib.parse.urlencode({
        "client_id": settings.FACEBOOK_APP_ID,
        "redirect_uri": _facebook_redirect_uri(request),
        "scope": FB_SCOPES,
        "response_type": "code",
        "state": state,
    })
    return redirect(f"https://www.facebook.com/{settings.FACEBOOK_GRAPH_VERSION}/dialog/oauth?{params}")


@login_required
@require_role("admin")
def facebook_callback(request):
    if request.GET.get("error"):
        messages.info(request, "Conexão com o Facebook cancelada.")
        return redirect("integrations")
    if not request.GET.get("state") or request.GET.get("state") != request.session.get("fb_oauth_state"):
        messages.error(request, "A sessão do Facebook expirou. Conecte de novo.")
        return redirect("integrations")
    code = request.GET.get("code")
    if not code:
        messages.error(request, "O Facebook não retornou o código de autorização.")
        return redirect("integrations")
    try:
        token_data = _fb_graph("oauth/access_token", {
            "client_id": settings.FACEBOOK_APP_ID,
            "client_secret": settings.FACEBOOK_APP_SECRET,
            "redirect_uri": _facebook_redirect_uri(request),
            "code": code,
        })
        user_token = token_data.get("access_token", "")
        # Exchange the short-lived token for a long-lived one (~60 days) so the user
        # can switch pages later without another trip through the Facebook dialog.
        try:
            longlived = _fb_graph("oauth/access_token", {
                "grant_type": "fb_exchange_token",
                "client_id": settings.FACEBOOK_APP_ID,
                "client_secret": settings.FACEBOOK_APP_SECRET,
                "fb_exchange_token": user_token,
            })
            user_token = longlived.get("access_token") or user_token
        except Exception:
            pass
        pages_data = _fb_graph("me/accounts", {"access_token": user_token, "limit": 100})
    except Exception:
        messages.error(request, "Falha ao falar com o Facebook. Verifique o App e tente de novo.")
        return redirect("integrations")
    pages = [
        {"id": p.get("id"), "name": p.get("name", "Página"), "access_token": p.get("access_token", "")}
        for p in pages_data.get("data", []) if p.get("id")
    ]
    if not pages:
        messages.error(request, "Nenhuma página do Facebook encontrada nessa conta.")
        return redirect("integrations")
    request.session["fb_pages"] = pages
    request.session["fb_user_token"] = user_token
    return render(request, "core/facebook_pages.html", {
        "page_title": "Conectar Facebook",
        "breadcrumb": ["Configurações", "Integrações", "Facebook"],
        "pages": pages,
    })


@login_required
@require_role("admin")
@require_POST
def facebook_select_page(request):
    page_id = request.POST.get("page_id", "")
    pages = request.session.get("fb_pages", [])
    page = next((p for p in pages if str(p.get("id")) == str(page_id)), None)
    if not page:
        messages.error(request, "Página inválida. Conecte de novo.")
        return redirect("integrations")
    subscribed = False
    try:
        result = _fb_graph(
            f"{page['id']}/subscribed_apps",
            {"subscribed_fields": "leadgen", "access_token": page["access_token"]},
            method="POST",
        )
        subscribed = bool(result.get("success"))
    except Exception:
        subscribed = False
    conn, _ = IntegrationConnection.objects.get_or_create(
        workspace=request.workspace, provider="facebook", defaults={"name": "Facebook Lead Ads"},
    )
    conn.name = "Facebook Lead Ads"
    conn.status = "connected"
    conn.config = {
        **(conn.config or {}),
        "page_id": page["id"],
        "page_name": page["name"],
        "page_access_token": page["access_token"],
        # Keep the (long-lived) user token so "Trocar página" can re-list the pages
        # without sending the user back through the Facebook dialog.
        "user_access_token": request.session.get("fb_user_token") or (conn.config or {}).get("user_access_token", ""),
        "leadgen_subscribed": subscribed,
        "connected_by": request.user.get_username(),
        "source": "oauth",
    }
    conn.save(update_fields=["name", "status", "config", "updated_at"])
    # Best-effort: cache the page's lead forms so the automation trigger can offer
    # a form picker right away (refreshable later in Integrações → Formulários).
    forms = _facebook_list_forms(conn)
    if forms:
        conn.config = {**conn.config, "forms": forms}
        conn.save(update_fields=["config", "updated_at"])
    request.session.pop("fb_pages", None)
    request.session.pop("fb_oauth_state", None)
    request.session.pop("fb_user_token", None)
    if subscribed:
        messages.success(request, f"Página “{page['name']}” conectada. Novos leads viram contato e negócio automaticamente.")
    else:
        messages.warning(request, f"Página “{page['name']}” salva, mas não consegui assinar os leads — confira as permissões do App.")
    return redirect("integrations")


@login_required
@require_role("admin")
def facebook_switch_page(request):
    """Switch the connected page WITHOUT another Facebook dialog: reuse the stored
    (long-lived) user token to re-list the pages. Falls back to full OAuth if the
    token is missing or expired."""
    conn = _facebook_active_connection(request.workspace)
    token = (conn.config or {}).get("user_access_token") if conn else ""
    if not token:
        return redirect("facebook_connect")
    try:
        pages_data = _fb_graph("me/accounts", {"access_token": token, "limit": 100})
    except Exception:
        messages.info(request, "Sua sessão do Facebook expirou — conecte de novo.")
        return redirect("facebook_connect")
    pages = [
        {"id": p.get("id"), "name": p.get("name", "Página"), "access_token": p.get("access_token", "")}
        for p in pages_data.get("data", []) if p.get("id")
    ]
    if not pages:
        messages.error(request, "Nenhuma página encontrada na sua conta do Facebook.")
        return redirect("integrations")
    request.session["fb_pages"] = pages
    request.session["fb_user_token"] = token
    return render(request, "core/facebook_pages.html", {
        "page_title": "Trocar página do Facebook",
        "breadcrumb": ["Configurações", "Integrações", "Facebook"],
        "pages": pages,
        "switching": True,
    })


# --------------------------------------------------------------------------- #
# Facebook lead forms — list, per-form field mapping (Kommo/RD parity).
#   Integrações → Formulários lists the page's forms; each form has a mapping
#   screen (question -> CRM field). The map lives in conn.config["form_maps"].
# --------------------------------------------------------------------------- #
# Standard destination fields offered in the mapping <select>, grouped by object.
# The token is resolved to a `body` key the create actions already understand.
_FB_STANDARD_TARGETS = [
    ("contact", "Contato", [
        ("contact:name", "Nome completo"),
        ("contact:first_name", "Nome"),
        ("contact:last_name", "Sobrenome"),
        ("contact:email", "Email"),
        ("contact:phone", "Telefone"),
        ("contact:job_title", "Cargo"),
    ]),
    ("company", "Empresa", [
        ("company:name", "Nome da empresa"),
        ("company:domain", "Site / Domínio"),
        ("company:city", "Cidade"),
        ("company:industry", "Segmento"),
    ]),
    ("deal", "Negócio", [
        ("deal:title", "Título do negócio"),
    ]),
]

# token -> the key the create actions (_create_contact/company/deal) read from body.
_FB_STANDARD_BODY_KEYS = {
    "contact:name": "name",
    "contact:first_name": "first_name",
    "contact:last_name": "last_name",
    "contact:email": "email",
    "contact:phone": "phone",
    "contact:job_title": "job_title",
    "company:name": "company",
    "company:domain": "domain",
    "company:city": "city",
    "company:industry": "industry",
    "deal:title": "title",
}

# Best-guess mapping: normalized question name -> target token.
_FB_SUGGEST = {
    "email": "contact:email", "e_mail": "contact:email", "email_address": "contact:email",
    "full_name": "contact:name", "name": "contact:name", "nome": "contact:name",
    "nome_completo": "contact:name", "fullname": "contact:name",
    "first_name": "contact:first_name", "primeiro_nome": "contact:first_name",
    "last_name": "contact:last_name", "sobrenome": "contact:last_name", "surname": "contact:last_name",
    "phone": "contact:phone", "phone_number": "contact:phone", "telefone": "contact:phone",
    "celular": "contact:phone", "whatsapp": "contact:phone",
    "job_title": "contact:job_title", "cargo": "contact:job_title",
    "company": "company:name", "company_name": "company:name", "empresa": "company:name",
    "city": "company:city", "cidade": "company:city",
}


def _facebook_active_connection(ws):
    return IntegrationConnection.objects.filter(workspace=ws, provider="facebook", status="connected").first()


def _facebook_list_forms(conn):
    """List the connected page's lead forms via Graph. Returns [{id,name,status}]."""
    cfg = conn.config or {}
    page_id = cfg.get("page_id")
    token = cfg.get("page_access_token")
    if not page_id or not token:
        return []
    try:
        data = _fb_graph(f"{page_id}/leadgen_forms", {"access_token": token, "fields": "id,name,status", "limit": 200})
    except Exception:
        return []
    forms = []
    for item in data.get("data", []) if isinstance(data, dict) else []:
        if isinstance(item, dict) and item.get("id"):
            forms.append({
                "id": str(item["id"]),
                "name": item.get("name") or "Formulário",
                "status": item.get("status", ""),
            })
    return forms


def _facebook_form_questions(conn, form_id):
    """Fetch a form's questions from Graph: [{key,label,type}]."""
    token = (conn.config or {}).get("page_access_token")
    if not token or not form_id:
        return []
    try:
        data = _fb_graph(str(form_id), {"access_token": token, "fields": "name,questions"})
    except Exception:
        return []
    out = []
    for question in (data.get("questions") or []) if isinstance(data, dict) else []:
        if not isinstance(question, dict):
            continue
        key = str(question.get("key") or question.get("id") or "").strip()
        if not key:
            continue
        out.append({"key": key, "label": question.get("label") or key, "type": question.get("type", "")})
    return out


def _facebook_target_catalog(ws):
    """Grouped (token, label) options for the mapping <select>, incl. custom fields."""
    custom_by_obj = {"contact": [], "company": [], "deal": []}
    custom_fields = list(CustomField.objects.filter(workspace=ws)) if ws is not None else []
    for field in custom_fields:
        if field.object_type in custom_by_obj:
            custom_by_obj[field.object_type].append((f"custom:{field.object_type}:{field.key}", field.label))
    groups = []
    for obj, label, std in _FB_STANDARD_TARGETS:
        groups.append({"object": obj, "label": label, "options": list(std) + custom_by_obj.get(obj, [])})
    return groups


def _facebook_has_custom_fields(ws):
    return bool(ws) and CustomField.objects.filter(workspace=ws).exists()


def _facebook_valid_targets(ws):
    tokens = {"ignore"}
    for group in _facebook_target_catalog(ws):
        for token, _label in group["options"]:
            tokens.add(token)
    return tokens


def _facebook_suggest_target(question, ws, allow_new_fields=None):
    """Best-guess destination token for a form question (matched by key/label)."""
    from core.events import _norm_key

    norm = _norm_key(question.get("key") or question.get("label") or "")
    if norm in _FB_SUGGEST:
        return _FB_SUGGEST[norm]
    custom_fields = list(CustomField.objects.filter(workspace=ws)) if ws is not None else []
    for field in custom_fields:
        if norm and norm in {_norm_key(field.key), _norm_key(field.label)}:
            return f"custom:{field.object_type}:{field.key}"
    # No matching field: use existing CRM fields unless this workspace is empty.
    # Empty workspaces can still create the first field from here.
    if allow_new_fields is None:
        allow_new_fields = not custom_fields
    # If CRM custom fields already exist, this screen should not invent another
    # field. The user picks an existing field or discards that answer.
    return "new:deal" if allow_new_fields else "ignore"


def _facebook_body_key_for_token(token):
    """Resolve a mapping token to the body key the create actions read from."""
    if not token or token == "ignore":
        return None
    if token in _FB_STANDARD_BODY_KEYS:
        return _FB_STANDARD_BODY_KEYS[token]
    if token.startswith("custom:"):
        parts = token.split(":", 2)
        if len(parts) == 3 and parts[2]:
            return parts[2]  # the CRM custom field key
    return None


def _facebook_apply_form_map(conn, form_id, flat):
    """Turn a flat lead dict into the standard `body` using the form's saved mapping.

    Mapped questions go to the body key the create actions understand; 'ignore'
    drops them; questions with no mapping entry are kept raw so nothing is lost.
    Without a saved map, returns the flat dict unchanged (auto-map still applies).
    """
    from core.events import _norm_key

    if not isinstance(flat, dict):
        return {}
    fmap = ((conn.config or {}).get("form_maps") or {}).get(str(form_id or ""), {})
    if not fmap:
        return dict(flat)
    norm_flat = {}
    for key, value in flat.items():
        norm_flat.setdefault(_norm_key(key), value)
    body = {}
    for qkey, token in fmap.items():
        value = flat.get(qkey)
        if value in (None, "", []):
            value = norm_flat.get(_norm_key(qkey))
        if value in (None, "", []):
            continue
        body_key = _facebook_body_key_for_token(token)
        if body_key:
            body.setdefault(body_key, value)
    mapped_norms = {_norm_key(qkey) for qkey in fmap.keys()}
    for key, value in flat.items():
        if _norm_key(key) in mapped_norms or value in (None, "", []):
            continue
        body.setdefault(key, value)
    return body


@login_required
@require_role("admin")
@require_POST
def facebook_sync_forms(request):
    conn = _facebook_active_connection(request.workspace)
    if not conn:
        messages.error(request, "Conecte uma página do Facebook primeiro.")
        return redirect("integrations")
    forms = _facebook_list_forms(conn)
    conn.config = {**(conn.config or {}), "forms": forms}
    conn.save(update_fields=["config", "updated_at"])
    if forms:
        messages.success(request, f"{len(forms)} formulário(s) sincronizado(s).")
    else:
        messages.warning(request, "Nenhum formulário encontrado nessa página. Publique um formulário de leads no Facebook e sincronize de novo.")
    return redirect("facebook_forms")


@login_required
@require_role("admin")
def facebook_forms(request):
    ws = request.workspace
    conn = _facebook_active_connection(ws)
    if not conn:
        messages.error(request, "Conecte uma página do Facebook primeiro.")
        return redirect("integrations")
    cfg = conn.config or {}
    maps = cfg.get("form_maps") or {}
    flows = cfg.get("form_flows") or {}
    flow_autos = {a.pk: a for a in Automation.objects.filter(workspace=ws, pk__in=[v for v in flows.values() if v])}
    items = []
    for form in (cfg.get("forms") or []):
        fid = str(form.get("id"))
        auto = flow_autos.get(flows.get(fid))
        items.append({
            "id": fid,
            "name": form.get("name") or "Formulário",
            "status": form.get("status", ""),
            "mapped": bool(maps.get(fid)),
            "flow_active": bool(auto and auto.active),
        })
    return render(request, "core/facebook_forms.html", {
        "page_title": "Formulários do Facebook",
        "breadcrumb": ["Configurações", "Integrações", "Facebook"],
        "page_name": cfg.get("page_name", ""),
        "forms": items,
    })


def _facebook_resolve_new_fields(ws, raw, allow_new_fields=True):
    """Resolve 'new:<obj>' mapping tokens by creating a text custom field on the
    fly (idempotent). Returns the mapping with those tokens turned into real
    'custom:<obj>:<key>' targets, so a form question is never silently dropped."""
    resolved = {}
    for qkey, token in raw.items():
        if token.startswith("new:"):
            if not allow_new_fields:
                resolved[qkey] = "ignore"
                continue
            obj = token.split(":", 1)[1]
            if obj in {"contact", "company", "deal"}:
                fkey = slugify(qkey)[:60] or "campo"
                field, _ = CustomField.objects.get_or_create(
                    workspace=ws, object_type=obj, key=fkey,
                    defaults={"label": (qkey[:80] or fkey), "field_type": "text"},
                )
                token = f"custom:{obj}:{field.key}"
            else:
                token = "ignore"
        resolved[qkey] = token
    return resolved


def _facebook_destino_needs(mapping):
    """Which objects the mapping implies must exist so no answer is lost."""
    needs = {"contact": False, "company": False, "deal": False}
    for token in (mapping or {}).values():
        obj = ""
        if token.startswith("custom:"):
            parts = token.split(":", 2)
            obj = parts[1] if len(parts) == 3 else ""
        elif ":" in token:
            obj = token.split(":", 1)[0]
        if obj in needs:
            needs[obj] = True
    return needs


def _facebook_build_automation(ws, conn, form_id, form_name, destino):
    """Create or update the Automation for a Facebook form from a simple destino
    (which objects to create + pipeline/stage). Generates a valid canvas because
    ONLY the canvas execution path passes the event (so the lead data maps), and
    so the flow stays editable in the advanced canvas. Idempotent via
    conn.config['form_flows'][form_id]."""
    form_id = str(form_id)
    ordered = []
    if destino.get("create_company"):
        ordered.append({"type": "create_company"})
    if destino.get("create_contact"):
        ordered.append({"type": "create_contact"})
    if destino.get("create_deal"):
        deal = {"type": "create_deal"}
        if destino.get("pipeline"):
            deal["pipeline"] = str(destino["pipeline"])
        if destino.get("stage"):
            deal["deal_stage"] = str(destino["stage"])
        ordered.append(deal)

    nodes = [{
        "id": "entry", "type": "trigger", "x": 120, "y": 200,
        "data": {"trigger": "facebook_lead", "trigger_form_id": form_id},
    }]
    edges = []
    prev_id, x = "entry", 120
    for i, action in enumerate(ordered, start=1):
        x += 360
        node_id = f"action-{i}"
        data = {"action_index": i, "action_type": action["type"]}
        if action.get("pipeline"):
            data["pipeline"] = action["pipeline"]
        if action.get("deal_stage"):
            data["deal_stage"] = action["deal_stage"]
        nodes.append({"id": node_id, "type": "action", "x": x, "y": 200, "data": data})
        edges.append({"from": prev_id, "to": node_id})
        prev_id = node_id
    canvas = {
        "version": 1,
        "nodes": nodes,
        "edges": edges,
        "viewport": {"scroll_left": 0, "scroll_top": 0, "zoom": 1},
    }

    cfg = conn.config or {}
    flows = dict(cfg.get("form_flows") or {})
    existing_pk = flows.get(form_id)
    auto = Automation.objects.filter(pk=existing_pk, workspace=ws).first() if existing_pk else None
    fields = {
        "name": f"Facebook — {form_name}"[:120],
        "icon": "users",
        "trigger": "facebook_lead",
        "condition_stage": "",
        "action": ordered[0]["type"] if ordered else "create_contact",
        "action_text": "",
        "action_due_days": 0,
        "conditions": {},
        "actions": ordered,
        "canvas": canvas,
        "active": bool(ordered),
    }
    if auto:
        for key, value in fields.items():
            setattr(auto, key, value)
        auto.save()
    else:
        auto = Automation.objects.create(workspace=ws, **fields)
    flows[form_id] = auto.pk
    conn.config = {**cfg, "form_flows": flows}
    conn.save(update_fields=["config", "updated_at"])
    return auto


@login_required
@require_role("admin")
def facebook_form_map(request, form_id):
    ws = request.workspace
    conn = _facebook_active_connection(ws)
    if not conn:
        messages.error(request, "Conecte uma página do Facebook primeiro.")
        return redirect("integrations")
    form_id = str(form_id)
    cfg = conn.config or {}
    form_name = next(
        (form.get("name") for form in (cfg.get("forms") or []) if str(form.get("id")) == form_id),
        form_id,
    )
    has_custom_fields = _facebook_has_custom_fields(ws)
    allow_new_fields = not has_custom_fields
    if request.method == "POST":
        raw = {}
        for key in request.POST:
            match = re.match(r"map_(.+)$", key)
            if match:
                raw[match.group(1)] = request.POST.get(key, "ignore").strip()
        # Resolve legacy/new tokens before validating the saved map.
        raw = _facebook_resolve_new_fields(ws, raw, allow_new_fields=allow_new_fields)
        valid = _facebook_valid_targets(ws)  # includes any fields just created
        new_map = {qkey: (token if token in valid else "ignore") for qkey, token in raw.items()}
        # If a field was mapped to an object, force that object on so its answer
        # is not lost.
        needs = _facebook_destino_needs(new_map)
        destino = {
            "create_contact": bool(request.POST.get("create_contact")) or needs["contact"],
            "create_company": bool(request.POST.get("create_company")) or needs["company"],
            "create_deal": bool(request.POST.get("create_deal")) or needs["deal"],
            "pipeline": request.POST.get("pipeline", "").strip(),
            "stage": request.POST.get("deal_stage", "").strip(),
        }
        form_maps = dict(cfg.get("form_maps") or {})
        form_maps[form_id] = new_map
        form_dest = dict(cfg.get("form_dest") or {})
        form_dest[form_id] = destino
        conn.config = {**cfg, "form_maps": form_maps, "form_dest": form_dest}
        conn.save(update_fields=["config", "updated_at"])
        auto = _facebook_build_automation(ws, conn, form_id, form_name, destino)
        if auto.active:
            messages.success(request, f"Formulário “{form_name}” configurado — leads viram registros automaticamente.")
        else:
            messages.warning(request, "Mapeamento salvo, mas nenhum registro foi marcado para criar — o fluxo ficou pausado.")
        return redirect("facebook_forms")

    saved = (cfg.get("form_maps") or {}).get(form_id, {})
    questions = _facebook_form_questions(conn, form_id)
    valid_targets = _facebook_valid_targets(ws)
    rows = []
    for question in questions:
        current = saved.get(question["key"]) or _facebook_suggest_target(
            question, ws, allow_new_fields=allow_new_fields
        )
        if current.startswith("new:") and not allow_new_fields:
            current = "ignore"
        elif current not in valid_targets and not (allow_new_fields and current.startswith("new:")):
            current = "ignore"
        rows.append({
            "key": question["key"],
            "label": question["label"],
            "type": question.get("type", ""),
            "current": current,
        })
    # Destino state: last saved for this form, else sensible defaults.
    dest = (cfg.get("form_dest") or {}).get(form_id) or {"create_contact": True, "create_deal": True}
    pipelines = _automation_pipelines(ws)
    return render(request, "core/facebook_form_map.html", {
        "page_title": "Configurar formulário",
        "breadcrumb": ["Configurações", "Integrações", "Facebook", "Configurar"],
        "form_id": form_id,
        "form_name": form_name,
        "rows": rows,
        "catalog": _facebook_target_catalog(ws),
        "has_questions": bool(questions),
        "pipelines": pipelines,
        "dest": dest,
        "has_custom_fields": has_custom_fields,
        "allow_new_fields": allow_new_fields,
        "flow_pk": (cfg.get("form_flows") or {}).get(form_id),
    })


@csrf_exempt
def automation_webhook(request, key):
    # Facebook (and other providers) verify a webhook with a GET handshake: echo
    # back hub.challenge when hub.verify_token matches this webhook's key.
    if request.method == "GET" and request.GET.get("hub.mode") == "subscribe":
        if request.GET.get("hub.verify_token") == key:
            return HttpResponse(request.GET.get("hub.challenge", ""))
        return HttpResponse("verify token invalido", status=403)

    body = _request_body_payload(request)
    matched = 0
    autos = Automation.objects.filter(active=True, trigger="webhook_received").select_related("workspace")
    for auto in autos:
        trigger_data = _automation_trigger_data(auto)
        if trigger_data.get("webhook_key") != key:
            continue
        with tenant_context(auto.workspace):
            set_current_workspace(auto.workspace)
            try:
                # A Facebook Lead Ads payload is flattened to standard lead fields
                # so the same "create contact/company/deal" actions just work.
                fb = _normalize_facebook_leadgen(body, auto.workspace)
                event = Event.objects.create(
                    workspace=auto.workspace,
                    event_type="webhook_received",
                    object_repr=f"Webhook {key}",
                    payload={
                        "webhook_key": key,
                        "body": fb if fb else body,
                        "query": request.GET.dict(),
                        "method": request.method,
                        "source": "facebook" if fb else "webhook",
                    },
                )
                run_automation_for_event(auto, event, None)
                event.processed = True
                event.save(update_fields=["processed"])
            finally:
                clear_current_workspace()
        matched += 1
    return JsonResponse({"received": True, "matched": matched})


def _facebook_flatten_fields(field_data):
    """Turn Facebook's field_data ([{name, values:[...]}]) into a flat dict, and
    map FB field names to the ones our create actions understand."""
    out = {}
    for field in field_data or []:
        if not isinstance(field, dict):
            continue
        name = str(field.get("name", "")).strip().lower()
        values = field.get("values") or []
        if name and values:
            out[name] = values[0] if len(values) == 1 else ", ".join(str(v) for v in values)
    for fb_name, std_name in (("full_name", "name"), ("phone_number", "phone"), ("company_name", "company")):
        if fb_name in out and std_name not in out:
            out[std_name] = out[fb_name]
    return out


def _normalize_facebook_leadgen(body, workspace):
    """If `body` is a Facebook Lead Ads payload, return a flat lead dict; else None.
    Handles inline field_data (test tool / connectors) and the native leadgen
    webhook (entry[].changes[].value), fetching the lead via Graph API when only a
    leadgen_id is present and a page token is configured."""
    if not isinstance(body, dict):
        return None
    if isinstance(body.get("field_data"), list):
        return _facebook_flatten_fields(body["field_data"]) or None
    entries = body.get("entry")
    if not isinstance(entries, list):
        return None
    for entry in entries:
        for change in (entry.get("changes") or []) if isinstance(entry, dict) else []:
            value = change.get("value") or {} if isinstance(change, dict) else {}
            if isinstance(value.get("field_data"), list):
                return _facebook_flatten_fields(value["field_data"]) or None
            leadgen_id = value.get("leadgen_id")
            if leadgen_id:
                fetched = _facebook_fetch_lead(leadgen_id, workspace)
                if fetched:
                    return fetched
    return None


def _facebook_fetch_lead(leadgen_id, workspace):
    """Fetch a lead's fields from the Graph API using the workspace's stored
    Facebook page token. Returns a flat dict, or None on any failure."""
    conn = IntegrationConnection.objects.filter(workspace=workspace, provider="facebook").first()
    token = (conn.config or {}).get("page_access_token", "") if conn else ""
    if not token or not leadgen_id:
        return None
    import urllib.parse
    import urllib.request

    url = "https://graph.facebook.com/v19.0/{}?{}".format(
        urllib.parse.quote(str(leadgen_id)),
        urllib.parse.urlencode({"access_token": token}),
    )
    try:
        with urllib.request.urlopen(url, timeout=8) as resp:
            payload = json.loads(resp.read().decode("utf-8"))
        return _facebook_flatten_fields(payload.get("field_data")) or None
    except Exception:
        return None


@csrf_exempt
def facebook_leadgen(request):
    """Meta app webhook for Facebook Lead Ads. Handles the verify handshake, then for
    each incoming lead finds the workspace that connected that page, fetches the
    lead's fields via the Graph API and creates the contact/company/deal."""
    if request.method == "GET":
        if settings.FACEBOOK_VERIFY_TOKEN and request.GET.get("hub.verify_token") == settings.FACEBOOK_VERIFY_TOKEN:
            return HttpResponse(request.GET.get("hub.challenge", ""))
        return HttpResponse("verify token invalido", status=403)

    body = _request_body_payload(request)
    entries = body.get("entry", []) if isinstance(body, dict) else []
    processed = 0
    for entry in entries:
        if not isinstance(entry, dict):
            continue
        page_id_root = str(entry.get("id", ""))
        for change in entry.get("changes", []) or []:
            if not isinstance(change, dict) or change.get("field") != "leadgen":
                continue
            value = change.get("value") or {}
            page_id = str(value.get("page_id") or page_id_root)
            leadgen_id = value.get("leadgen_id")
            form_id = str(value.get("form_id") or "")
            if not leadgen_id or not page_id:
                continue
            conn = (
                IntegrationConnection.objects
                .filter(provider="facebook", config__page_id=page_id)
                .select_related("workspace").first()
            )
            if not conn:
                continue
            ws = conn.workspace
            form_name = next(
                (f.get("name") for f in (conn.config or {}).get("forms", []) if str(f.get("id")) == form_id),
                "",
            )
            with tenant_context(ws):
                set_current_workspace(ws)
                try:
                    data = _facebook_fetch_lead(leadgen_id, ws)
                    if data:
                        # Apply the form's field mapping (if the gestor configured one)
                        # so answers land on the exact CRM fields they chose.
                        data = _facebook_apply_form_map(conn, form_id, data)
                        event = Event.objects.create(
                            workspace=ws,
                            event_type="facebook_lead",
                            object_repr=f"Lead do Facebook — {form_name}" if form_name else "Lead do Facebook",
                            payload={"body": data, "source": "facebook", "page_id": page_id,
                                     "form_id": form_id, "form_name": form_name},
                        )
                        # One trigger per form: run only automations whose trigger form
                        # matches this lead's form (empty = any form).
                        for auto in Automation.objects.filter(workspace=ws, active=True, trigger="facebook_lead"):
                            trigger_form = str(_automation_trigger_data(auto).get("trigger_form_id") or "")
                            if trigger_form and trigger_form != form_id:
                                continue
                            run_automation_for_event(auto, event, None)
                        event.processed = True
                        event.save(update_fields=["processed"])
                        processed += 1
                finally:
                    clear_current_workspace()
    return JsonResponse({"received": True, "processed": processed})


def _request_body_payload(request):
    raw = request.body.decode("utf-8", errors="ignore") if request.body else ""
    if not raw:
        return {}
    try:
        return json.loads(raw)
    except json.JSONDecodeError:
        return {"raw": raw}


def _automation_trigger_data(auto):
    for node in (auto.canvas or {}).get("nodes", []):
        if node.get("type") == "trigger":
            return node.get("data") or {}
    return {}


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
