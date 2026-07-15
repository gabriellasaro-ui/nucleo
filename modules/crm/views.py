import json
import re

from django.contrib import messages
from django.contrib.auth import get_user_model
from django.contrib.auth.decorators import login_required
from django.db.models import Count, Max, Q, Sum
from django.http import HttpResponse
from django.shortcuts import get_object_or_404, render
from django.urls import reverse
from django.utils.text import slugify

from core.customfields import get_fields, read_from_post, with_values
from core.events import emit
from core.rbac import can_edit

from django.shortcuts import redirect

from .forms import ActivityForm, CompanyForm, ContactForm, DealForm
from .models import Activity, Attachment, Company, Contact, Deal, Pipeline, Stage, Tag


def _default_pipeline(request):
    pipe = Pipeline.objects.filter(is_default=True).first() or Pipeline.objects.first()
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
    names = [t.strip() for t in request.POST.get("tags_text", "").split(",") if t.strip()]
    tags = []
    for name in list(dict.fromkeys(names))[:20]:
        tag, _ = Tag.all_objects.get_or_create(workspace=request.workspace, name=name)
        tags.append(tag)
    obj.tags.set(tags)


def _tags_text(obj):
    return ", ".join(t.name for t in obj.tags.all()) if (obj and obj.pk) else ""


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
        "tags_text": _tags_text(instance),
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
        "tags_text": _tags_text(instance),
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
    base = slugify(name).replace("-", "_")[:40] or "etapa"
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


def _board_context(request):
    pipeline = _current_pipeline(request)
    stages = list(pipeline.stages.all())
    deals = list(
        Deal.objects.filter(workspace=request.workspace, pipeline=pipeline).select_related("company", "contact")
    )
    columns = []
    for st in stages:
        col_deals = [d for d in deals if d.stage == st.key]
        columns.append({
            "key": st.key, "label": st.name, "color": st.color, "kind": st.kind, "stage_id": st.pk,
            "deals": col_deals,
            "total": sum((d.value for d in col_deals), 0),
            "count": len(col_deals),
        })
    return {
        "columns": columns,
        "stats": _deal_stats(request, pipeline),
        "pipeline": pipeline,
        "pipelines": list(Pipeline.objects.all()),
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
    if request.method == "POST":
        if not can_edit(request):
            return _forbidden()
        is_new = instance is None
        form = DealForm(request.POST, instance=instance)
        _scope_deal_fields(form, request)
        _scope_owner_field(form, request)
        if form.is_valid():
            deal = form.save(commit=False)
            deal.workspace = request.workspace
            if not deal.pipeline_id:
                deal.pipeline = _default_pipeline(request)
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
    context = {
        "form": form,
        "title": "Editar negócio" if instance else "Novo negócio",
        "action": request.path,
        "delete_pk": instance.pk if instance else None,
        "custom_fields": with_values(get_fields(request.workspace, "deal"), instance),
        "tags_text": _tags_text(instance),
        "members": _workspace_members(request),
        "assignee_ids": _assignee_ids(instance),
    }
    return render(request, "crm/partials/deal_form.html", context)


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
            order = (Pipeline.objects.aggregate(m=Max("order"))["m"] or 0) + 1
            pipe = Pipeline.objects.create(workspace=request.workspace, name=name, order=order)
            pipe.ensure_stages()
            return redirect(f"{reverse('crm:deal_board')}?pipeline={pipe.pk}")
    return redirect("crm:deal_board")


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
    message = "Informe o nome da etapa."
    if name:
        order = (pipeline.stages.aggregate(m=Max("order"))["m"] or 0) + 1
        pipeline.stages.create(
            key=_unique_stage_key(pipeline, name), name=name, color=color,
            kind=kind if kind in dict(Stage.KIND_CHOICES) else "open", order=order,
        )
        message = f"Etapa “{name}” criada."
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
    has_deals = Deal.objects.filter(
        workspace=request.workspace, pipeline=stage.pipeline, stage=stage.key
    ).exists()
    if has_deals:
        message, kind = "Mova os negócios desta etapa antes de excluí-la.", "error"
    elif stage.pipeline.stages.count() <= 1:
        message, kind = "A pipeline precisa de ao menos uma etapa.", "error"
    else:
        stage.delete()
        message, kind = "Etapa excluída.", "success"
    resp = HttpResponse(status=204)
    resp["HX-Trigger"] = _trigger_header({
        "nucleo:dealsBoard": True, "nucleo:toast": {"text": message, "kind": kind},
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
    context = {
        "page_title": contact.full_name,
        "breadcrumb": ["CRM", "Contatos", contact.full_name],
        "contact": contact,
        "deals": contact.deals.all(),
        "attachments": contact.attachments.all(),
        "custom_fields": with_values(get_fields(request.workspace, "contact"), contact),
        **_timeline_context("contact", contact),
    }
    return render(request, "crm/contact_detail.html", context)


@login_required
def deal_detail(request, pk):
    deal = get_object_or_404(Deal.objects.select_related("company", "contact"), pk=pk, workspace=request.workspace)
    context = {
        "page_title": deal.title,
        "breadcrumb": ["CRM", "Negócios", deal.title],
        "deal": deal,
        "attachments": deal.attachments.all(),
        "custom_fields": with_values(get_fields(request.workspace, "deal"), deal),
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


def _owner_of(activity):
    if activity.company_id:
        return "company", activity.company
    if activity.contact_id:
        return "contact", activity.contact
    return "deal", activity.deal
