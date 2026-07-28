"""Instagram (and future Messenger) direct-message inbox: ingest incoming
webhook events and record outgoing replies. Mirrors the WhatsApp inbox, but on
the SocialConversation/SocialMessage models so the two never interfere.
"""
import logging
from datetime import datetime, timezone as _tz

from django.db.models import Sum
from django.utils import timezone

log = logging.getLogger("nucleo.social")


def social_unread_count(workspace, channel="instagram"):
    """Total unread messages for the nav badge. Best-effort (never raises)."""
    if workspace is None:
        return 0
    from modules.crm.models import SocialConversation
    try:
        agg = (SocialConversation.all_objects
               .filter(workspace=workspace, channel=channel, status="open")
               .aggregate(n=Sum("unread_count")))
        return int(agg["n"] or 0)
    except Exception:
        return 0


def _ts_to_dt(ms):
    try:
        return datetime.fromtimestamp(int(ms) / 1000, tz=_tz.utc)
    except (TypeError, ValueError, OSError):
        return timezone.now()


def _ig_profile(sender_id, page_token):
    """Best-effort sender profile (username/name/avatar) via the Graph API."""
    if not page_token or not sender_id:
        return {}
    try:
        from core.views import _fb_graph
        data = _fb_graph(str(sender_id), {"fields": "name,username,profile_pic",
                                          "access_token": page_token}) or {}
        return {"name": data.get("name", "") or "",
                "username": data.get("username", "") or "",
                "avatar": data.get("profile_pic", "") or ""}
    except Exception:
        return {}


def ingest_instagram_messaging(ws, connection, entry, channel="instagram"):
    """Turn one webhook `entry` (its `messaging` array) into conversations +
    messages. Returns how many new messages were stored. Idempotent (dedupes by
    the message id)."""
    from modules.crm.models import SocialConversation, SocialMessage
    cfg = connection.config or {}
    account_id = str(cfg.get("ig_account_id") or entry.get("id") or "")
    page_token = cfg.get("page_access_token", "")
    stored = 0
    for m in entry.get("messaging", []) or []:
        if not isinstance(m, dict):
            continue
        msg = m.get("message") or {}
        mid = str(msg.get("mid") or "")
        if not mid:
            continue
        is_echo = bool(msg.get("is_echo"))
        # For an echo (a message WE sent, mirrored back) the other party is the
        # recipient; for an incoming DM it's the sender.
        other = (m.get("recipient") if is_echo else m.get("sender")) or {}
        sender_id = str(other.get("id") or "")
        if not sender_id or sender_id == account_id:
            continue

        text = msg.get("text", "") or ""
        mtype, media_url = "text", ""
        attachments = msg.get("attachments") or []
        if isinstance(attachments, list) and attachments and isinstance(attachments[0], dict):
            att = attachments[0]
            mtype = att.get("type") or "media"
            payload = att.get("payload") or {}
            media_url = payload.get("url", "") if isinstance(payload, dict) else ""
        sent_at = _ts_to_dt(m.get("timestamp"))

        conv, created = SocialConversation.all_objects.get_or_create(
            workspace=ws, channel=channel, account_id=account_id, sender_id=sender_id,
            defaults={"last_message": text[:300], "last_message_at": sent_at},
        )
        if created and not conv.username:
            prof = _ig_profile(sender_id, page_token)
            if prof:
                conv.username = prof.get("username", "")[:160]
                conv.name = prof.get("name", "")[:160]
                conv.avatar_url = prof.get("avatar", "")[:700]

        _, msg_created = SocialMessage.all_objects.get_or_create(
            workspace=ws, channel=channel, provider_message_id=mid,
            defaults={
                "conversation": conv,
                "direction": "outgoing" if is_echo else "incoming",
                "message_type": mtype, "text": text, "media_url": media_url,
                "sent_at": sent_at, "raw": m,
            },
        )
        if not msg_created:
            continue
        stored += 1
        conv.last_message = (text or mtype)[:300]
        conv.last_message_at = sent_at
        if not is_echo:
            conv.unread_count = (conv.unread_count or 0) + 1
        conv.save()
    return stored


def record_outgoing_social(ws, conversation, provider_message_id, text, channel="instagram", raw=None):
    """Store a reply we just sent, and bump the conversation's last message."""
    from modules.crm.models import SocialMessage
    now = timezone.now()
    msg, _ = SocialMessage.all_objects.get_or_create(
        workspace=ws, channel=channel, provider_message_id=provider_message_id,
        defaults={
            "conversation": conversation, "direction": "outgoing",
            "message_type": "text", "text": text, "sent_at": now, "raw": raw or {},
        },
    )
    conversation.last_message = text[:300]
    conversation.last_message_at = now
    conversation.save(update_fields=["last_message", "last_message_at", "updated_at"])
    return msg
