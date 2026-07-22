import json
import secrets
import urllib.error
import urllib.request
from urllib.parse import urljoin

from django.conf import settings


class EvoGoError(Exception):
    pass


def evogo_is_configured():
    return bool(settings.EVOGO_API_URL and settings.EVOGO_GLOBAL_API_KEY)


def _evogo_request(path, *, api_key, method="GET", payload=None, timeout=15):
    base_url = settings.EVOGO_API_URL.rstrip("/") + "/"
    if not base_url.startswith(("http://", "https://")):
        raise EvoGoError("A URL da EvoGo não está configurada corretamente.")

    data = None
    headers = {"apikey": api_key, "Accept": "application/json"}
    if payload is not None:
        data = json.dumps(payload).encode("utf-8")
        headers["Content-Type"] = "application/json"

    request = urllib.request.Request(
        urljoin(base_url, path.lstrip("/")),
        data=data,
        headers=headers,
        method=method,
    )
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            raw = response.read().decode("utf-8")
    except urllib.error.HTTPError as exc:
        raw = exc.read().decode("utf-8", errors="replace")
        try:
            body = json.loads(raw)
            detail = body.get("error") or body.get("message")
        except (json.JSONDecodeError, AttributeError):
            detail = None
        raise EvoGoError(detail or f"EvoGo respondeu com HTTP {exc.code}.") from exc
    except (urllib.error.URLError, TimeoutError) as exc:
        raise EvoGoError("Não foi possível acessar o servidor do WhatsApp.") from exc

    try:
        result = json.loads(raw) if raw else {}
    except json.JSONDecodeError as exc:
        raise EvoGoError("A EvoGo retornou uma resposta inválida.") from exc
    if isinstance(result, dict) and result.get("error"):
        raise EvoGoError(str(result["error"]))
    return result


def create_instance(name):
    token = secrets.token_urlsafe(32)
    result = _evogo_request(
        "/instance/create",
        api_key=settings.EVOGO_GLOBAL_API_KEY,
        method="POST",
        payload={
            "name": name,
            "token": token,
            "advancedSettings": {
                "alwaysOnline": False,
                "readMessages": False,
                "ignoreGroups": True,
                "ignoreStatus": True,
                "rejectCall": False,
                "msgRejectCall": "",
            },
        },
    )
    data = result.get("data") or {}
    if not data.get("id"):
        raise EvoGoError("A EvoGo não devolveu o identificador da instância.")
    return data, token


def connect_instance(instance_token, webhook_url):
    return _evogo_request(
        "/instance/connect",
        api_key=instance_token,
        method="POST",
        payload={
            "webhookUrl": webhook_url,
            "subscribe": [
                "MESSAGE",
                "SEND_MESSAGE",
                "READ_RECEIPT",
                "CONNECTION",
                "QRCODE",
                "CONTACT",
                "HISTORY_SYNC",
            ],
            "immediate": True,
            "rabbitmqEnable": "disabled",
            "websocketEnable": "disabled",
            "natsEnable": "disabled",
        },
        timeout=20,
    )


def get_instance_status(instance_token):
    result = _evogo_request("/instance/status", api_key=instance_token)
    return result.get("data") or {}


def get_instance_qr(instance_token):
    result = _evogo_request(
        "/instance/qr", api_key=instance_token, timeout=20,
    )
    return result.get("data") or {}


def send_text(instance_token, phone, text):
    return _evogo_request(
        "/send/text",
        api_key=instance_token,
        method="POST",
        payload={"number": phone, "text": text, "formatJid": True},
        timeout=30,
    )


def logout_instance(instance_token):
    return _evogo_request(
        "/instance/logout", api_key=instance_token, method="DELETE", timeout=20,
    )


def delete_instance(instance_id):
    return _evogo_request(
        f"/instance/delete/{instance_id}",
        api_key=settings.EVOGO_GLOBAL_API_KEY,
        method="DELETE",
        timeout=20,
    )
