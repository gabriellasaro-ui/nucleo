import json
import re

from django.contrib import messages
from django.contrib.auth import get_user_model
from django.contrib.auth.decorators import login_required
from django.db.models import Count, Max, Q, Sum
from django.http import HttpResponse, HttpResponseForbidden, JsonResponse
from django.shortcuts import get_object_or_404, render
from django.urls import reverse
from django.utils import timezone
from django.utils.text import slugify

from core import audit
from core.customfields import get_fields, read_from_post, with_values
from core.events import emit
from core.models import CustomField, IntegrationConnection
from core.privacy import BASIS_CHOICES, CONSENT_STATUS_CHOICES
from core.rbac import can_edit
from core.whatsapp_inbox import apply_whatsapp_directory_names

from django.shortcuts import redirect

from .forms import ActivityForm, CompanyForm, ContactForm, DealForm
from .models import (
    Activity, Attachment, Company, Contact, Deal, Pipeline, Stage, Tag,
    WhatsAppConversation,
)


CARD_FIELD_CHOICES = [
    ("company", "Empresa"),
    ("value", "Valor"),
    ("expected_close", "Previsão"),
    ("contact", "Contato"),
    ("owner", "Responsável"),
]


def _default_pipeline(request):
    pipe = (
        Pipeline.objects.filter(workspace=request.workspace, is_default=True).first()
        or Pipeline.objects.filter(workspace=request.workspace).first()
    )
    if pipe is None:
        pipe = Pipeline.objects.create(workspace=request.workspace, name="Vendas", is_default=True)
    pipe.ensure_stages()
    return pipe

def _trigger_header(events):
    return json.dumps(events)


def _refresh_response(message="Alterações salvas", kind="success"):
    resp = HttpResponse(status=204)
    events = {"nucleo:closeModal": True, "nucleo:dataChanged": True}
    if message:
        events["nucleo:toast"] = {"text": message, "kind": kind}
    resp["HX-Trigger"] = _trigger_header(events)
    return resp


def _with_toast(response, message, kind="success"):
    response["HX-Trigger"] = _trigger_header({"nucleo:toast": {"text": message, "kind": kind}})
    return response


def _forbidden():
    return HttpResponse("Você não tem permissão para editar.", status=403)


def _apply_custom(request, obj, object_type):
    """Merge posted custom-field values into the record's `custom` JSON."""
    fields = get_fields(request.workspace, object_type)
    obj.custom = {**(obj.custom or {}), **read_from_post(request.POST, fields)}


def _scope_owner_field(form, request):
    """Limit the 'Responsável' select to members of the active workspace."""
    if "owner" in form.fields:
        User = get_user_model()
        form.fields["owner"].queryset = User.objects.filter(
            memberships__workspace=request.workspace
        ).distinct()


def _apply_tags(request, obj):
    """Parse comma-separated tags, get-or-create them in the workspace, attach."""
    if "tags_text" not in request.POST:
        return
    names = [t.strip() for t in request.POST.get("tags_text", "").split(",") if t.strip()]
    tags = []
    for name in list(dict.fromkeys(names))[:20]:
        tag, _ = Tag.all_objects.get_or_create(workspace=request.workspace, name=name)
        tags.append(tag)
    obj.tags.set(tags)

def _scope_parent_field(form, request, instance):
    """The 'Empresa matriz' select shows other companies in the workspace."""
    if "parent" in form.fields:
        qs = Company.objects.all()
        if instance is not None:
            qs = qs.exclude(pk=instance.pk)
        form.fields["parent"].queryset = qs


_MENTION_RE = re.compile(r"@([\w.\-]+)")


def _workspace_members(request):
    User = get_user_model()
    return User.objects.filter(memberships__workspace=request.workspace).distinct()


def _apply_assignees(request, obj):
    """Set the co-responsáveis (M2M) from the posted user ids."""
    ids = request.POST.getlist("assignees")
    obj.assignees.set(_workspace_members(request).filter(pk__in=ids))


def _assignee_ids(obj):
    return list(obj.assignees.values_list("pk", flat=True)) if (obj and obj.pk) else []


def _apply_mentions(request, activity):
    """Link @username tokens in the body to workspace members."""
    usernames = set(_MENTION_RE.findall(activity.body or ""))
    if usernames:
        activity.mentions.set(_workspace_members(request).filter(username__in=usernames))


@login_required
def attachment_upload(request):
    if request.method != "POST":
        return HttpResponse(status=405)
    if not can_edit(request):
        return _forbidden()
    obj, owner_key = _resolve_owner(request, request.POST.get("on"), request.POST.get("id"))
    upload = request.FILES.get("file")
    if obj is None:
        return HttpResponse(status=400)
    if upload:
        attachment = Attachment(workspace=request.workspace, file=upload, uploaded_by=request.user)
        setattr(attachment, owner_key, obj)
        attachment.save()
        messages.success(request, "Arquivo enviado.")
    return redirect(obj.get_absolute_url())


@login_required
def attachment_delete(request, pk):
    if request.method != "POST":
        return HttpResponse(status=405)
    if not can_edit(request):
        return _forbidden()
    attachment = get_object_or_404(Attachment, pk=pk, workspace=request.workspace)
    target = attachment.company or attachment.contact or attachment.deal
    attachment.file.delete(save=False)
    attachment.delete()
    messages.success(request, "Arquivo removido.")
    return redirect(target.get_absolute_url() if target else "dashboard")


# --------------------------------------------------------------------------- #
# Companies
# --------------------------------------------------------------------------- #
@login_required
def company_list(request):
    context = {
        "page_title": "Empresas",
        "breadcrumb": ["CRM", "Empresas"],
        "companies": _filter_companies(request),
    }
    return render(request, "crm/company_list.html", context)


@login_required
def company_rows(request):
    return render(request, "crm/partials/company_rows.html", {"companies": _filter_companies(request)})


def _filter_companies(request):
    qs = Company.objects.filter(workspace=request.workspace)
    q = request.GET.get("q", "").strip()
    if q:
        qs = qs.filter(Q(name__icontains=q) | Q(domain__icontains=q) | Q(industry__icontains=q))
    return qs


@login_required
def company_form(request, pk=None):
    instance = get_object_or_404(Company, pk=pk, workspace=request.workspace) if pk else None
    if request.method == "POST":
        if not can_edit(request):
            return _forbidden()
        is_new = instance is None
        form = CompanyForm(request.POST, instance=instance)
        _scope_owner_field(form, request)
        _scope_parent_field(form, request, instance)
        if form.is_valid():
            company = form.save(commit=False)
            company.workspace = request.workspace
            _apply_custom(request, company, "company")
            company.save()
            _apply_tags(request, company)
            _apply_assignees(request, company)
            if is_new:
                emit(request.workspace, "company_created", company)
            return _refresh_response("Empresa criada." if is_new else "Empresa atualizada.")
    else:
        form = CompanyForm(instance=instance)
        _scope_owner_field(form, request)
        _scope_parent_field(form, request, instance)
    context = {
        "form": form,
        "title": "Editar empresa" if instance else "Nova empresa",
        "action": request.path,
        "delete_pk": instance.pk if instance else None,
        "custom_fields": with_values(get_fields(request.workspace, "company"), instance),
        "members": _workspace_members(request),
        "assignee_ids": _assignee_ids(instance),
    }
    return render(request, "crm/partials/company_form.html", context)


@login_required
def company_delete(request, pk):
    if request.method == "POST":
        if not can_edit(request):
            return _forbidden()
        company = get_object_or_404(Company, pk=pk, workspace=request.workspace)
        company.delete()
        return _refresh_response("Empresa excluída.")
    return HttpResponse(status=405)


# --------------------------------------------------------------------------- #
# Contacts
# --------------------------------------------------------------------------- #
@login_required
def contact_list(request):
    context = {
        "page_title": "Contatos",
        "breadcrumb": ["CRM", "Contatos"],
        "contacts": _filter_contacts(request),
        "stages": Contact.STAGE_CHOICES,
    }
    return render(request, "crm/contact_list.html", context)


@login_required
def contact_rows(request):
    return render(request, "crm/partials/contact_rows.html", {"contacts": _filter_contacts(request)})


def _filter_contacts(request):
    qs = Contact.objects.filter(workspace=request.workspace).select_related("company")
    q = request.GET.get("q", "").strip()
    if q:
        qs = qs.filter(
            Q(first_name__icontains=q)
            | Q(last_name__icontains=q)
            | Q(email__icontains=q)
            | Q(company__name__icontains=q)
        )
    return qs


@login_required
def contact_form(request, pk=None):
    instance = get_object_or_404(Contact, pk=pk, workspace=request.workspace) if pk else None
    old_stage = instance.stage if instance else None
    if request.method == "POST":
        if not can_edit(request):
            return _forbidden()
        is_new = instance is None
        form = ContactForm(request.POST, instance=instance)
        _scope_company_field(form, request)
        _scope_owner_field(form, request)
        if form.is_valid():
            contact = form.save(commit=False)
            contact.workspace = request.workspace
            company, company_created = _company_from_contact_post(request, contact.owner)
            if company:
                contact.company = company
            _apply_custom(request, contact, "contact")
            contact.save()
            _apply_tags(request, contact)
            _apply_assignees(request, contact)
            if company_created:
                emit(request.workspace, "company_created", company)
            if is_new:
                emit(request.workspace, "contact_created", contact, {"stage": contact.stage})
            elif contact.stage != old_stage:
                emit(
                    request.workspace,
                    "contact_stage_changed",
                    contact,
                    {"stage": contact.stage, "old_stage": old_stage},
                )
            return _refresh_response("Contato criado." if is_new else "Contato atualizado.")
    else:
        initial = {}
        company_id = request.GET.get("company")
        if not instance and company_id and Company.objects.filter(workspace=request.workspace, pk=company_id).exists():
            initial["company"] = company_id
        form = ContactForm(instance=instance, initial=initial)
        _scope_company_field(form, request)
        _scope_owner_field(form, request)
    context = {
        "form": form,
        "title": "Editar contato" if instance else "Novo contato",
        "action": request.path,
        "delete_pk": instance.pk if instance else None,
        "custom_fields": with_values(get_fields(request.workspace, "contact"), instance),
        "members": _workspace_members(request),
        "assignee_ids": _assignee_ids(instance),
        "new_company_name": request.POST.get("new_company_name", "") if request.method == "POST" else "",
        "new_company_domain": request.POST.get("new_company_domain", "") if request.method == "POST" else "",
        "new_company_industry": request.POST.get("new_company_industry", "") if request.method == "POST" else "",
        "new_company_city": request.POST.get("new_company_city", "") if request.method == "POST" else "",
    }
    return render(request, "crm/partials/contact_form.html", context)


@login_required
def contact_delete(request, pk):
    if request.method == "POST":
        if not can_edit(request):
            return _forbidden()
        contact = get_object_or_404(Contact, pk=pk, workspace=request.workspace)
        contact.delete()
        return _refresh_response("Contato excluído.")
    return HttpResponse(status=405)


def _scope_company_field(form, request):
    if "company" in form.fields:
        form.fields["company"].queryset = Company.objects.filter(workspace=request.workspace)


def _scope_deal_stage_field(form, pipeline):
    if "stage" not in form.fields or pipeline is None:
        return
    choices = [(stage.key, stage.name) for stage in pipeline.stages.all()]
    form.fields["stage"].choices = choices
    form.fields["stage"].widget.attrs["data-deal-stage-select"] = "1"
    if choices and not form.initial.get("stage"):
        form.initial["stage"] = choices[0][0]


def _selected_deal_stage_key(request, form, pipeline, instance=None):
    if request.method == "POST" and request.POST.get("stage"):
        return request.POST.get("stage")
    if instance and instance.stage:
        return instance.stage
    value = form.initial.get("stage") if form else ""
    if value:
        return value
    first = pipeline.stages.first() if pipeline else None
    return first.key if first else ""


def _company_from_contact_post(request, owner=None):
    if request.POST.get("create_company_inline") != "on":
        return None, False
    name = request.POST.get("new_company_name", "").strip()
    if not name:
        return None, False

    domain = request.POST.get("new_company_domain", "").strip()
    industry = request.POST.get("new_company_industry", "").strip()
    city = request.POST.get("new_company_city", "").strip()
    company = Company.objects.filter(workspace=request.workspace, name__iexact=name).first()
    created = company is None
    if created:
        company = Company.objects.create(
            workspace=request.workspace,
            name=name,
            domain=domain,
            industry=industry,
            city=city,
            owner=owner,
        )
    else:
        updates = []
        for field_name, value in {"domain": domain, "industry": industry, "city": city}.items():
            if value and not getattr(company, field_name):
                setattr(company, field_name, value)
                updates.append(field_name)
        if owner and not company.owner_id:
            company.owner = owner
            updates.append("owner")
        if updates:
            company.save(update_fields=updates + ["updated_at"])
    return company, created


# --------------------------------------------------------------------------- #
# Deals (Kanban pipeline)
# --------------------------------------------------------------------------- #
def _deals_refresh_response(message="Negócio salvo.", kind="success"):
    resp = HttpResponse(status=204)
    events = {
        "nucleo:closeModal": True,
        "nucleo:dataChanged": True,
        "nucleo:dealsBoard": True,
        "nucleo:dealsStats": True,
        "nucleo:toast": {"text": message, "kind": kind},
    }
    resp["HX-Trigger"] = _trigger_header(events)
    return resp


def _current_pipeline(request):
    pid = request.GET.get("pipeline")
    if pid:
        pipe = Pipeline.objects.filter(pk=pid, workspace=request.workspace).first()
        if pipe:
            return pipe
    return _default_pipeline(request)


def _unique_stage_key(pipeline, name):
    base = slugify(name).replace("-", "_")[:20] or "etapa"
    existing = set(pipeline.stages.values_list("key", flat=True))
    key, i = base, 2
    while key in existing:
        key = f"{base}_{i}"
        i += 1
    return key


def _deal_stats(request, pipeline):
    agg = Deal.objects.filter(workspace=request.workspace, pipeline=pipeline).aggregate(
        open_value=Sum("value", filter=Q(stage_kind="open")),
        open_count=Count("id", filter=Q(stage_kind="open")),
        won_value=Sum("value", filter=Q(stage_kind="won")),
    )
    return {
        "open_value": agg["open_value"] or 0,
        "open_count": agg["open_count"] or 0,
        "won_value": agg["won_value"] or 0,
    }


def _card_field_options(workspace):
    options = [{"key": key, "label": label, "kind": "native"} for key, label in CARD_FIELD_CHOICES]
    options.extend(
        {"key": f"custom:{field.key}", "label": field.label, "kind": "custom", "field": field}
        for field in CustomField.objects.filter(workspace=workspace, object_type="deal")
    )
    return options


def _safe_card_fields(workspace, values):
    allowed = {option["key"] for option in _card_field_options(workspace)}
    return [value for value in values if value in allowed]


def _stage_custom_field_keys(stage):
    return [
        value.split(":", 1)[1]
        for value in (stage.card_fields or [])
        if isinstance(value, str) and value.startswith("custom:")
    ]


def _stage_custom_fields(workspace, pipeline, stage_key):
    stage = pipeline.stages.filter(key=stage_key).first() if pipeline else None
    if stage is None:
        return []
    keys = _stage_custom_field_keys(stage)
    if not keys:
        return []
    fields = list(CustomField.objects.filter(workspace=workspace, object_type="deal", key__in=keys))
    by_key = {field.key: field for field in fields}
    return [by_key[key] for key in keys if key in by_key]


def _stage_custom_field_groups(workspace, pipeline, instance=None):
    groups = []
    for stage in pipeline.stages.all():
        fields = _stage_custom_fields(workspace, pipeline, stage.key)
        groups.append({
            "stage": stage,
            "fields": with_values(fields, instance),
        })
    return groups


def _merge_card_fields(primary, secondary):
    merged = []
    for value in list(primary) + list(secondary):
        if value and value not in merged:
            merged.append(value)
    return merged


def _unique_deal_field_key(workspace, label):
    base = slugify(label).replace("-", "_")[:60] or "campo"
    existing = set(
        CustomField.objects.filter(workspace=workspace, object_type="deal").values_list("key", flat=True)
    )
    key, i = base, 2
    while key in existing:
        suffix = f"_{i}"
        key = f"{base[:60 - len(suffix)]}{suffix}"
        i += 1
    return key


def _custom_field_options(raw_options):
    return [option.strip() for option in raw_options.split(",") if option.strip()]


def _remove_card_field_from_pipeline(pipeline, card_field_key):
    for stage in pipeline.stages.all():
        card_fields = [value for value in (stage.card_fields or []) if value != card_field_key]
        if card_fields != (stage.card_fields or []):
            stage.card_fields = card_fields
            stage.save(update_fields=["card_fields", "updated_at"])


def _propagate_stage_card_fields(stage, selected_fields):
    found = False
    for item in stage.pipeline.stages.all():
        if item.pk == stage.pk:
            item.card_fields = selected_fields
            found = True
        elif found:
            item.card_fields = _merge_card_fields(selected_fields, item.card_fields or [])
        else:
            continue
        item.save(update_fields=["card_fields", "updated_at"])


def _format_custom_card_value(field, value):
    if value in (None, ""):
        return ""
    if field.field_type == "checkbox":
        return "Sim" if value else "Não"
    return str(value)


def _deal_card_rows(deal, card_fields, custom_fields):
    rows = []
    custom_by_key = {field.key: field for field in custom_fields}
    custom_values = deal.custom or {}
    for key in card_fields:
        if key == "company" and deal.company_id:
            rows.append({"label": "Empresa", "value": deal.company.name})
        elif key == "value":
            value = f"R$ {deal.value:,.0f}".replace(",", ".")
            rows.append({"label": "Valor", "value": value, "emphasis": True})
        elif key == "expected_close" and deal.expected_close:
            rows.append({"label": "Previsão", "value": deal.expected_close.strftime("%d/%m/%Y")})
        elif key == "contact" and deal.contact_id:
            rows.append({"label": "Contato", "value": deal.contact.full_name})
        elif key == "owner" and deal.owner_id:
            rows.append({"label": "Responsável", "value": deal.owner.get_username()})
        elif key.startswith("custom:"):
            field_key = key.split(":", 1)[1]
            field = custom_by_key.get(field_key)
            if field:
                value = _format_custom_card_value(field, custom_values.get(field.key))
                if value:
                    rows.append({"label": field.label, "value": value})
    return rows


def _board_context(request):
    pipeline = _current_pipeline(request)
    stages = list(pipeline.stages.all())
    custom_fields = list(CustomField.objects.filter(workspace=request.workspace, object_type="deal"))
    deals = list(
        Deal.objects.filter(workspace=request.workspace, pipeline=pipeline).select_related("company", "contact", "owner")
    )
    columns = []
    for st in stages:
        col_deals = [d for d in deals if d.stage == st.key]
        card_fields = st.card_fields or []
        for deal in col_deals:
            deal.card_rows = _deal_card_rows(deal, card_fields, custom_fields)
        columns.append({
            "key": st.key, "label": st.name, "color": st.color, "kind": st.kind, "stage_id": st.pk,
            "card_fields": card_fields,
            "deals": col_deals,
            "total": sum((d.value for d in col_deals), 0),
            "count": len(col_deals),
        })
    return {
        "columns": columns,
        "stats": _deal_stats(request, pipeline),
        "pipeline": pipeline,
        "pipelines": list(Pipeline.objects.filter(workspace=request.workspace)),
    }


@login_required
def deal_board(request):
    context = {"page_title": "Negócios", "breadcrumb": ["CRM", "Negócios"], **_board_context(request)}
    return render(request, "crm/deal_board.html", context)


@login_required
def deal_board_cards(request):
    return render(request, "crm/partials/deal_columns.html", _board_context(request))


@login_required
def deal_stats(request):
    return render(request, "crm/partials/deal_stats.html", {"stats": _deal_stats(request, _current_pipeline(request))})


@login_required
def deal_form(request, pk=None):
    instance = get_object_or_404(Deal, pk=pk, workspace=request.workspace) if pk else None
    old_stage = instance.stage if instance else None
    pipeline = instance.pipeline if instance and instance.pipeline_id else _current_pipeline(request)
    posted_pipeline = request.POST.get("pipeline") if request.method == "POST" else None
    if posted_pipeline:
        pipeline = get_object_or_404(Pipeline, pk=posted_pipeline, workspace=request.workspace)
    if request.method == "POST":
        if not can_edit(request):
            return _forbidden()
        is_new = instance is None
        form = DealForm(request.POST, instance=instance)
        _scope_deal_fields(form, request)
        _scope_owner_field(form, request)
        _scope_deal_stage_field(form, pipeline)
        if form.is_valid():
            deal = form.save(commit=False)
            deal.workspace = request.workspace
            deal.pipeline = pipeline
            deal.sync_stage_kind()
            _apply_custom(request, deal, "deal")
            deal.save()
            _apply_tags(request, deal)
            _apply_assignees(request, deal)
            if is_new:
                emit(request.workspace, "deal_created", deal, {"stage": deal.stage})
            elif deal.stage != old_stage:
                emit(request.workspace, "deal_stage_changed", deal,
                     {"stage": deal.stage, "old_stage": old_stage})
            return _deals_refresh_response("Negócio criado." if is_new else "Negócio atualizado.")
    else:
        form = DealForm(instance=instance)
        _scope_deal_fields(form, request)
        _scope_owner_field(form, request)
        _scope_deal_stage_field(form, pipeline)
    context = {
        "form": form,
        "title": "Editar negócio" if instance else "Novo negócio",
        "action": request.path,
        "pipeline": pipeline,
        "delete_pk": instance.pk if instance else None,
        "selected_stage_key": _selected_deal_stage_key(request, form, pipeline, instance),
        "stage_custom_field_groups": _stage_custom_field_groups(request.workspace, pipeline, instance),
        "members": _workspace_members(request),
        "assignee_ids": _assignee_ids(instance),
    }
    return render(request, "crm/partials/deal_form.html", context)


def _deal_workspace_pipelines(workspace):
    pipelines = list(Pipeline.objects.filter(workspace=workspace).order_by("order", "id"))
    for pipeline in pipelines:
        pipeline.ensure_stages()
    return pipelines


def _deal_workspace_context(request, deal, form, pipeline, pipelines):
    selected_pipeline_id = str(
        request.POST.get("pipeline") or deal.pipeline_id or pipeline.pk
    )
    selected_stage_key = str(request.POST.get("stage") or deal.stage or "")
    stage_groups = []
    for item in pipelines:
        for stage in item.stages.all():
            stage_groups.append({
                "pipeline": item,
                "stage": stage,
                "fields": with_values(
                    _stage_custom_fields(request.workspace, item, stage.key),
                    deal,
                ),
            })
    conversation = None
    thread_messages = []
    if deal.contact_id:
        conversation = (
            WhatsAppConversation.objects.filter(
                workspace=request.workspace,
                contact=deal.contact,
                is_history_import=False,
            )
            .exclude(remote_jid__endswith="@lid")
            .order_by("-last_message_at", "-updated_at")
            .first()
        )
        if conversation:
            config = (
                IntegrationConnection.objects.filter(
                    workspace=request.workspace,
                    provider="whatsapp",
                ).values_list("config", flat=True).first()
                or {}
            )
            apply_whatsapp_directory_names([conversation], config)
            thread_messages = list(conversation.messages.order_by("-sent_at", "-id")[:150])
            thread_messages.reverse()
    return {
        "deal": deal,
        "form": form,
        "pipeline": pipeline,
        "pipelines": pipelines,
        "selected_pipeline_id": selected_pipeline_id,
        "selected_stage_key": selected_stage_key,
        "stage_custom_field_groups": stage_groups,
        "conversation": conversation,
        "thread_messages": thread_messages,
    }


@login_required
def deal_workspace(request, pk):
    deal = get_object_or_404(
        Deal.objects.select_related("company", "contact", "owner", "pipeline"),
        pk=pk,
        workspace=request.workspace,
    )
    pipelines = _deal_workspace_pipelines(request.workspace)
    if not pipelines:
        pipelines = [_default_pipeline(request)]
    pipeline = deal.pipeline or pipelines[0]
    posted_pipeline = request.POST.get("pipeline") if request.method == "POST" else None
    if posted_pipeline:
        pipeline = get_object_or_404(
            Pipeline,
            pk=posted_pipeline,
            workspace=request.workspace,
        )
    old_stage = deal.stage
    old_pipeline_id = deal.pipeline_id

    if request.method == "POST":
        if not can_edit(request):
            return _forbidden()
        form = DealForm(request.POST, instance=deal)
        _scope_deal_fields(form, request)
        _scope_owner_field(form, request)
        _scope_deal_stage_field(form, pipeline)
        if form.is_valid():
            deal = form.save(commit=False)
            deal.workspace = request.workspace
            deal.pipeline = pipeline
            deal.sync_stage_kind()
            fields = _stage_custom_fields(request.workspace, pipeline, deal.stage)
            deal.custom = {
                **(deal.custom or {}),
                **read_from_post(request.POST, fields),
            }
            deal.save()
            _apply_tags(request, deal)
            if deal.stage != old_stage or deal.pipeline_id != old_pipeline_id:
                emit(
                    request.workspace,
                    "deal_stage_changed",
                    deal,
                    {"stage": deal.stage, "old_stage": old_stage},
                )
            form = DealForm(instance=deal)
            _scope_deal_fields(form, request)
            _scope_owner_field(form, request)
            _scope_deal_stage_field(form, pipeline)
            response = render(
                request,
                "crm/partials/deal_workspace.html",
                _deal_workspace_context(request, deal, form, pipeline, pipelines),
            )
            response["HX-Trigger"] = _trigger_header({
                "nucleo:dealsBoard": True,
                "nucleo:dealsStats": True,
                "nucleo:toast": {"text": "Negócio atualizado.", "kind": "success"},
            })
            return response
    else:
        form = DealForm(instance=deal)
        _scope_deal_fields(form, request)
        _scope_owner_field(form, request)
        _scope_deal_stage_field(form, pipeline)

    return render(
        request,
        "crm/partials/deal_workspace.html",
        _deal_workspace_context(request, deal, form, pipeline, pipelines),
    )


@login_required
def deal_delete(request, pk):
    if request.method == "POST":
        if not can_edit(request):
            return _forbidden()
        deal = get_object_or_404(Deal, pk=pk, workspace=request.workspace)
        deal.delete()
        return _deals_refresh_response("Negócio excluído.")
    return HttpResponse(status=405)


@login_required
def deal_move(request, pk):
    if request.method != "POST":
        return HttpResponse(status=405)
    if not can_edit(request):
        return _forbidden()
    deal = get_object_or_404(Deal, pk=pk, workspace=request.workspace)
    stage = request.POST.get("stage")
    old_stage = deal.stage
    moved = False
    valid_keys = (
        set(deal.pipeline.stages.values_list("key", flat=True))
        if deal.pipeline_id else set(dict(Deal.STAGE_CHOICES))
    )
    if stage in valid_keys and stage != old_stage:
        deal.stage = stage
        deal.sync_stage_kind()
        deal.save(update_fields=["stage", "stage_kind", "updated_at"])
        emit(request.workspace, "deal_stage_changed", deal, {"stage": stage, "old_stage": old_stage})
        moved = True
    ids = [i for i in request.POST.get("order", "").split(",") if i]
    for pos, deal_id in enumerate(ids):
        Deal.objects.filter(pk=deal_id, workspace=request.workspace).update(order=pos)
    resp = HttpResponse(status=204)
    events = {"nucleo:dealsStats": True}
    if moved:
        events["nucleo:toast"] = {"text": f"Negócio movido para {deal.stage_display}.", "kind": "success"}
    resp["HX-Trigger"] = _trigger_header(events)
    return resp


# --------------------------------------------------------------------------- #
# Pipelines & stages (columns)
# --------------------------------------------------------------------------- #
@login_required
def pipeline_create(request):
    if request.method == "POST" and can_edit(request):
        name = request.POST.get("name", "").strip()
        if name:
            pipelines = Pipeline.objects.filter(workspace=request.workspace)
            order = (pipelines.aggregate(m=Max("order"))["m"] or 0) + 1
            pipe = Pipeline.objects.create(
                workspace=request.workspace,
                name=name,
                order=order,
                is_default=not pipelines.exists(),
            )
            pipe.ensure_stages()
            return redirect(f"{reverse('crm:deal_board')}?pipeline={pipe.pk}")
    return redirect("crm:deal_board")


def _pipeline_modal_context(request, pipeline):
    deal_counts = {
        row["stage"]: row["count"]
        for row in Deal.objects.filter(workspace=request.workspace, pipeline=pipeline)
        .values("stage")
        .annotate(count=Count("id"))
    }
    stages = list(pipeline.stages.all())
    for stage in stages:
        stage.deal_count = deal_counts.get(stage.key, 0)
        stage.selected_card_fields = stage.card_fields or []
        stage.stage_fields = _stage_custom_fields(request.workspace, pipeline, stage.key)
    active_stage_id = str(
        getattr(request, "active_pipeline_stage", "")
        or request.POST.get("active_stage")
        or request.GET.get("active_stage")
        or (stages[0].pk if stages else "")
    )
    if active_stage_id not in {str(stage.pk) for stage in stages}:
        active_stage_id = str(stages[0].pk) if stages else ""
    return {
        "pipeline": pipeline,
        "stages": stages,
        "kind_choices": Stage.KIND_CHOICES,
        # Only the native "show on card" toggles live here now; custom fields are
        # created and listed inside each stage (modular, Pipefy-style).
        "card_field_options": [{"key": key, "label": label} for key, label in CARD_FIELD_CHOICES],
        "custom_field_type_choices": CustomField.TYPE_CHOICES,
        "active_stage_id": active_stage_id,
    }


def _pipeline_modal_response(request, pipeline, message=None, kind="success", extra_events=None):
    resp = render(request, "crm/partials/pipeline_customize.html", _pipeline_modal_context(request, pipeline))
    events = {
        "nucleo:dealsBoard": True,
        "nucleo:dealsStats": True,
    }
    if message:
        events["nucleo:toast"] = {"text": message, "kind": kind}
    if extra_events:
        events.update(extra_events)
    resp["HX-Trigger"] = _trigger_header(events)
    return resp


@login_required
def pipeline_customize(request, pk):
    if not can_edit(request):
        return _forbidden()
    pipeline = get_object_or_404(Pipeline, pk=pk, workspace=request.workspace)
    return render(request, "crm/partials/pipeline_customize.html", _pipeline_modal_context(request, pipeline))


@login_required
def pipeline_field_add(request, pk):
    if request.method != "POST":
        return HttpResponse(status=405)
    if not can_edit(request):
        return _forbidden()
    pipeline = get_object_or_404(Pipeline, pk=pk, workspace=request.workspace)
    label = request.POST.get("label", "").strip()
    field_type = request.POST.get("field_type", "text")
    required = request.POST.get("required") == "on"
    options = _custom_field_options(request.POST.get("options", ""))
    if not label:
        return _pipeline_modal_response(request, pipeline, "Informe o nome do campo.", "error")
    if field_type not in dict(CustomField.TYPE_CHOICES):
        return _pipeline_modal_response(request, pipeline, "Tipo de campo inválido.", "error")
    if field_type in ("select", "multiselect") and not options:
        return _pipeline_modal_response(request, pipeline, "Informe as opções do campo.", "error")
    CustomField.objects.create(
        workspace=request.workspace,
        object_type="deal",
        key=_unique_deal_field_key(request.workspace, label),
        label=label,
        field_type=field_type,
        options=options if field_type in ("select", "multiselect") else [],
        required=required,
        order=CustomField.objects.filter(workspace=request.workspace, object_type="deal").count(),
    )
    return _pipeline_modal_response(request, pipeline, f"Campo \"{label}\" criado.")


@login_required
def pipeline_field_update(request, pk, field_pk):
    if request.method != "POST":
        return HttpResponse(status=405)
    if not can_edit(request):
        return _forbidden()
    pipeline = get_object_or_404(Pipeline, pk=pk, workspace=request.workspace)
    field = get_object_or_404(CustomField, pk=field_pk, workspace=request.workspace, object_type="deal")
    label = request.POST.get("label", "").strip()
    field_type = request.POST.get("field_type", field.field_type)
    options = _custom_field_options(request.POST.get("options", ""))
    if not label:
        return _pipeline_modal_response(request, pipeline, "Informe o nome do campo.", "error")
    if field_type not in dict(CustomField.TYPE_CHOICES):
        return _pipeline_modal_response(request, pipeline, "Tipo de campo inválido.", "error")
    if field_type in ("select", "multiselect") and not options:
        return _pipeline_modal_response(request, pipeline, "Informe as opções do campo.", "error")
    field.label = label
    field.field_type = field_type
    field.options = options if field_type in ("select", "multiselect") else []
    field.required = request.POST.get("required") == "on"
    field.save(update_fields=["label", "field_type", "options", "required"])
    return _pipeline_modal_response(request, pipeline, f"Campo \"{field.label}\" atualizado.")


@login_required
def pipeline_field_delete(request, pk, field_pk):
    if request.method != "POST":
        return HttpResponse(status=405)
    if not can_edit(request):
        return _forbidden()
    pipeline = get_object_or_404(Pipeline, pk=pk, workspace=request.workspace)
    field = get_object_or_404(CustomField, pk=field_pk, workspace=request.workspace, object_type="deal")
    card_field_key = f"custom:{field.key}"
    field.delete()
    _remove_card_field_from_pipeline(pipeline, card_field_key)
    return _pipeline_modal_response(request, pipeline, "Campo removido.")


@login_required
def stage_field_add(request, pk):
    """Create a custom field that belongs to a single stage (Pipefy-style):
    the field is attached only to this stage and does not carry to others."""
    if request.method != "POST":
        return HttpResponse(status=405)
    if not can_edit(request):
        return _forbidden()
    stage = get_object_or_404(Stage, pk=pk, pipeline__workspace=request.workspace)
    pipeline = stage.pipeline
    label = request.POST.get("label", "").strip()
    field_type = request.POST.get("field_type", "text")
    required = request.POST.get("required") == "on"
    options = _custom_field_options(request.POST.get("options", ""))
    if not label:
        return _pipeline_modal_response(request, pipeline, "Informe o nome do campo.", "error")
    if field_type not in dict(CustomField.TYPE_CHOICES):
        return _pipeline_modal_response(request, pipeline, "Tipo de campo inválido.", "error")
    if field_type in ("select", "multiselect") and not options:
        return _pipeline_modal_response(request, pipeline, "Informe as opções do campo.", "error")
    field = CustomField.objects.create(
        workspace=request.workspace,
        object_type="deal",
        key=_unique_deal_field_key(request.workspace, label),
        label=label,
        field_type=field_type,
        options=options if field_type in ("select", "multiselect") else [],
        required=required,
        order=CustomField.objects.filter(workspace=request.workspace, object_type="deal").count(),
    )
    stage.card_fields = _merge_card_fields(stage.card_fields or [], [f"custom:{field.key}"])
    stage.save(update_fields=["card_fields", "updated_at"])
    return _pipeline_modal_response(request, pipeline, f"Campo “{label}” adicionado à fase “{stage.name}”.")


@login_required
def pipeline_update(request, pk):
    if request.method != "POST":
        return HttpResponse(status=405)
    if not can_edit(request):
        return _forbidden()
    pipeline = get_object_or_404(Pipeline, pk=pk, workspace=request.workspace)
    name = request.POST.get("name", "").strip()
    if not name:
        return _pipeline_modal_response(request, pipeline, "Informe o nome da pipeline.", "error")
    pipeline.name = name
    pipeline.save(update_fields=["name", "updated_at"])
    return _pipeline_modal_response(
        request,
        pipeline,
        "Pipeline atualizada.",
        "success",
        {"nucleo:pipelineRenamed": {"id": pipeline.pk, "name": pipeline.name}},
    )


@login_required
def stage_add(request):
    if request.method != "POST":
        return HttpResponse(status=405)
    if not can_edit(request):
        return _forbidden()
    pipeline = get_object_or_404(Pipeline, pk=request.POST.get("pipeline"), workspace=request.workspace)
    name = request.POST.get("name", "").strip()
    color = request.POST.get("color", "").strip() or "#64748b"
    kind = request.POST.get("kind", "open")
    message = "Informe o nome da fase."
    if name:
        order = (pipeline.stages.aggregate(m=Max("order"))["m"] or 0) + 1
        stage = pipeline.stages.create(
            key=_unique_stage_key(pipeline, name), name=name, color=color,
            kind=kind if kind in dict(Stage.KIND_CHOICES) else "open", order=order,
        )
        request.active_pipeline_stage = stage.pk
        message = f"Fase “{name}” criada."
    if request.POST.get("return_modal") == "1":
        return _pipeline_modal_response(request, pipeline, message, "success" if name else "error")
    resp = HttpResponse(status=204)
    resp["HX-Trigger"] = _trigger_header({
        "nucleo:dealsBoard": True, "nucleo:closeModal": True,
        "nucleo:toast": {"text": message, "kind": "success" if name else "error"},
    })
    return resp


@login_required
def stage_delete(request, pk):
    if request.method != "POST":
        return HttpResponse(status=405)
    if not can_edit(request):
        return _forbidden()
    stage = get_object_or_404(Stage, pk=pk, pipeline__workspace=request.workspace)
    pipeline = stage.pipeline
    has_deals = Deal.objects.filter(
        workspace=request.workspace, pipeline=pipeline, stage=stage.key
    ).exists()
    if has_deals:
        message, kind = "Mova os negócios desta fase antes de excluí-la.", "error"
    elif pipeline.stages.count() <= 1:
        message, kind = "A pipeline precisa de ao menos uma fase.", "error"
    else:
        stage.delete()
        message, kind = "Fase excluída.", "success"
    if request.POST.get("return_modal") == "1":
        return _pipeline_modal_response(request, pipeline, message, kind)
    resp = HttpResponse(status=204)
    resp["HX-Trigger"] = _trigger_header({
        "nucleo:dealsBoard": True, "nucleo:toast": {"text": message, "kind": kind},
    })
    return resp


@login_required
def stage_reorder(request):
    """Persist a new column order after dragging stages on the board."""
    if request.method != "POST":
        return HttpResponse(status=405)
    if not can_edit(request):
        return _forbidden()
    ids = [i for i in request.POST.get("order", "").split(",") if i]
    pipeline = None
    pipeline_id = request.POST.get("pipeline")
    if pipeline_id:
        pipeline = get_object_or_404(Pipeline, pk=pipeline_id, workspace=request.workspace)
    for pos, sid in enumerate(ids):
        qs = Stage.objects.filter(pk=sid, pipeline__workspace=request.workspace)
        if pipeline is not None:
            qs = qs.filter(pipeline=pipeline)
        qs.update(order=pos)
    if request.POST.get("return_modal") == "1" and pipeline is not None:
        return _pipeline_modal_response(request, pipeline, "Ordem das fases salva.")
    resp = HttpResponse(status=204)
    resp["HX-Trigger"] = _trigger_header({
        "nucleo:dealsBoard": True,
        "nucleo:toast": {"text": "Ordem das fases salva.", "kind": "success"},
    })
    return resp


@login_required
def stage_update(request, pk):
    """Rename / recolor / retype a column (stage)."""
    if request.method != "POST":
        return HttpResponse(status=405)
    if not can_edit(request):
        return _forbidden()
    stage = get_object_or_404(Stage, pk=pk, pipeline__workspace=request.workspace)
    name = request.POST.get("name", "").strip()
    if request.POST.get("return_modal") == "1" and not name:
        return _pipeline_modal_response(request, stage.pipeline, "Informe o nome da fase.", "error")
    if name:
        stage.name = name
    color = request.POST.get("color", "").strip()
    if color:
        stage.color = color
    kind = request.POST.get("kind")
    kind_changed = kind in dict(Stage.KIND_CHOICES) and kind != stage.kind
    if kind in dict(Stage.KIND_CHOICES):
        stage.kind = kind
    update_card_fields = request.POST.get("card_fields_present") == "1"
    if update_card_fields:
        # The stage panel only submits the native "show on card" toggles. Keep
        # this stage's own custom fields (managed separately) and never
        # propagate to other stages — each stage owns its fields.
        native = [v for v in _safe_card_fields(request.workspace, request.POST.getlist("card_fields"))
                  if not str(v).startswith("custom:")]
        existing_custom = [v for v in (stage.card_fields or []) if isinstance(v, str) and v.startswith("custom:")]
        stage.card_fields = native + existing_custom
    stage.save()
    if kind_changed:
        # keep deals' denormalized stage_kind in sync with the stage's new kind
        Deal.objects.filter(
            workspace=request.workspace, pipeline=stage.pipeline, stage=stage.key
        ).update(stage_kind=stage.kind)
    if request.POST.get("return_modal") == "1":
        return _pipeline_modal_response(request, stage.pipeline, f"Fase “{stage.name}” atualizada.")
    resp = HttpResponse(status=204)
    resp["HX-Trigger"] = _trigger_header({
        "nucleo:dealsBoard": True, "nucleo:dealsStats": True, "nucleo:closeModal": True,
        "nucleo:toast": {"text": f"Fase “{stage.name}” atualizada.", "kind": "success"},
    })
    return resp


def _scope_deal_fields(form, request):
    if "company" in form.fields:
        form.fields["company"].queryset = Company.objects.filter(workspace=request.workspace)
    if "contact" in form.fields:
        form.fields["contact"].queryset = Contact.objects.filter(workspace=request.workspace)


# --------------------------------------------------------------------------- #
# Detail pages
# --------------------------------------------------------------------------- #
@login_required
def company_detail(request, pk):
    company = get_object_or_404(Company, pk=pk, workspace=request.workspace)
    audit.record("view", object_type="company", object_id=company.pk, object_repr=str(company))
    context = {
        "page_title": company.name,
        "breadcrumb": ["CRM", "Empresas", company.name],
        "company": company,
        "contacts": company.contacts.all(),
        "deals": company.deals.all(),
        "subsidiaries": company.subsidiaries.all(),
        "attachments": company.attachments.all(),
        "custom_fields": with_values(get_fields(request.workspace, "company"), company),
        **_timeline_context("company", company),
    }
    return render(request, "crm/company_detail.html", context)


@login_required
def contact_detail(request, pk):
    contact = get_object_or_404(Contact.objects.select_related("company"), pk=pk, workspace=request.workspace)
    audit.record("view", object_type="contact", object_id=contact.pk, object_repr=str(contact))
    context = {
        "page_title": contact.full_name,
        "breadcrumb": ["CRM", "Contatos", contact.full_name],
        "contact": contact,
        "deals": contact.deals.all(),
        "attachments": contact.attachments.all(),
        "custom_fields": with_values(get_fields(request.workspace, "contact"), contact),
        "basis_choices": BASIS_CHOICES,
        **_timeline_context("contact", contact),
    }
    return render(request, "crm/contact_detail.html", context)


@login_required
def contact_privacy(request, pk):
    """Register or withdraw a contact's consent (LGPD legal basis)."""
    if not can_edit(request):
        return HttpResponseForbidden()
    contact = get_object_or_404(Contact, pk=pk, workspace=request.workspace)
    if request.method == "POST" and not contact.is_anonymized:
        status = request.POST.get("status")
        basis = request.POST.get("basis") or ""
        if status in dict(CONSENT_STATUS_CHOICES):
            contact.consent_status = status
            if basis in dict(BASIS_CHOICES):
                contact.consent_basis = basis
            if status == "granted":
                contact.consent_source = contact.consent_source or "Registrado manualmente"
                contact.consent_at = timezone.now()
            contact.save(update_fields=[
                "consent_status", "consent_basis", "consent_source", "consent_at", "updated_at",
            ])
            audit.record(
                "consent", object_type="contact", object_id=contact.pk,
                object_repr=str(contact), changes={"status": status, "basis": basis},
            )
            messages.success(request, "Consentimento atualizado.")
    return redirect(contact.get_absolute_url())


def _is_admin(request):
    return bool(getattr(request, "membership", None) and request.membership.can("admin"))


@login_required
def contact_forget(request, pk):
    """LGPD right to erasure: anonymise the data subject on request. Keeps the
    row (and deal history/metrics) but wipes every personal field."""
    if not _is_admin(request):
        return HttpResponseForbidden()
    contact = get_object_or_404(Contact, pk=pk, workspace=request.workspace)
    if request.method == "POST":
        from core.privacy import anonymize_contact
        if anonymize_contact(contact, reason="erasure", source="user"):
            messages.success(request, "Dados pessoais anonimizados (direito ao esquecimento).")
        else:
            messages.info(request, "Este contato já estava anonimizado.")
    return redirect(contact.get_absolute_url())


@login_required
def contact_export(request, pk):
    """LGPD right of access/portability: download everything we hold on this
    data subject as JSON. Logged as an export."""
    if not _is_admin(request):
        return HttpResponseForbidden()
    contact = get_object_or_404(Contact.objects.select_related("company"), pk=pk, workspace=request.workspace)
    data = {
        "exportado_em": timezone.now().isoformat(),
        "workspace": request.workspace.name,
        "titular": {
            "id": contact.pk,
            "nome": contact.first_name,
            "sobrenome": contact.last_name,
            "email": contact.email,
            "telefone": contact.phone,
            "cargo": contact.job_title,
            "empresa": contact.company.name if contact.company else None,
            "endereco": {
                "logradouro": contact.address_street,
                "numero": contact.address_number,
                "complemento": contact.address_complement,
                "bairro": contact.district,
                "cidade": contact.city,
                "uf": contact.state,
                "cep": contact.zipcode,
            },
            "estagio": contact.get_stage_display(),
            "campos_personalizados": contact.custom or {},
            "consentimento": {
                "status": contact.get_consent_status_display(),
                "base_legal": contact.get_consent_basis_display(),
                "origem": contact.consent_source,
                "data": contact.consent_at.isoformat() if contact.consent_at else None,
            },
            "criado_em": contact.created_at.isoformat() if contact.created_at else None,
        },
        "negocios": [
            {"titulo": d.title, "valor": float(d.value or 0), "estagio": d.get_stage_display()}
            for d in contact.deals.all()
        ],
        "atividades": [
            {"tipo": a.get_kind_display(), "conteudo": a.body,
             "data": a.created_at.isoformat() if a.created_at else None}
            for a in contact.activities.all()
        ],
    }
    audit.record("export", object_type="contact", object_id=contact.pk, object_repr=str(contact))
    resp = JsonResponse(data, json_dumps_params={"ensure_ascii": False, "indent": 2})
    resp["Content-Disposition"] = f'attachment; filename="titular_{contact.pk}.json"'
    return resp


@login_required
def deal_detail(request, pk):
    deal = get_object_or_404(Deal.objects.select_related("company", "contact"), pk=pk, workspace=request.workspace)
    audit.record("view", object_type="deal", object_id=deal.pk, object_repr=str(deal))
    stage_fields = _stage_custom_fields(request.workspace, deal.pipeline, deal.stage) if deal.pipeline_id else get_fields(request.workspace, "deal")
    context = {
        "page_title": deal.title,
        "breadcrumb": ["CRM", "Negócios", deal.title],
        "deal": deal,
        "attachments": deal.attachments.all(),
        "custom_fields": with_values(stage_fields, deal),
        **_timeline_context("deal", deal),
    }
    return render(request, "crm/deal_detail.html", context)


# --------------------------------------------------------------------------- #
# Activities (timeline: notes + tasks)
# --------------------------------------------------------------------------- #
_OWNERS = {"company": Company, "contact": Contact, "deal": Deal}


def _resolve_owner(request, on, oid):
    model = _OWNERS.get(on)
    if not model:
        return None, None
    obj = get_object_or_404(model, pk=oid, workspace=request.workspace)
    return obj, on


def _timeline_context(on, obj):
    return {
        "owner": on,
        "owner_id": obj.pk,
        "activities": obj.activities.select_related("author").all(),
    }


def _render_timeline(request, on, obj):
    return render(request, "crm/partials/timeline.html", _timeline_context(on, obj))


@login_required
def activity_create(request):
    if request.method != "POST":
        return HttpResponse(status=405)
    if not can_edit(request):
        return _forbidden()
    obj, owner_key = _resolve_owner(request, request.POST.get("on"), request.POST.get("id"))
    if obj is None:
        return HttpResponse(status=400)
    form = ActivityForm(request.POST)
    if form.is_valid():
        activity = form.save(commit=False)
        activity.author = request.user
        activity.workspace = request.workspace
        setattr(activity, owner_key, obj)
        activity.save()
        _apply_mentions(request, activity)
        return _with_toast(_render_timeline(request, owner_key, obj), "Atividade adicionada.")
    return _render_timeline(request, owner_key, obj)


@login_required
def activity_toggle(request, pk):
    if request.method != "POST":
        return HttpResponse(status=405)
    if not can_edit(request):
        return _forbidden()
    activity = get_object_or_404(Activity, pk=pk, workspace=request.workspace)
    activity.done = not activity.done
    activity.save(update_fields=["done", "updated_at"])
    on, obj = _owner_of(activity)
    message = "Tarefa concluída." if activity.done else "Tarefa reaberta."
    return _with_toast(_render_timeline(request, on, obj), message)


@login_required
def activity_delete(request, pk):
    if request.method != "POST":
        return HttpResponse(status=405)
    if not can_edit(request):
        return _forbidden()
    activity = get_object_or_404(Activity, pk=pk, workspace=request.workspace)
    on, obj = _owner_of(activity)
    activity.delete()
    return _with_toast(_render_timeline(request, on, obj), "Atividade excluída.")


@login_required
def tasks(request):
    """Workspace-wide task list — Activities of kind 'task', grouped by due date."""
    from django.utils import timezone

    ws = request.workspace
    show = request.GET.get("show", "pending")
    if show not in {"pending", "done", "all"}:
        show = "pending"
    base = Activity.objects.filter(workspace=ws, kind="task").select_related(
        "author", "deal", "contact", "company",
    )
    pending_count = base.filter(done=False).count()
    done_count = base.filter(done=True).count()
    qs = base
    if show == "pending":
        qs = qs.filter(done=False)
    elif show == "done":
        qs = qs.filter(done=True)
    today = timezone.localdate()
    groups = {"overdue": [], "today": [], "upcoming": [], "nodate": [], "flat": []}
    for activity in qs.order_by("due_date", "-created_at")[:400]:
        activity.rec = activity.deal or activity.contact or activity.company
        if show != "pending":
            groups["flat"].append(activity)
        elif activity.due_date and activity.due_date < today:
            groups["overdue"].append(activity)
        elif activity.due_date == today:
            groups["today"].append(activity)
        elif activity.due_date:
            groups["upcoming"].append(activity)
        else:
            groups["nodate"].append(activity)
    return render(request, "crm/tasks.html", {
        "page_title": "Tarefas",
        "breadcrumb": ["CRM", "Tarefas"],
        "show": show,
        "groups": groups,
        "pending_count": pending_count,
        "done_count": done_count,
        "today": today,
        "can_edit": can_edit(request),
    })


@login_required
def task_create(request):
    from django.utils.dateparse import parse_date

    if request.method != "POST" or not can_edit(request):
        return redirect("crm:tasks")
    body = request.POST.get("body", "").strip()
    due = request.POST.get("due_date", "").strip()
    if body:
        Activity.objects.create(
            workspace=request.workspace, kind="task", source="manual",
            body=body[:2000], due_date=parse_date(due) if due else None,
            author=request.user,
        )
        messages.success(request, "Tarefa criada.")
    return redirect("crm:tasks")


@login_required
def task_toggle(request, pk):
    if request.method != "POST" or not can_edit(request):
        return redirect("crm:tasks")
    activity = get_object_or_404(Activity, pk=pk, workspace=request.workspace, kind="task")
    activity.done = not activity.done
    activity.save(update_fields=["done", "updated_at"])
    messages.success(request, "Tarefa concluída." if activity.done else "Tarefa reaberta.")
    show = request.POST.get("show", "pending")
    return redirect(f"{reverse('crm:tasks')}?show={show}")


def _owner_of(activity):
    if activity.company_id:
        return "company", activity.company
    if activity.contact_id:
        return "contact", activity.contact
    return "deal", activity.deal
