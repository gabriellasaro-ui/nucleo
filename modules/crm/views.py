from django.contrib.auth import get_user_model
from django.contrib.auth.decorators import login_required
from django.db.models import Count, Q, Sum
from django.http import HttpResponse
from django.shortcuts import get_object_or_404, render

from core.customfields import get_fields, read_from_post, with_values
from core.events import emit
from core.rbac import can_edit

from .forms import ActivityForm, CompanyForm, ContactForm, DealForm
from .models import Activity, Company, Contact, Deal, Tag

# HTMX helper: 204 + client events so the modal closes and lists refresh.
_REFRESH_HEADER = '{"nucleo:closeModal": true, "nucleo:dataChanged": true}'


def _refresh_response():
    resp = HttpResponse(status=204)
    resp["HX-Trigger"] = _REFRESH_HEADER
    return resp


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
        form = CompanyForm(request.POST, instance=instance)
        _scope_owner_field(form, request)
        if form.is_valid():
            company = form.save(commit=False)
            company.workspace = request.workspace
            _apply_custom(request, company, "company")
            company.save()
            _apply_tags(request, company)
            if instance is None:
                emit(request.workspace, "company_created", company)
            return _refresh_response()
    else:
        form = CompanyForm(instance=instance)
        _scope_owner_field(form, request)
    context = {
        "form": form,
        "title": "Editar empresa" if instance else "Nova empresa",
        "action": request.path,
        "delete_pk": instance.pk if instance else None,
        "custom_fields": with_values(get_fields(request.workspace, "company"), instance),
        "tags_text": _tags_text(instance),
    }
    return render(request, "crm/partials/company_form.html", context)


@login_required
def company_delete(request, pk):
    if request.method == "POST":
        if not can_edit(request):
            return _forbidden()
        get_object_or_404(Company, pk=pk, workspace=request.workspace).delete()
        return _refresh_response()
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
    if request.method == "POST":
        if not can_edit(request):
            return _forbidden()
        form = ContactForm(request.POST, instance=instance)
        _scope_company_field(form, request)
        _scope_owner_field(form, request)
        if form.is_valid():
            contact = form.save(commit=False)
            contact.workspace = request.workspace
            _apply_custom(request, contact, "contact")
            contact.save()
            _apply_tags(request, contact)
            if instance is None:
                emit(request.workspace, "contact_created", contact)
            return _refresh_response()
    else:
        form = ContactForm(instance=instance)
        _scope_company_field(form, request)
        _scope_owner_field(form, request)
    context = {
        "form": form,
        "title": "Editar contato" if instance else "Novo contato",
        "action": request.path,
        "delete_pk": instance.pk if instance else None,
        "custom_fields": with_values(get_fields(request.workspace, "contact"), instance),
        "tags_text": _tags_text(instance),
    }
    return render(request, "crm/partials/contact_form.html", context)


@login_required
def contact_delete(request, pk):
    if request.method == "POST":
        if not can_edit(request):
            return _forbidden()
        get_object_or_404(Contact, pk=pk, workspace=request.workspace).delete()
        return _refresh_response()
    return HttpResponse(status=405)


def _scope_company_field(form, request):
    if "company" in form.fields:
        form.fields["company"].queryset = Company.objects.filter(workspace=request.workspace)


# --------------------------------------------------------------------------- #
# Deals (Kanban pipeline)
# --------------------------------------------------------------------------- #
def _deals_refresh_response():
    resp = HttpResponse(status=204)
    resp["HX-Trigger"] = (
        '{"nucleo:closeModal": true, "nucleo:dataChanged": true, '
        '"nucleo:dealsBoard": true, "nucleo:dealsStats": true}'
    )
    return resp


def _deal_stats(request):
    agg = Deal.objects.filter(workspace=request.workspace).aggregate(
        open_value=Sum("value", filter=Q(stage__in=Deal.OPEN_STAGES)),
        open_count=Count("id", filter=Q(stage__in=Deal.OPEN_STAGES)),
        won_value=Sum("value", filter=Q(stage="ganho")),
    )
    return {
        "open_value": agg["open_value"] or 0,
        "open_count": agg["open_count"] or 0,
        "won_value": agg["won_value"] or 0,
    }


def _board_context(request):
    deals = list(Deal.objects.filter(workspace=request.workspace).select_related("company", "contact"))
    columns = []
    for key, label in Deal.STAGE_CHOICES:
        col_deals = [d for d in deals if d.stage == key]
        columns.append({
            "key": key,
            "label": label,
            "deals": col_deals,
            "total": sum((d.value for d in col_deals), 0),
            "count": len(col_deals),
        })
    return {"columns": columns, "stats": _deal_stats(request)}


@login_required
def deal_board(request):
    context = {"page_title": "Negócios", "breadcrumb": ["CRM", "Negócios"], **_board_context(request)}
    return render(request, "crm/deal_board.html", context)


@login_required
def deal_board_cards(request):
    return render(request, "crm/partials/deal_columns.html", _board_context(request))


@login_required
def deal_stats(request):
    return render(request, "crm/partials/deal_stats.html", {"stats": _deal_stats(request)})


@login_required
def deal_form(request, pk=None):
    instance = get_object_or_404(Deal, pk=pk, workspace=request.workspace) if pk else None
    old_stage = instance.stage if instance else None
    if request.method == "POST":
        if not can_edit(request):
            return _forbidden()
        form = DealForm(request.POST, instance=instance)
        _scope_deal_fields(form, request)
        _scope_owner_field(form, request)
        if form.is_valid():
            deal = form.save(commit=False)
            deal.workspace = request.workspace
            _apply_custom(request, deal, "deal")
            deal.save()
            _apply_tags(request, deal)
            if instance is None:
                emit(request.workspace, "deal_created", deal)
            elif deal.stage != old_stage:
                emit(request.workspace, "deal_stage_changed", deal,
                     {"stage": deal.stage, "old_stage": old_stage})
            return _deals_refresh_response()
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
    }
    return render(request, "crm/partials/deal_form.html", context)


@login_required
def deal_delete(request, pk):
    if request.method == "POST":
        if not can_edit(request):
            return _forbidden()
        get_object_or_404(Deal, pk=pk, workspace=request.workspace).delete()
        return _deals_refresh_response()
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
    if stage in dict(Deal.STAGE_CHOICES) and stage != old_stage:
        deal.stage = stage
        deal.save(update_fields=["stage", "updated_at"])
        emit(request.workspace, "deal_stage_changed", deal, {"stage": stage, "old_stage": old_stage})
    ids = [i for i in request.POST.get("order", "").split(",") if i]
    for pos, deal_id in enumerate(ids):
        Deal.objects.filter(pk=deal_id, workspace=request.workspace).update(order=pos)
    resp = HttpResponse(status=204)
    resp["HX-Trigger"] = '{"nucleo:dealsStats": true}'
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
    return _render_timeline(request, on, obj)


@login_required
def activity_delete(request, pk):
    if request.method != "POST":
        return HttpResponse(status=405)
    if not can_edit(request):
        return _forbidden()
    activity = get_object_or_404(Activity, pk=pk, workspace=request.workspace)
    on, obj = _owner_of(activity)
    activity.delete()
    return _render_timeline(request, on, obj)


def _owner_of(activity):
    if activity.company_id:
        return "company", activity.company
    if activity.contact_id:
        return "contact", activity.contact
    return "deal", activity.deal
