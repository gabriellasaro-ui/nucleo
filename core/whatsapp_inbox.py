import hashlib
import json
import re
from datetime import datetime

from django.db import transaction
from django.db.models import Q
from django.utils import timezone
from django.utils.dateparse import parse_datetime

from modules.crm.models import Contact, WhatsAppConversation, WhatsAppMessage


def _value(mapping, *names, default=None):
    if not isinstance(mapping, dict):
        return default
    lowered = {str(key).lower(): value for key, value in mapping.items()}
    for name in names:
        if name in mapping:
            return mapping[name]
        if name.lower() in lowered:
            return lowered[name.lower()]
    return default


def normalize_whatsapp_phone(value):
    value = str(value or "").split("@", 1)[0].split(":", 1)[0]
    return re.sub(r"\D", "", value)


def _phone_variants(value):
    digits = normalize_whatsapp_phone(value)
    variants = {digits} if digits else set()
    if len(digits) in {10, 11}:
        variants.add("55" + digits)
    if digits.startswith("55") and len(digits) in {12, 13}:
        variants.add(digits[2:])
    return variants


def find_contact_by_phone(phone):
    wanted = _phone_variants(phone)
    if not wanted:
        return None
    for contact in Contact.objects.exclude(phone="").iterator():
        if wanted & _phone_variants(contact.phone):
            return contact
    return None


def _split_contact_name(name, phone):
    parts = str(name or "").strip().split()
    if not parts:
        return f"WhatsApp {phone[-4:]}", ""
    return parts[0][:120], " ".join(parts[1:])[:120]


def _jid_string(info, *names):
    value = _value(info, *names, default="")
    if isinstance(value, dict):
        user = str(_value(value, "User", "user", default="") or "")
        server = str(_value(value, "Server", "server", default="") or "")
        return f"{user}@{server}" if user and server else user
    return str(value or "")


def _conversation_jid(info, is_from_me):
    chat = _jid_string(info, "Chat", "chat")
    if chat.endswith("@s.whatsapp.net"):
        return chat

    alternate_names = (
        ("RecipientAlt", "recipientAlt", "RemoteJIDAlt", "remoteJidAlt")
        if is_from_me
        else ("Sender", "sender", "SenderAlt", "senderAlt", "RemoteJIDAlt", "remoteJidAlt")
    )
    for name in alternate_names:
        candidate = _jid_string(info, name)
        if candidate.endswith("@s.whatsapp.net"):
            return candidate

    # A LID is an internal WhatsApp identifier, not a customer phone number.
    # Without its phone-number alternate it cannot safely identify a CRM person.
    if chat.endswith("@lid"):
        return ""
    return chat


@transaction.atomic
def promote_whatsapp_conversation(workspace, conversation):
    conversation = (
        WhatsAppConversation.objects.select_for_update()
        .get(workspace=workspace, pk=conversation.pk)
    )
    contact = conversation.contact or find_contact_by_phone(conversation.phone)
    created = False
    if not contact:
        first_name, last_name = _split_contact_name(
            conversation.name, conversation.phone,
        )
        contact = Contact.objects.create(
            workspace=workspace,
            first_name=first_name,
            last_name=last_name,
            phone="+" + conversation.phone,
            stage="lead",
        )
        created = True

    WhatsAppConversation.objects.filter(
        workspace=workspace,
        instance_id=conversation.instance_id,
        phone=conversation.phone,
    ).update(contact=contact, name=contact.full_name)
    return contact, created


@transaction.atomic
def sync_whatsapp_contact_directory(workspace, instance_id, rows):
    names_by_phone = {}
    for row in rows:
        jid = str(_value(row, "Jid", "jid", default="") or "")
        if any(suffix in jid for suffix in ("@g.us", "@broadcast", "@newsletter")):
            continue
        phone = normalize_whatsapp_phone(jid)
        name = ""
        for field in ("FullName", "BusinessName", "PushName", "FirstName"):
            candidate = str(_value(row, field, default="") or "").strip()
            if candidate:
                name = candidate
                break
        if phone and name:
            names_by_phone[phone] = name

    conversation_updates = 0
    contact_updates = 0
    conversations = WhatsAppConversation.objects.filter(
        workspace=workspace,
        instance_id=instance_id,
    ).select_related("contact")
    for conversation in conversations:
        name = names_by_phone.get(normalize_whatsapp_phone(conversation.phone))
        if not name:
            continue
        if conversation.name != name:
            conversation.name = name[:160]
            conversation.save(update_fields=["name", "updated_at"])
            conversation_updates += 1

        contact = conversation.contact
        if not contact or contact.last_name or not contact.first_name.startswith("WhatsApp "):
            continue
        first_name, last_name = _split_contact_name(name, conversation.phone)
        contact.first_name = first_name
        contact.last_name = last_name
        contact.save(update_fields=["first_name", "last_name", "updated_at"])
        contact_updates += 1

    return {
        "directory_count": len(names_by_phone),
        "conversation_updates": conversation_updates,
        "contact_updates": contact_updates,
    }


def _message_content(message):
    if not isinstance(message, dict):
        return "Mensagem", "unknown", ""
    direct = _value(message, "conversation")
    if direct:
        return str(direct), "text", ""

    candidates = [
        ("extendedTextMessage", "text", "text"),
        ("imageMessage", "caption", "image"),
        ("videoMessage", "caption", "video"),
        ("documentMessage", "caption", "document"),
        ("audioMessage", "", "audio"),
        ("stickerMessage", "", "sticker"),
        ("contactMessage", "displayName", "contact"),
        ("locationMessage", "name", "location"),
        ("buttonsResponseMessage", "selectedDisplayText", "button"),
        ("listResponseMessage", "title", "button"),
    ]
    labels = {
        "image": "Imagem",
        "video": "Vídeo",
        "document": "Documento",
        "audio": "Áudio",
        "sticker": "Figurinha",
        "contact": "Contato",
        "location": "Localização",
        "button": "Resposta interativa",
    }
    for key, text_key, kind in candidates:
        content = _value(message, key)
        if not isinstance(content, dict):
            continue
        text = _value(content, text_key) if text_key else ""
        filename = _value(content, "fileName", "filename")
        text = str(text or filename or labels.get(kind, "Mensagem"))
        media_url = str(_value(content, "mediaUrl", default="") or "")
        return text, kind, media_url
    return "Mensagem", "unknown", ""


def _message_datetime(info):
    raw = _value(info, "Timestamp", "timestamp")
    if isinstance(raw, (int, float)):
        return datetime.fromtimestamp(raw, tz=timezone.get_current_timezone())
    if raw:
        if str(raw).isdigit():
            return datetime.fromtimestamp(
                int(raw), tz=timezone.get_current_timezone(),
            )
        parsed = parse_datetime(str(raw))
        if parsed:
            return parsed if timezone.is_aware(parsed) else timezone.make_aware(parsed)
    return timezone.now()


def _safe_message_raw(event_name, info, message, kind):
    return {
        "event": event_name,
        "info": {
            "id": _value(info, "ID", "id", default=""),
            "chat": _value(info, "Chat", "chat", default=""),
            "sender": _value(info, "Sender", "sender", default=""),
            "push_name": _value(info, "PushName", "pushName", default=""),
            "from_me": bool(_value(info, "IsFromMe", "isFromMe", default=False)),
        },
        "message_type": kind,
        "keys": list(message.keys())[:20] if isinstance(message, dict) else [],
    }


def _provider_message_id(payload, info, instance_id, remote_jid):
    provider_id = str(_value(info, "ID", "id", default="") or "")
    if provider_id:
        return provider_id[:180]
    fingerprint = json.dumps(payload, sort_keys=True, default=str).encode("utf-8")
    digest = hashlib.sha256(fingerprint).hexdigest()[:40]
    return f"fallback-{instance_id}-{remote_jid}-{digest}"[:180]


@transaction.atomic
def ingest_whatsapp_event(workspace, connection, payload):
    event_name = str(_value(payload, "event", default="") or "")
    event_key = event_name.lower()
    data = _value(payload, "data", default={}) or {}
    config = connection.config or {}

    if event_key in {"connected", "pairsuccess"}:
        connection.status = "connected"
        connection.config = {
            **config,
            "jid": str(_value(data, "jid", default="") or config.get("jid", "")),
            "display_name": str(
                _value(data, "pushName", "name", default="")
                or config.get("display_name", "WhatsApp")
            ),
        }
        connection.save(update_fields=["status", "config", "updated_at"])
        return None
    if event_key in {"disconnected", "loggedout", "connectfailure", "qrtimeout"}:
        connection.status = "disconnected"
        connection.save(update_fields=["status", "updated_at"])
        return None
    if event_key == "receipt":
        ids = _value(data, "MessageIDs", "messageIDs", "IDs", "ids", default=[])
        if isinstance(ids, str):
            ids = [ids]
        WhatsAppMessage.objects.filter(
            workspace=workspace,
            instance_id=config.get("instance_id", ""),
            provider_message_id__in=ids or [],
        ).update(status="read")
        return None
    if event_key == "picture":
        jid = str(_value(data, "JID", "jid", default="") or "")
        phone = normalize_whatsapp_phone(jid)
        if phone:
            WhatsAppConversation.objects.filter(
                workspace=workspace,
                instance_id=config.get("instance_id", ""),
                phone=phone,
            ).update(avatar_url="")
        return None
    if event_key == "historysync":
        # The CRM inbox starts clean. Old phone history must not look like
        # newly arrived commercial conversations.
        return None
    if event_key not in {"message", "sendmessage"}:
        return None

    info = _value(data, "Info", "info", default={}) or {}
    message = _value(data, "Message", "message", default={}) or {}
    is_from_me = bool(_value(info, "IsFromMe", "isFromMe", default=False))
    remote_jid = _conversation_jid(info, is_from_me)
    if not remote_jid or any(
        suffix in remote_jid for suffix in ("@g.us", "@broadcast", "@newsletter")
    ):
        return None

    phone = normalize_whatsapp_phone(remote_jid)
    if not phone:
        return None
    instance_id = str(
        _value(payload, "instanceId", default="")
        or config.get("instance_id", "")
    )
    direction = "outgoing" if is_from_me or event_key == "sendmessage" else "incoming"
    push_name = str(
        _value(info, "PushName", "pushName", default="")
        or _value(data, "PushName", "pushName", default="")
    ).strip()

    matches = list(
        WhatsAppConversation.objects.select_for_update()
        .filter(workspace=workspace, instance_id=instance_id)
        .filter(Q(remote_jid=remote_jid) | Q(phone=phone))
        .order_by("-contact_id", "id")
    )
    conversation = next(
        (item for item in matches if item.contact_id),
        matches[0] if matches else None,
    )
    if conversation and len(matches) > 1:
        for duplicate in (item for item in matches if item.pk != conversation.pk):
            duplicate.messages.update(conversation=conversation)
            conversation.unread_count += duplicate.unread_count
            if not conversation.avatar_url and duplicate.avatar_url:
                conversation.avatar_url = duplicate.avatar_url
            duplicate.delete()

    contact = conversation.contact if conversation and conversation.contact_id else None
    if not contact:
        contact = find_contact_by_phone(phone)

    if not conversation:
        conversation = WhatsAppConversation.objects.create(
            workspace=workspace,
            contact=contact,
            instance_id=instance_id,
            remote_jid=remote_jid,
            phone=phone,
            name=contact.full_name if contact else push_name,
            is_history_import=False,
        )
    else:
        changed = []
        if contact and conversation.contact_id != contact.pk:
            conversation.contact = contact
            changed.append("contact")
        display_name = contact.full_name if contact else push_name
        if display_name and conversation.name != display_name:
            conversation.name = display_name
            changed.append("name")
        if conversation.is_history_import:
            conversation.is_history_import = False
            changed.append("is_history_import")
        if changed:
            conversation.save(update_fields=[*changed, "updated_at"])

    text, message_type, media_url = _message_content(message)
    provider_id = _provider_message_id(payload, info, instance_id, remote_jid)
    sent_at = _message_datetime(info)
    message_obj, created = WhatsAppMessage.objects.get_or_create(
        workspace=workspace,
        instance_id=instance_id,
        provider_message_id=provider_id,
        defaults={
            "conversation": conversation,
            "direction": direction,
            "message_type": message_type,
            "text": text,
            "media_url": media_url,
            "status": "sent" if direction == "outgoing" else "received",
            "sent_at": sent_at,
            "raw": _safe_message_raw(event_name, info, message, message_type),
        },
    )
    if not created:
        return message_obj

    conversation.last_message = text[:300]
    conversation.last_message_at = sent_at
    if direction == "incoming":
        conversation.unread_count += 1
    conversation.save(update_fields=[
        "last_message", "last_message_at", "unread_count", "avatar_url", "updated_at",
    ])
    return message_obj


def conversation_for_contact(workspace, instance_id, contact):
    phone = normalize_whatsapp_phone(contact.phone)
    if not phone:
        return None
    conversation = WhatsAppConversation.objects.filter(
        workspace=workspace,
        instance_id=instance_id,
        contact=contact,
    ).first()
    if conversation:
        return conversation
    remote_jid = f"{phone}@s.whatsapp.net"
    return WhatsAppConversation.objects.create(
        workspace=workspace,
        contact=contact,
        instance_id=instance_id,
        remote_jid=remote_jid,
        phone=phone,
        name=contact.full_name,
    )


def record_outgoing_message(workspace, conversation, provider_id, text, raw=None):
    sent_at = timezone.now()
    message, created = WhatsAppMessage.objects.get_or_create(
        workspace=workspace,
        instance_id=conversation.instance_id,
        provider_message_id=provider_id[:180],
        defaults={
            "conversation": conversation,
            "direction": "outgoing",
            "message_type": "text",
            "text": text,
            "status": "sent",
            "sent_at": sent_at,
            "raw": raw or {},
        },
    )
    if created:
        conversation.last_message = text[:300]
        conversation.last_message_at = sent_at
        conversation.save(update_fields=["last_message", "last_message_at", "updated_at"])
    return message
