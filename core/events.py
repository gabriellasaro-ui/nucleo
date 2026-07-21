"""Outbox + automation engine."""
import base64
from datetime import timedelta
import json
import re
from urllib import parse as urlparse, request as urlrequest
from urllib.error import URLError

from django.db.models import F
from django.utils.dateparse import parse_datetime
from django.utils.text import slugify
from django.utils import timezone

from .models import Automation, AutomationRun, Event


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
    automations = Automation.objects.filter(
        workspace=event.workspace, trigger=event.event_type, active=True
    )
    for auto in automations:
        run_automation_for_event(auto, event, obj)
    event.processed = True
    event.save(update_fields=["processed"])
    return event


def run_automation_for_event(auto, event, obj=None):
    if obj is None:
        obj = _reconstruct(event)
    status, path = _run_automation(auto, event, obj)
    if status not in {"skipped", "error"}:
        Automation.objects.filter(pk=auto.pk).update(run_count=F("run_count") + 1)
    return _save_run(auto, event, obj, status, path)


def _matches(auto, event):
    if auto.condition_stage and event.payload.get("stage") != auto.condition_stage:
        return False
    return True


def _save_run(auto, event, obj, status, path):
    scheduled_for = None
    resume_node_id = ""
    if status == "scheduled" and path:
        last = path[-1]
        resume_node_id = str(last.get("resume_node_id") or "")[:80]
        raw_scheduled_for = last.get("scheduled_for")
        if raw_scheduled_for:
            scheduled_for = parse_datetime(raw_scheduled_for)
    return AutomationRun.objects.create(
        workspace=auto.workspace,
        automation=auto,
        event=event,
        status=status,
        object_model=event.payload.get("object_model", ""),
        object_id=event.payload.get("object_id"),
        object_repr=str(obj)[:200] if obj is not None else event.object_repr,
        summary=_path_summary(path),
        path=path,
        scheduled_for=scheduled_for,
        resume_node_id=resume_node_id,
    )


def _path_summary(path):
    if not path:
        return "Nenhum node executado."
    last = path[-1]
    if last.get("status") in {"error", "scheduled", "skipped"}:
        return last.get("message", "Fluxo interrompido.")[:300]
    done = [step.get("label") for step in path if step.get("status") == "success"]
    return " -> ".join([x for x in done if x])[:300] or "Fluxo executado."


def _run_automation(auto, event, obj):
    if auto.canvas and auto.canvas.get("nodes"):
        return _run_canvas(auto, event, obj)
    if not _matches(auto, event):
        return "skipped", [{"label": "Condicao", "status": "skipped", "message": "Evento nao passou pela condicao."}]
    path = _run_action(auto, obj)
    return _status_from_path(path), path


def automation_trigger_data(auto):
    for node in (auto.canvas or {}).get("nodes", []):
        if node.get("type") == "trigger":
            return node.get("data") or {}
    return {}


def resume_scheduled_run(run):
    if run.status != "scheduled":
        return run
    if run.scheduled_for and run.scheduled_for > timezone.now():
        return run
    if not run.automation_id or not run.event_id or not run.resume_node_id:
        run.status = "error"
        run.summary = "Execucao agendada sem contexto para retomar."
        run.resumed_at = timezone.now()
        run.save(update_fields=["status", "summary", "resumed_at"])
        return run

    obj = _reconstruct(run.event)
    if obj is None and run.event.payload.get("object_model"):
        run.status = "error"
        run.summary = "Registro original nao encontrado para retomar o fluxo."
        run.resumed_at = timezone.now()
        run.save(update_fields=["status", "summary", "resumed_at"])
        return run

    status, next_path = _run_canvas(run.automation, run.event, obj, start_node_id=run.resume_node_id)
    full_path = list(run.path or []) + next_path
    run.status = status
    run.path = full_path
    run.summary = _path_summary(full_path)
    run.resumed_at = timezone.now()
    if status == "scheduled" and next_path:
        last = next_path[-1]
        run.resume_node_id = str(last.get("resume_node_id") or "")[:80]
        raw_scheduled_for = last.get("scheduled_for")
        run.scheduled_for = parse_datetime(raw_scheduled_for) if raw_scheduled_for else None
    else:
        run.resume_node_id = ""
        run.scheduled_for = None
    run.save(update_fields=["status", "path", "summary", "resumed_at", "resume_node_id", "scheduled_for"])
    return run


def _run_canvas(auto, event, obj, start_node_id=None):
    nodes = {node.get("id"): node for node in auto.canvas.get("nodes", [])}
    outgoing = {}
    incoming_count = {}
    for edge in auto.canvas.get("edges", []):
        source = edge.get("from")
        target = edge.get("to")
        outgoing.setdefault(source, []).append((target, edge.get("branch", "")))
        incoming_count[target] = incoming_count.get(target, 0) + 1

    trigger_nodes = [node_id for node_id, node in nodes.items() if node.get("type") == "trigger"]
    start_id = start_node_id or (trigger_nodes[0] if trigger_nodes else "entry")
    queue = [(start_id, obj)]
    path = []
    edge_visits = {}
    merge_arrivals = {}
    merge_contexts = {}
    released_merges = set()
    condition_results = {}
    max_steps = max(20, len(nodes) * 12)
    steps = 0

    while queue and steps < max_steps:
        current_id, current_obj = queue.pop(0)
        edges_out = outgoing.get(current_id, [])
        current_node = nodes.get(current_id)
        # If the source is a condition-type node, route by which branch matched:
        # "sim" follows default/true edges, "senão" follows the false edges.
        if current_node and current_node.get("type") in {"filter", "condition", "switch"} and current_id in condition_results:
            if condition_results[current_id]:
                edges_out = [pair for pair in edges_out if pair[1] != "false"]
            else:
                edges_out = [pair for pair in edges_out if pair[1] == "false"]
        next_ids = [target for target, _branch in edges_out if target in nodes]
        if not next_ids:
            continue
        for next_id in next_ids:
            visit_key = (current_id, next_id)
            edge_visits[visit_key] = edge_visits.get(visit_key, 0) + 1
            if edge_visits[visit_key] > 30:
                path.append({"label": "Loop", "status": "error", "message": "Loop detectado no canvas."})
                return "error", path

            node = nodes[next_id]
            node_type = node.get("type")
            data = node.get("data") or {}
            next_obj = current_obj
            steps += 1
            if steps >= max_steps:
                path.append({"label": "Loop", "status": "error", "message": "Limite de execucao do canvas atingido."})
                return "error", path

            if node_type in {"filter", "condition", "switch"}:
                matched = _condition_matches(auto, event, current_obj, data)
                condition_results[next_id] = matched
                label = _node_label(node)
                if matched:
                    path.append({"label": label, "status": "success", "message": "Condição atendida."})
                else:
                    has_false = any(pair[1] == "false" for pair in outgoing.get(next_id, []))
                    path.append({
                        "label": label,
                        "status": "success" if has_false else "skipped",
                        "message": "Condição não atendida — segue pelo senão." if has_false else "Condição não atendida.",
                    })
                queue.append((next_id, next_obj))
                continue

            if node_type == "merge":
                merge_arrivals[next_id] = merge_arrivals.get(next_id, 0) + 1
                merge_contexts.setdefault(next_id, []).append(current_obj)
                expected = max(1, incoming_count.get(next_id, 1))
                mode = data.get("merge_mode") or "wait_all"
                if mode == "wait_all" and merge_arrivals[next_id] < expected:
                    path.append({"label": _node_label(node), "status": "success", "message": f"Aguardando entradas {merge_arrivals[next_id]}/{expected}."})
                    continue
                if next_id in released_merges:
                    continue
                released_merges.add(next_id)
                path.append({"label": _node_label(node), "status": "success", "message": "Caminhos mesclados."})
                queue.append((next_id, _merge_contexts(merge_contexts.get(next_id) or [next_obj])))
                continue

            if node_type == "loop":
                mode = data.get("loop_mode") or "for_each"
                repeat = data.get("loop_count") if mode == "repeat_times" else 1
                repeat = max(1, min(_int_or_default(repeat, 1), 30))
                path.append({"label": _node_label(node), "status": "success", "message": f"Loop configurado para {repeat} passagem(ns)."})
                for _ in range(repeat):
                    queue.append((next_id, next_obj))
                continue

            if node_type == "rule":
                ok, message = _rule_allows(auto, event, current_obj, data)
                if not ok:
                    path.append({"label": _node_label(node), "status": "skipped", "message": message})
                    continue
                path.append({"label": _node_label(node), "status": "success", "message": message})
                queue.append((next_id, next_obj))
                continue

            if node_type == "action":
                action = _action_from_node(data)
                result = _run_single_action(auto, current_obj, action, event)
                if result.get("object") is not None:
                    next_obj = result["object"]
                step = {
                    "label": _action_label(action),
                    "status": result.get("status", "success"),
                    "message": result.get("message", ""),
                }
                if result.get("status") == "scheduled":
                    scheduled_for = result.get("scheduled_for")
                    if scheduled_for is not None:
                        step["scheduled_for"] = scheduled_for.isoformat()
                    step["resume_node_id"] = next_id
                path.append(step)
                if result.get("status") in {"scheduled", "error"}:
                    return result["status"], path
                queue.append((next_id, next_obj))

    return _status_from_path(path), path


def _status_from_path(path):
    if not path:
        return "skipped"
    last_status = path[-1].get("status")
    if last_status in {"error", "scheduled", "skipped"}:
        return last_status
    return "success"


# --------------------------------------------------------------------------- #
# Rich condition engine: field + operator + value, combinable with AND / OR.
# A condition node stores `data.condition = {"match": "all"|"any", "rules": [...]}`
# where each rule is {"field": ..., "op": ..., "value": ...}. Legacy single-field
# conditions (condition_stage / condition_custom_*) are still honoured.
# --------------------------------------------------------------------------- #
CONDITION_OPERATORS = [
    ("eq", "é igual a"),
    ("ne", "é diferente de"),
    ("contains", "contém"),
    ("not_contains", "não contém"),
    ("gt", "maior que"),
    ("lt", "menor que"),
    ("gte", "maior ou igual a"),
    ("lte", "menor ou igual a"),
    ("is_empty", "está vazio"),
    ("is_not_empty", "está preenchido"),
    ("changed_to", "mudou para"),
]
CONDITION_NO_VALUE_OPS = {"is_empty", "is_not_empty"}


def _field_value(obj, event, field):
    if field == "stage":
        return event.payload.get("stage", getattr(obj, "stage", None))
    if field == "stage_kind":
        return getattr(obj, "stage_kind", None)
    if field == "value":
        return getattr(obj, "value", None)
    if field == "score":
        return getattr(obj, "score", None)
    if field == "owner":
        owner = getattr(obj, "owner", None)
        return owner.get_username() if owner else None
    if field == "email":
        return getattr(obj, "email", None)
    if field == "phone":
        return getattr(obj, "phone", None)
    if field == "tag":
        if hasattr(obj, "tags"):
            try:
                return [t.name for t in obj.tags.all()]
            except Exception:
                return []
        return []
    if field.startswith("custom:"):
        custom_key = field.split(":", 1)[1]  # e.g. "deal:priority"
        target = _target_for_custom(obj, custom_key)
        key = custom_key.split(":", 1)[-1]
        if target is None:
            return None
        return (getattr(target, "custom", {}) or {}).get(key)
    return None


def _is_blank(value):
    if value is None:
        return True
    if isinstance(value, (list, tuple, dict, str)):
        return len(value) == 0
    return False


def _apply_op(actual, op, expected):
    if op == "is_empty":
        return _is_blank(actual)
    if op == "is_not_empty":
        return not _is_blank(actual)
    if isinstance(actual, (list, tuple)):
        vals = [str(v).strip().lower() for v in actual]
        exp = str(expected).strip().lower()
        if op in ("eq", "contains"):
            return exp in vals
        if op in ("ne", "not_contains"):
            return exp not in vals
        return False
    if op in ("gt", "lt", "gte", "lte"):
        try:
            a, e = float(actual), float(expected)
        except (TypeError, ValueError):
            return False
        return {"gt": a > e, "lt": a < e, "gte": a >= e, "lte": a <= e}[op]
    a = ("" if actual is None else str(actual)).strip().lower()
    e = ("" if expected is None else str(expected)).strip().lower()
    if op == "ne":
        return a != e
    if op == "contains":
        return e in a
    if op == "not_contains":
        return e not in a
    return a == e  # default: eq


def _eval_rule(auto, event, obj, rule):
    field = rule.get("field") or ""
    op = rule.get("op") or "eq"
    expected = rule.get("value", "")
    if op == "changed_to":
        payload_key = "stage" if field in ("stage", "stage_kind") else field
        return str(event.payload.get(payload_key, "") or "").strip().lower() == str(expected).strip().lower()
    return _apply_op(_field_value(obj, event, field), op, expected)


def _eval_conditions(auto, event, obj, condition):
    rules = [r for r in (condition.get("rules") or []) if r.get("field")]
    if not rules:
        return True
    results = [_eval_rule(auto, event, obj, r) for r in rules]
    return any(results) if condition.get("match") == "any" else all(results)


def _condition_matches(auto, event, obj, data):
    condition = data.get("condition")
    if isinstance(condition, dict) and condition.get("rules"):
        return _eval_conditions(auto, event, obj, condition)
    # --- Legacy single-field conditions (stage + one custom field, equality) ---
    stage = data.get("condition_stage")
    if stage and event.payload.get("stage") != stage and getattr(obj, "stage", None) != stage:
        return False
    custom_key = data.get("condition_custom_key")
    if custom_key:
        target = _target_for_custom(obj, custom_key)
        key = custom_key.split(":", 1)[-1]
        expected = str(data.get("condition_custom_value", "")).strip()
        actual = "" if target is None else str((getattr(target, "custom", {}) or {}).get(key, ""))
        if expected and actual != expected:
            return False
    return True


def _rule_allows(auto, event, obj, data):
    rule_type = data.get("rule_type") or "once_per_record"
    model = event.payload.get("object_model", "")
    object_id = event.payload.get("object_id")
    runs = AutomationRun.objects.filter(
        workspace=auto.workspace,
        automation=auto,
        object_model=model,
        object_id=object_id,
    )
    if rule_type == "once_per_record":
        if runs.exists():
            return False, "Registro ja passou por esta automacao."
        return True, "Primeira entrada permitida."
    if rule_type == "cooldown_days":
        days = _int_or_default(data.get("rule_days"), 0)
        if days and runs.filter(created_at__gte=timezone.now() - timedelta(days=days)).exists():
            return False, f"Registro entrou nesta automacao nos ultimos {days} dia(s)."
        return True, "Intervalo permitido."
    if rule_type == "stop_if_open_deal":
        from modules.crm.models import Contact, Deal
        contact = obj if isinstance(obj, Contact) else getattr(obj, "contact", None)
        if contact and Deal.all_objects.filter(workspace=auto.workspace, contact=contact, stage_kind="open").exists():
            return False, "Contato ja possui negocio aberto."
        return True, "Sem negocio aberto bloqueando."
    return True, "Regra permitida."


def _run_action(auto, obj):
    path = []
    current = obj
    for action in auto.normalized_actions():
        result = _run_single_action(auto, current, action)
        if result.get("object") is not None:
            current = result["object"]
        path.append({
            "label": _action_label(action),
            "status": result.get("status", "success"),
            "message": result.get("message", ""),
        })
        if result.get("status") in {"scheduled", "error"}:
            break
    return path


def _run_single_action(auto, obj, action, event=None):
    action_type = action.get("type")
    if action_type in {"create_task", "create_note"}:
        _create_activity(auto, obj, action)
        return {"status": "success", "message": "Atividade criada."}
    if action_type == "create_contact":
        contact = _create_contact(auto, obj, action, event)
        return {"status": "success", "message": "Contato criado.", "object": _merge_contexts([obj, contact])}
    if action_type == "create_company":
        company = _create_company(auto, obj, action, event)
        return {"status": "success", "message": "Empresa criada.", "object": _merge_contexts([obj, company])}
    if action_type == "create_deal":
        return {"status": "success", "message": "Negocio criado.", "object": _create_deal(auto, obj, action, event)}
    if action_type == "move_deal":
        deal = _move_deal(auto, obj, action)
        return {"status": "success" if deal else "skipped", "message": "Negocio movido." if deal else "Nenhum negocio encontrado.", "object": deal}
    if action_type == "set_contact_stage":
        _set_contact_stage(obj, action)
        return {"status": "success", "message": "Contato atualizado."}
    if action_type == "set_custom_field":
        return _set_custom_field(auto, obj, action)
    if action_type == "delay":
        scheduled_for, message = _delay_schedule(action)
        return {
            "status": "scheduled",
            "message": message,
            "scheduled_for": scheduled_for,
        }
    if action_type == "send_webhook":
        return _send_webhook(auto, obj, action, event)
    if action_type == "http_request":
        return _send_http_request(auto, obj, action, event)
    return {"status": "skipped", "message": "Acao sem implementacao."}


def _action_from_node(data):
    return {
        "type": data.get("action_type"),
        "text": data.get("text", ""),
        "pipeline": data.get("pipeline", ""),
        "deal_stage": data.get("deal_stage", ""),
        "contact_stage": data.get("contact_stage", ""),
        "contact_first_name": data.get("contact_first_name", ""),
        "contact_last_name": data.get("contact_last_name", ""),
        "contact_email": data.get("contact_email", ""),
        "contact_phone": data.get("contact_phone", ""),
        "contact_job_title": data.get("contact_job_title", ""),
        "company_name": data.get("company_name", ""),
        "company_domain": data.get("company_domain", ""),
        "company_industry": data.get("company_industry", ""),
        "company_city": data.get("company_city", ""),
        "custom_key": data.get("custom_key", ""),
        "custom_value": data.get("custom_value", ""),
        "delay_amount": data.get("delay_amount", 0),
        "delay_unit": data.get("delay_unit", "minutes"),
        "delay_until": data.get("delay_until", ""),
        "delay_minutes": data.get("delay_minutes", 0),
        "webhook_url": data.get("webhook_url", ""),
        "http_method": data.get("http_method", "POST"),
        "http_url": data.get("http_url", ""),
        "http_auth_type": data.get("http_auth_type", "none"),
        "http_auth_token": data.get("http_auth_token", ""),
        "http_username": data.get("http_username", ""),
        "http_password": data.get("http_password", ""),
        "http_send_query": data.get("http_send_query", ""),
        "http_query": data.get("http_query", ""),
        "http_send_headers": data.get("http_send_headers", ""),
        "http_headers": data.get("http_headers", ""),
        "http_content_type": data.get("http_content_type", "json"),
        "http_send_body": data.get("http_send_body", ""),
        "http_body": data.get("http_body", ""),
        "http_timeout": data.get("http_timeout", 10),
        "due_days": data.get("due_days", 0),
    }


def _action_label(action):
    labels = dict(Automation.ACTION_CHOICES)
    if action.get("type") == "delay":
        return "Aguardar"
    if action.get("type") == "http_request":
        return f"HTTP {action.get('http_method') or 'POST'}"
    return labels.get(action.get("type"), action.get("type") or "Acao")


def _node_label(node):
    return {
        "filter": "Filtro",
        "condition": "If / condicao",
        "switch": "Roteador",
        "merge": "Mesclar",
        "loop": "Loop",
        "rule": "Regra",
    }.get(node.get("type"), "Node")


def _pipeline_for(auto, action):
    from modules.crm.models import Pipeline

    pipeline_id = action.get("pipeline")
    if str(pipeline_id).isdigit():
        pipe = Pipeline.objects.filter(pk=int(pipeline_id), workspace=auto.workspace).first()
        if pipe:
            return pipe.ensure_stages()
    pipe = Pipeline.objects.filter(workspace=auto.workspace, is_default=True).first()
    if pipe is None:
        pipe = Pipeline.objects.filter(workspace=auto.workspace).first()
    return pipe.ensure_stages() if pipe else None


def _stage_for(pipeline, stage_key):
    if not pipeline:
        return None
    stage = pipeline.stages.filter(key=stage_key).first()
    return stage or pipeline.stages.first()


def _context_for(obj):
    from modules.crm.models import Company, Contact, Deal

    if isinstance(obj, dict):
        return dict(obj)
    context = {"object": obj}
    if isinstance(obj, Deal):
        context.update({"deal": obj, "contact": obj.contact, "company": obj.company})
    elif isinstance(obj, Contact):
        context.update({"contact": obj, "company": obj.company})
    elif isinstance(obj, Company):
        context.update({"company": obj})
    return context


def _merge_contexts(items):
    merged = {}
    for item in items:
        context = _context_for(item)
        for key in ("company", "contact", "deal", "object"):
            if context.get(key) is not None:
                merged[key] = context[key]
    contact = merged.get("contact")
    company = merged.get("company")
    if contact is not None and company is not None and not getattr(contact, "company_id", None):
        contact.company = company
        contact.save(update_fields=["company", "updated_at"])
    return merged


def _context_object(obj):
    if isinstance(obj, dict):
        return obj.get("deal") or obj.get("contact") or obj.get("company") or obj.get("object")
    return obj


def _create_activity(auto, obj, action):
    from modules.crm.models import Activity, Company, Contact, Deal

    obj = _context_object(obj)
    kind = "task" if action.get("type") == "create_task" else "note"
    due_days = _int_or_default(action.get("due_days"), auto.action_due_days)
    due = timezone.localdate() + timedelta(days=due_days) if kind == "task" else None
    kwargs = {
        "workspace": auto.workspace,
        "kind": kind,
        "source": "automation",
        "body": action.get("text") or auto.action_text or "Acao criada pela automacao.",
        "due_date": due,
    }
    if isinstance(obj, Deal):
        kwargs["deal"] = obj
    elif isinstance(obj, Company):
        kwargs["company"] = obj
    elif isinstance(obj, Contact):
        kwargs["contact"] = obj
    Activity.objects.create(**kwargs)


def _incoming(event):
    """Raw data that arrived with the event (e.g. a webhook / lead-form body)."""
    if not event:
        return {}
    body = (event.payload or {}).get("body")
    return body if isinstance(body, dict) else {}


def _pick(data, *keys):
    """First non-empty value among the given keys (case-insensitive)."""
    if not isinstance(data, dict):
        return ""
    lowered = {str(k).strip().lower(): v for k, v in data.items()}
    for key in keys:
        value = lowered.get(key)
        if value not in (None, "", []):
            return str(value).strip()
    return ""


# Lead fields already consumed by the standard mapping (name/email/phone/...),
# so they are NOT re-saved as "extra" custom fields.
_STANDARD_LEAD_KEYS = {
    "company": {
        "company", "company_name", "empresa", "organization", "organizacao",
        "domain", "website", "site", "url", "industry", "segmento", "setor",
        "city", "cidade",
    },
    "contact": {
        "first_name", "firstname", "nome", "primeiro_nome",
        "last_name", "lastname", "surname", "sobrenome",
        "name", "full_name", "fullname", "nome_completo",
        "email", "e_mail", "email_address",
        "phone", "phone_number", "telefone", "celular", "whatsapp",
        "job_title", "cargo", "company", "company_name", "empresa",
    },
    "deal": {"title", "titulo", "deal", "negocio", "interesse", "produto"},
}


def _norm_key(name):
    """Canonical token for matching lead fields to CRM fields (accent-insensitive)."""
    return slugify(str(name)).replace("-", "_")


def _coerce_custom_value(field, value):
    """Coerce a raw lead value to the CRM custom field's type."""
    ftype = getattr(field, "field_type", "text") if field else "text"
    if ftype == "checkbox":
        return str(value).strip().lower() in ("1", "true", "sim", "yes", "on", "verdadeiro", "checked")
    if ftype == "number":
        try:
            n = float(str(value).replace(",", "."))
            return int(n) if n.is_integer() else n
        except (TypeError, ValueError):
            return str(value).strip()
    if ftype == "multiselect":
        if isinstance(value, list):
            return [str(v).strip() for v in value if str(v).strip()]
        return [p.strip() for p in re.split(r"[;,]", str(value)) if p.strip()]
    return str(value).strip()


def _apply_lead_custom_fields(workspace, obj, object_type, data):
    """Save the lead's extra fields into obj.custom so nothing from the form is lost.

    1. Fields matching a workspace CustomField (by key or label) fill that field,
       typed to the field's kind — so they render nicely in the CRM.
    2. Any remaining answers are kept under their normalized name (raw), so no
       data is ever dropped even before the gestor defines a matching field.
    """
    from core.models import CustomField

    if obj is None or not isinstance(data, dict) or not data:
        return
    incoming = {}
    for key, value in data.items():
        nk = _norm_key(key)
        if nk and nk not in incoming:
            incoming[nk] = value
    if not incoming:
        return
    custom = dict(obj.custom or {})
    consumed = set(_STANDARD_LEAD_KEYS.get(object_type, set()))
    for field in CustomField.objects.filter(workspace=workspace, object_type=object_type):
        for cand in {_norm_key(field.key), _norm_key(field.label)}:
            if cand in incoming and incoming[cand] not in (None, "", []):
                custom[field.key] = _coerce_custom_value(field, incoming[cand])
                consumed.add(cand)
                break
    for nk, value in incoming.items():
        if nk in consumed or value in (None, "", []):
            continue
        custom.setdefault(nk, value if isinstance(value, (str, int, float, bool, list)) else str(value))
    if custom != (obj.custom or {}):
        obj.custom = custom
        obj.save(update_fields=["custom", "updated_at"])


def _create_company(auto, obj, action, event=None):
    from modules.crm.models import Company

    data = _incoming(event)
    name = (action.get("company_name") or _pick(data, "company", "company_name", "empresa", "organization")
            or action.get("text") or "Empresa criada pela automacao").strip()
    domain = (action.get("company_domain") or _pick(data, "domain", "website", "site")).strip()
    defaults = {
        "industry": action.get("company_industry", "") or _pick(data, "industry", "segmento"),
        "city": action.get("company_city", "") or _pick(data, "city", "cidade"),
    }
    if domain:
        company, created = Company.all_objects.get_or_create(
            workspace=auto.workspace,
            domain=domain,
            defaults={"name": name, **defaults},
        )
        if not created:
            updates = []
            for field, value in {"name": name, **defaults}.items():
                if value and getattr(company, field) != value:
                    setattr(company, field, value)
                    updates.append(field)
            if updates:
                updates.append("updated_at")
                company.save(update_fields=updates)
    else:
        company = Company.all_objects.create(workspace=auto.workspace, name=name, **defaults)
    _apply_lead_custom_fields(auto.workspace, company, "company", data)
    return company


def _create_contact(auto, obj, action, event=None):
    from modules.crm.models import Contact

    context = _context_for(obj)
    data = _incoming(event)
    first_name = (action.get("contact_first_name") or _pick(data, "first_name", "firstname", "nome")).strip()
    last_name = (action.get("contact_last_name") or _pick(data, "last_name", "lastname", "surname", "sobrenome")).strip()
    full = _pick(data, "name", "full_name", "fullname", "nome_completo")
    if not first_name and full:
        parts = full.split(" ", 1)
        first_name = parts[0]
        if not last_name and len(parts) > 1:
            last_name = parts[1]
    if not first_name:
        first_name = (action.get("text") or "Contato").strip()
    email = (action.get("contact_email") or _pick(data, "email", "e-mail", "email_address")).strip()
    company = context.get("company")
    defaults = {
        "first_name": first_name,
        "last_name": last_name,
        "phone": action.get("contact_phone", "") or _pick(data, "phone", "phone_number", "telefone", "celular", "whatsapp"),
        "job_title": action.get("contact_job_title", "") or _pick(data, "job_title", "cargo"),
        "stage": action.get("contact_stage") or "lead",
        "company": company,
    }
    if email:
        contact, created = Contact.all_objects.get_or_create(
            workspace=auto.workspace,
            email=email,
            defaults=defaults,
        )
        if not created:
            updates = []
            for field, value in defaults.items():
                if value and getattr(contact, field) != value:
                    setattr(contact, field, value)
                    updates.append(field)
            if updates:
                updates.append("updated_at")
                contact.save(update_fields=updates)
    else:
        contact = Contact.all_objects.create(workspace=auto.workspace, **defaults)
    _apply_lead_custom_fields(auto.workspace, contact, "contact", data)
    return contact


def _create_deal(auto, obj, action, event=None):
    from modules.crm.models import Company, Contact, Deal

    company = None
    contact = None
    context = _context_for(obj)
    if context.get("deal"):
        obj = context["deal"]
    if context.get("contact"):
        contact = context["contact"]
    if context.get("company"):
        company = context["company"]
    if isinstance(obj, Deal):
        company = obj.company
        contact = obj.contact
    elif isinstance(obj, Contact):
        contact = obj
        company = obj.company
    elif isinstance(obj, Company):
        company = obj

    data = _incoming(event)
    pipeline = _pipeline_for(auto, action)
    stage_obj = _stage_for(pipeline, action.get("deal_stage") or action.get("stage") or "novo")
    stage = stage_obj.key if stage_obj else (action.get("deal_stage") or action.get("stage") or "novo")
    title = (action.get("text") or _pick(data, "title", "deal", "negocio", "interesse", "produto")
             or _deal_title_for(contact or company or obj))
    order = Deal.all_objects.filter(workspace=auto.workspace, pipeline=pipeline, stage=stage).count()
    deal = Deal.all_objects.create(
        workspace=auto.workspace,
        title=title,
        pipeline=pipeline,
        stage=stage,
        stage_kind=stage_obj.kind if stage_obj else "open",
        order=order,
        company=company,
        contact=contact,
    )
    _apply_lead_custom_fields(auto.workspace, deal, "deal", data)
    return deal


def _move_deal(auto, obj, action):
    from modules.crm.models import Contact, Deal

    context = _context_for(obj)
    obj = context.get("deal") or context.get("contact") or obj
    deal = obj if isinstance(obj, Deal) else None
    if deal is None and isinstance(obj, Contact):
        deal = (
            Deal.all_objects.filter(workspace=auto.workspace, contact=obj)
            .order_by("-created_at")
            .first()
        )
    if deal is None:
        return None

    pipeline = _pipeline_for(auto, action) or deal.pipeline
    stage_obj = _stage_for(pipeline, action.get("deal_stage") or action.get("stage") or deal.stage)
    if pipeline:
        deal.pipeline = pipeline
    if stage_obj:
        deal.stage = stage_obj.key
        deal.stage_kind = stage_obj.kind
    else:
        deal.stage = action.get("deal_stage") or action.get("stage") or deal.stage
        deal.sync_stage_kind()
    deal.order = Deal.all_objects.filter(workspace=auto.workspace, pipeline=deal.pipeline, stage=deal.stage).count()
    deal.save(update_fields=["pipeline", "stage", "stage_kind", "order", "updated_at"])
    return deal


def _set_contact_stage(obj, action):
    from modules.crm.models import Contact, Deal

    context = _context_for(obj)
    obj = context.get("contact") or context.get("deal") or obj
    contact = obj if isinstance(obj, Contact) else None
    if contact is None and isinstance(obj, Deal):
        contact = obj.contact
    stage = action.get("contact_stage") or action.get("stage")
    if contact is not None and stage and contact.stage != stage:
        contact.stage = stage
        contact.save(update_fields=["stage", "updated_at"])


def _target_for_custom(obj, custom_key):
    from modules.crm.models import Company, Contact, Deal

    context = _context_for(obj)
    object_type = custom_key.split(":", 1)[0] if ":" in custom_key else ""
    if object_type == "deal":
        if context.get("deal"):
            return context["deal"]
        if isinstance(obj, Deal):
            return obj
        return None
    if object_type == "contact":
        if context.get("contact"):
            return context["contact"]
        if isinstance(obj, Contact):
            return obj
        if isinstance(obj, Deal):
            return obj.contact
        return None
    if object_type == "company":
        if context.get("company"):
            return context["company"]
        if isinstance(obj, Company):
            return obj
        return getattr(obj, "company", None)
    return obj


def _set_custom_field(auto, obj, action):
    custom_key = action.get("custom_key", "")
    if not custom_key:
        return {"status": "skipped", "message": "Campo personalizado nao informado."}
    target = _target_for_custom(obj, custom_key)
    if target is None:
        return {"status": "skipped", "message": "Registro alvo nao encontrado."}
    key = custom_key.split(":", 1)[-1]
    target.custom = dict(target.custom or {})
    target.custom[key] = action.get("custom_value", "")
    target.save(update_fields=["custom", "updated_at"])
    return {"status": "success", "message": "Campo atualizado."}


def _send_webhook(auto, obj, action, event=None):
    return _send_http_request(auto, obj, {
        "type": "http_request",
        "http_method": "POST",
        "http_url": action.get("webhook_url", ""),
        "http_headers": "{}",
        "http_body": "",
        "http_send_body": "1",
        "http_timeout": 5,
    }, event)


def _send_http_request(auto, obj, action, event=None):
    method = str(action.get("http_method") or "POST").upper()
    if method not in {"GET", "POST", "PUT", "PATCH", "DELETE"}:
        method = "POST"
    url = (action.get("http_url") or action.get("webhook_url") or "").strip()
    if not url.startswith(("http://", "https://")):
        return {"status": "skipped", "message": "HTTP request sem URL valida."}

    raw_query = (action.get("http_query") or "").strip()
    send_query = _truthy(action.get("http_send_query")) or (raw_query and "http_send_query" not in action)
    if send_query and raw_query:
        try:
            parsed_query = json.loads(raw_query)
        except json.JSONDecodeError:
            return {"status": "error", "message": "Query params do HTTP request precisam ser JSON valido."}
        if isinstance(parsed_query, dict):
            separator = "&" if urlparse.urlsplit(url).query else "?"
            url = url + separator + urlparse.urlencode(parsed_query, doseq=True)

    content_type = action.get("http_content_type") or "json"
    headers = {"Content-Type": "application/json" if content_type == "json" else "text/plain"}
    raw_headers = (action.get("http_headers") or "").strip()
    send_headers = _truthy(action.get("http_send_headers")) or (raw_headers and "http_send_headers" not in action)
    if send_headers and raw_headers:
        try:
            parsed_headers = json.loads(raw_headers)
        except json.JSONDecodeError:
            return {"status": "error", "message": "Headers do HTTP request precisam ser JSON valido."}
        if isinstance(parsed_headers, dict):
            headers.update({str(k): str(v) for k, v in parsed_headers.items()})

    auth_type = action.get("http_auth_type") or "none"
    if auth_type == "bearer" and action.get("http_auth_token"):
        headers["Authorization"] = f"Bearer {action.get('http_auth_token')}"
    elif auth_type == "basic" and (action.get("http_username") or action.get("http_password")):
        raw_auth = f"{action.get('http_username', '')}:{action.get('http_password', '')}"
        encoded = base64.b64encode(raw_auth.encode("utf-8")).decode("ascii")
        headers["Authorization"] = f"Basic {encoded}"

    payload = {
        "automation_id": auto.pk,
        "automation": auto.name,
        "event": event.event_type if event else "",
        "event_payload": event.payload if event else {},
        "object": str(obj) if obj is not None else "",
        "object_id": getattr(obj, "pk", None),
        "object_model": obj.__class__.__name__.lower() if obj is not None else "",
    }

    body = None
    raw_body = (action.get("http_body") or "").strip()
    send_body = _truthy(action.get("http_send_body")) or (raw_body and "http_send_body" not in action)
    if send_body and raw_body:
        if content_type == "json":
            try:
                body = json.dumps(json.loads(raw_body)).encode("utf-8")
            except json.JSONDecodeError:
                return {"status": "error", "message": "Body JSON do HTTP request precisa ser valido."}
        else:
            body = raw_body.encode("utf-8")
    elif send_body and method in {"POST", "PUT", "PATCH"}:
        body = json.dumps(payload).encode("utf-8")

    timeout = _int_or_default(action.get("http_timeout"), 10) or 10
    req = urlrequest.Request(url, data=body, headers=headers, method=method)
    try:
        with urlrequest.urlopen(req, timeout=timeout) as resp:
            return {"status": "success", "message": f"HTTP {method} enviado ({resp.status})."}
    except (URLError, TimeoutError, ValueError) as exc:
        return {"status": "error", "message": f"HTTP request falhou: {exc}"[:300]}


def _delay_schedule(action):
    raw_until = str(action.get("delay_until") or "").strip()
    if raw_until:
        scheduled_for = parse_datetime(raw_until)
        if scheduled_for is not None:
            if timezone.is_naive(scheduled_for):
                scheduled_for = timezone.make_aware(scheduled_for, timezone.get_current_timezone())
            if scheduled_for < timezone.now():
                scheduled_for = timezone.now()
            return scheduled_for, f"Fluxo aguardando ate {timezone.localtime(scheduled_for):%d/%m/%Y %H:%M}."

    amount = _int_or_default(action.get("delay_amount") or action.get("delay_minutes"), 0)
    unit = action.get("delay_unit") or "minutes"
    multiplier = {"minutes": 1, "hours": 60, "days": 1440}.get(unit, 1)
    scheduled_for = timezone.now() + timedelta(minutes=amount * multiplier)
    unit_label = {"minutes": "minuto(s)", "hours": "hora(s)", "days": "dia(s)"}.get(unit, "minuto(s)")
    return scheduled_for, f"Fluxo aguardando {amount} {unit_label}."


def _truthy(value):
    return str(value).lower() in {"1", "true", "on", "yes"}


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
