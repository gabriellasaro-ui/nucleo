"""Outbox + automation engine.

`emit()` records an Event (the outbox) and processes it right away so effects
are instant. The stored Event doubles as an audit log, and `process()` can be
re-run later (e.g. by the `process_events` command) by reconstructing the
object from the payload — that's the async escape hatch of the outbox pattern.
"""
from datetime import timedelta

from django.db.models import F
from django.utils import timezone

from .models import Automation, Event


def _model_for(name):
    from modules.crm.models import Company, Contact, Deal
    return {"company": Company, "contact": Contact, "deal": Deal}.get(name)


def _reconstruct(event):
    model = _model_for(event.payload.get("object_model"))
    if not model:
        return None
    return model.objects.filter(pk=event.payload.get("object_id"), workspace=event.workspace).first()


def emit(workspace, event_type, obj, payload=None):
    data = dict(payload or {})
    data["object_model"] = obj.__class__.__name__.lower()
    data["object_id"] = obj.pk
    event = Event.objects.create(
        workspace=workspace,
        event_type=event_type,
        object_repr=str(obj)[:200],
        payload=data,
    )
    process(event, obj)
    return event


def process(event, obj=None):
    if obj is None:
        obj = _reconstruct(event)
    if obj is not None:
        automations = Automation.objects.filter(
            workspace=event.workspace, trigger=event.event_type, active=True
        )
        for auto in automations:
            if not _matches(auto, event):
                continue
            _run_action(auto, obj)
            Automation.objects.filter(pk=auto.pk).update(run_count=F("run_count") + 1)
    event.processed = True
    event.save(update_fields=["processed"])
    return event


def _matches(auto, event):
    if auto.condition_stage and event.payload.get("stage") != auto.condition_stage:
        return False
    return True


def _run_action(auto, obj):
    current = obj
    for action in auto.normalized_actions():
        result = _run_single_action(auto, current, action)
        if result is not None:
            current = result


def _run_single_action(auto, obj, action):
    action_type = action.get("type")
    if action_type in {"create_task", "create_note"}:
        _create_activity(auto, obj, action)
        return None
    if action_type == "create_deal":
        return _create_deal(auto, obj, action)
    if action_type == "move_deal":
        return _move_deal(auto, obj, action)
    if action_type == "set_contact_stage":
        _set_contact_stage(obj, action)
        return None
    return None


def _create_activity(auto, obj, action):
    from modules.crm.models import Activity, Company, Contact, Deal

    kind = "task" if action.get("type") == "create_task" else "note"
    due_days = _int_or_default(action.get("due_days"), auto.action_due_days)
    due = timezone.localdate() + timedelta(days=due_days) if kind == "task" else None
    kwargs = {
        "workspace": auto.workspace,
        "kind": kind,
        "source": "automation",
        "body": action.get("text") or auto.action_text or "Ação criada pela automação.",
        "due_date": due,
    }
    if isinstance(obj, Deal):
        kwargs["deal"] = obj
    elif isinstance(obj, Company):
        kwargs["company"] = obj
    elif isinstance(obj, Contact):
        kwargs["contact"] = obj
    Activity.objects.create(**kwargs)


def _create_deal(auto, obj, action):
    from modules.crm.models import Company, Contact, Deal

    company = None
    contact = None
    if isinstance(obj, Deal):
        company = obj.company
        contact = obj.contact
    elif isinstance(obj, Contact):
        contact = obj
        company = obj.company
    elif isinstance(obj, Company):
        company = obj

    stage = action.get("deal_stage") or action.get("stage") or "novo"
    title = action.get("text") or _deal_title_for(obj)
    order = Deal.all_objects.filter(workspace=auto.workspace, stage=stage).count()
    return Deal.all_objects.create(
        workspace=auto.workspace,
        title=title,
        stage=stage,
        order=order,
        company=company,
        contact=contact,
    )


def _move_deal(auto, obj, action):
    from modules.crm.models import Contact, Deal

    deal = obj if isinstance(obj, Deal) else None
    if deal is None and isinstance(obj, Contact):
        deal = (
            Deal.all_objects.filter(workspace=auto.workspace, contact=obj)
            .order_by("-created_at")
            .first()
        )
    if deal is None:
        return None

    stage = action.get("deal_stage") or action.get("stage")
    if stage and deal.stage != stage:
        deal.stage = stage
        deal.order = Deal.all_objects.filter(workspace=auto.workspace, stage=stage).count()
        deal.save(update_fields=["stage", "order", "updated_at"])
    return deal


def _set_contact_stage(obj, action):
    from modules.crm.models import Contact, Deal

    contact = obj if isinstance(obj, Contact) else None
    if contact is None and isinstance(obj, Deal):
        contact = obj.contact
    stage = action.get("contact_stage") or action.get("stage")
    if contact is not None and stage and contact.stage != stage:
        contact.stage = stage
        contact.save(update_fields=["stage", "updated_at"])


def _deal_title_for(obj):
    from modules.crm.models import Company, Contact, Deal

    if isinstance(obj, Deal):
        return obj.title
    if isinstance(obj, Contact):
        return f"Oportunidade - {obj.full_name}"
    if isinstance(obj, Company):
        return f"Oportunidade - {obj.name}"
    return "Nova oportunidade"


def _int_or_default(value, default):
    try:
        return max(0, int(value))
    except (TypeError, ValueError):
        return default
