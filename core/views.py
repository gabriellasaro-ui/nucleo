import re

from django.contrib import messages
from django.contrib.auth import get_user_model
from django.contrib.auth.decorators import login_required
from django.db.models import Q, Sum
from django.shortcuts import get_object_or_404, redirect, render
from django.utils.text import slugify

from modules.crm.models import Company, Contact, Deal

from .forms import WorkspaceForm
from .models import EVENT_CHOICES, Automation, CustomField, Membership
from .rbac import require_role


# Categorical palette for the contacts donut (validated: dataviz validator,
# light mode — CVD ΔE 39.3). Fixed order; never cycled.
_CONTACT_COLORS = {"lead": "#2563eb", "qualificado": "#d97706", "cliente": "#059669", "inativo": "#7c3aed"}
_FUNNEL_STAGES = ["novo", "qualificado", "proposta", "negociacao", "ganho"]


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
        workspace = form.save()
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
        ident = request.POST.get("identifier", "").strip()
        role = request.POST.get("role", Membership.ROLE_MEMBER)
        if role not in dict(Membership.ROLE_CHOICES):
            role = Membership.ROLE_MEMBER
        User = get_user_model()
        user = User.objects.filter(Q(username__iexact=ident) | Q(email__iexact=ident)).first()
        if not ident:
            messages.error(request, "Informe um usuário ou e-mail.")
        elif not user:
            messages.error(request, f"Usuário “{ident}” não encontrado.")
        elif Membership.objects.filter(user=user, workspace=request.workspace).exists():
            messages.warning(request, f"{user.get_username()} já é membro deste workspace.")
        else:
            Membership.objects.create(user=user, workspace=request.workspace, role=role)
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
    from modules.crm.models import Deal
    ws = request.workspace
    return render(request, "core/automations.html", {
        "page_title": "Automações",
        "breadcrumb": ["Configurações", "Automações"],
        "automations": ws.automations.all(),
        "recent_events": ws.events.all()[:10],
        "triggers": EVENT_CHOICES,
        "actions": Automation.ACTION_CHOICES,
        "stages": Deal.STAGE_CHOICES,
    })


@login_required
@require_role("admin")
def automation_add(request):
    if request.method == "POST":
        ws = request.workspace
        name = request.POST.get("name", "").strip()
        trigger = request.POST.get("trigger")
        action = request.POST.get("action", "create_task")
        action_text = request.POST.get("action_text", "").strip()
        condition_stage = request.POST.get("condition_stage", "") if trigger == "deal_stage_changed" else ""
        try:
            due = max(0, int(request.POST.get("action_due_days") or 2))
        except ValueError:
            due = 2
        if not name or not action_text:
            messages.error(request, "Informe o nome e o texto da ação.")
        elif trigger not in dict(EVENT_CHOICES) or action not in dict(Automation.ACTION_CHOICES):
            messages.error(request, "Gatilho ou ação inválido.")
        else:
            Automation.objects.create(
                workspace=ws, name=name, trigger=trigger, action=action,
                action_text=action_text, condition_stage=condition_stage,
                action_due_days=due, active=True,
            )
            messages.success(request, f"Automação “{name}” criada.")
    return redirect("automations")


@login_required
@require_role("admin")
def automation_toggle(request, pk):
    if request.method == "POST":
        auto = get_object_or_404(Automation, pk=pk, workspace=request.workspace)
        auto.active = not auto.active
        auto.save(update_fields=["active"])
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
