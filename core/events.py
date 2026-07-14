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
            if (
                event.event_type == "deal_stage_changed"
                and auto.condition_stage
                and event.payload.get("stage") != auto.condition_stage
            ):
                continue
            _run_action(auto, obj)
            Automation.objects.filter(pk=auto.pk).update(run_count=F("run_count") + 1)
    event.processed = True
    event.save(update_fields=["processed"])
    return event


def _run_action(auto, obj):
    from modules.crm.models import Activity, Company, Contact, Deal

    kind = "task" if auto.action == "create_task" else "note"
    due = timezone.localdate() + timedelta(days=auto.action_due_days) if kind == "task" else None
    kwargs = {
        "workspace": auto.workspace,
        "kind": kind,
        "source": "automation",
        "body": auto.action_text,
        "due_date": due,
    }
    if isinstance(obj, Deal):
        kwargs["deal"] = obj
    elif isinstance(obj, Company):
        kwargs["company"] = obj
    elif isinstance(obj, Contact):
        kwargs["contact"] = obj
    Activity.objects.create(**kwargs)
