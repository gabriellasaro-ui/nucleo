from django.conf import settings
from django.db import models
from django.utils.text import slugify
from django_tenants.models import DomainMixin, TenantMixin


def workspace_logo_path(instance, filename):
    """Store each tenant's uploads under its own folder (media isolation)."""
    return f"ws_{instance.pk or 'new'}/logo/{filename}"


class Workspace(TenantMixin):
    """A tenant. It OWNS a PostgreSQL schema; all CRM data lives inside it."""
    SIDEBAR_CHOICES = [("light", "Clara"), ("dark", "Escura")]
    RADIUS_CHOICES = [("rounded", "Arredondado"), ("soft", "Suave"), ("sharp", "Reto")]

    name = models.CharField("Nome", max_length=120)
    brand_color = models.CharField("Cor da marca", max_length=7, default="#2563eb")
    logo = models.FileField("Logo", upload_to=workspace_logo_path, blank=True, null=True)
    sidebar_theme = models.CharField("Menu lateral", max_length=10, choices=SIDEBAR_CHOICES, default="light")
    ui_radius = models.CharField("Cantos", max_length=10, choices=RADIUS_CHOICES, default="rounded")
    created_at = models.DateTimeField(auto_now_add=True)

    # Create/drop the Postgres schema automatically with the workspace.
    auto_create_schema = True
    auto_drop_schema = True

    class Meta:
        ordering = ["name"]
        verbose_name = "Workspace"
        verbose_name_plural = "Workspaces"

    def save(self, *args, **kwargs):
        if not self.schema_name:
            self.schema_name = self._unique_schema_name()
        super().save(*args, **kwargs)

    def delete(self, *args, **kwargs):
        # Tenant CRM tables live in this workspace's schema and FK back to it,
        # which confuses Django's cascade collector. So drop the whole schema
        # (removing all tenant data at once), then remove the public-side rows
        # via SQL — no ORM cascade into the tenant schema.
        from django.db import connection
        from django_tenants.utils import get_public_schema_name

        schema, pk = self.schema_name, self.pk
        connection.set_schema_to_public()
        with connection.cursor() as cur:
            if schema and schema != get_public_schema_name():
                cur.execute(f'DROP SCHEMA IF EXISTS "{schema}" CASCADE')
            for model, column in [
                (Domain, "tenant_id"),
                (Membership, "workspace_id"),
                (CustomField, "workspace_id"),
                (IntegrationConnection, "workspace_id"),
                (Automation, "workspace_id"),
                (AutomationRun, "workspace_id"),
                (Event, "workspace_id"),
            ]:
                cur.execute(f'DELETE FROM "{model._meta.db_table}" WHERE {column} = %s', [pk])
            cur.execute(f'DELETE FROM "{self._meta.db_table}" WHERE id = %s', [pk])

    def _unique_schema_name(self):
        base = "ws_" + (slugify(self.name).replace("-", "_")[:40] or "tenant")
        candidate, i = base, 2
        while Workspace.objects.filter(schema_name=candidate).exclude(pk=self.pk).exists():
            candidate = f"{base}_{i}"
            i += 1
        return candidate

    def __str__(self):
        return self.name

    @property
    def initials(self):
        parts = [p for p in self.name.split() if p]
        if not parts:
            return "?"
        if len(parts) == 1:
            return parts[0][:2].upper()
        return (parts[0][0] + parts[-1][0]).upper()


class Domain(DomainMixin):
    """Required by django-tenants. We route by session (not domain), so this is
    mostly a placeholder — one internal domain per tenant keeps the lib happy."""
    pass


class Membership(models.Model):
    """Links a user to a workspace with a role. Roles are hierarchical."""
    ROLE_OWNER = "owner"
    ROLE_ADMIN = "admin"
    ROLE_MEMBER = "member"
    ROLE_VIEWER = "viewer"
    ROLE_CHOICES = [
        (ROLE_OWNER, "Proprietário"),
        (ROLE_ADMIN, "Administrador"),
        (ROLE_MEMBER, "Membro"),
        (ROLE_VIEWER, "Visualizador"),
    ]
    ROLE_LEVEL = {ROLE_VIEWER: 0, ROLE_MEMBER: 1, ROLE_ADMIN: 2, ROLE_OWNER: 3}

    user = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name="memberships"
    )
    workspace = models.ForeignKey(
        Workspace, on_delete=models.CASCADE, related_name="memberships"
    )
    role = models.CharField(max_length=10, choices=ROLE_CHOICES, default=ROLE_MEMBER)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        unique_together = [("user", "workspace")]
        ordering = ["-role", "user__username"]
        verbose_name = "Membro"
        verbose_name_plural = "Membros"

    def __str__(self):
        return f"{self.user} @ {self.workspace} ({self.get_role_display()})"

    @property
    def level(self):
        return self.ROLE_LEVEL.get(self.role, 0)

    def can(self, min_role):
        return self.level >= self.ROLE_LEVEL.get(min_role, 99)


class CustomField(models.Model):
    """Metadata engine: workspace-defined fields for a CRM object type.
    Values live in each record's `custom` JSON, keyed by `key`.
    """
    OBJECT_CHOICES = [
        ("company", "Empresas"),
        ("contact", "Contatos"),
        ("deal", "Negócios"),
    ]
    TYPE_CHOICES = [
        ("text", "Texto"),
        ("textarea", "Texto longo"),
        ("number", "Número"),
        ("date", "Data"),
        ("select", "Lista (uma opção)"),
        ("multiselect", "Múltipla escolha"),
        ("checkbox", "Sim/Não"),
    ]

    workspace = models.ForeignKey(Workspace, on_delete=models.CASCADE, related_name="custom_fields")
    object_type = models.CharField(max_length=20, choices=OBJECT_CHOICES)
    key = models.SlugField(max_length=60)
    label = models.CharField("Rótulo", max_length=80)
    field_type = models.CharField("Tipo", max_length=12, choices=TYPE_CHOICES, default="text")
    options = models.JSONField(default=list, blank=True)  # for select
    required = models.BooleanField("Obrigatório", default=False)
    order = models.PositiveIntegerField(default=0)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        unique_together = [("workspace", "object_type", "key")]
        ordering = ["object_type", "order", "id"]
        verbose_name = "Campo personalizado"
        verbose_name_plural = "Campos personalizados"

    def __str__(self):
        return f"{self.get_object_type_display()} · {self.label}"

    @property
    def is_select(self):
        return self.field_type == "select"

    @property
    def is_multiselect(self):
        return self.field_type == "multiselect"

    @property
    def has_options(self):
        """Types backed by a user-defined option list."""
        return self.field_type in ("select", "multiselect")

    @property
    def is_checkbox(self):
        return self.field_type == "checkbox"


# Trigger/event vocabulary shared by Event (outbox) and Automation.
EVENT_CHOICES = [
    ("deal_created", "Negócio criado"),
    ("deal_stage_changed", "Negócio mudou de etapa"),
    ("company_created", "Empresa criada"),
    ("contact_created", "Contato criado"),
    ("contact_stage_changed", "Contato mudou de estágio"),
    ("schedule_interval", "Schedule trigger"),
    ("webhook_received", "Webhook recebido"),
    ("facebook_lead", "Lead do Facebook"),
]


class Event(models.Model):
    """Outbox: an immutable record of something that happened in a workspace.
    Automations react to events; the log doubles as an audit trail.
    """
    workspace = models.ForeignKey(Workspace, on_delete=models.CASCADE, related_name="events")
    event_type = models.CharField(max_length=40, choices=EVENT_CHOICES)
    source_key = models.CharField(max_length=180, blank=True, default="", db_index=True)
    object_repr = models.CharField(max_length=200, blank=True)
    payload = models.JSONField(default=dict, blank=True)
    processed = models.BooleanField(default=False)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["-created_at"]
        verbose_name = "Evento"
        verbose_name_plural = "Eventos"
        constraints = [
            models.UniqueConstraint(
                fields=["workspace", "event_type", "source_key"],
                condition=~models.Q(source_key=""),
                name="unique_event_source_key",
            ),
        ]

    def __str__(self):
        return f"{self.get_event_type_display()} · {self.object_repr}"


class Automation(models.Model):
    """When a trigger happens, run one or more CRM actions in order."""
    ICON_CHOICES = [
        ("bolt", "Raio"),
        ("clock", "Tempo"),
        ("pipeline", "Pipeline"),
        ("users", "Contatos"),
        ("command", "Integracao"),
        ("sliders", "Filtro"),
        ("check", "Regra"),
        ("hash", "Campo"),
    ]

    ACTION_CHOICES = [
        ("create_task", "Criar tarefa"),
        ("create_note", "Adicionar nota"),
        ("create_contact", "Criar contato"),
        ("create_company", "Criar empresa"),
        ("create_deal", "Criar negócio"),
        ("move_deal", "Mover negócio"),
        ("set_contact_stage", "Atualizar estágio do contato"),
        ("set_custom_field", "Atualizar campo"),
        ("delay", "Aguardar"),
        ("send_webhook", "Enviar webhook"),
        ("http_request", "HTTP request"),
    ]

    workspace = models.ForeignKey(Workspace, on_delete=models.CASCADE, related_name="automations")
    name = models.CharField("Nome", max_length=120)
    icon = models.CharField("Ícone", max_length=24, choices=ICON_CHOICES, default="bolt")
    trigger = models.CharField("Gatilho", max_length=40, choices=EVENT_CHOICES)
    condition_stage = models.CharField("Etapa/coluna (condição)", max_length=20, blank=True)
    action = models.CharField("Ação", max_length=20, choices=ACTION_CHOICES, default="create_task")
    action_text = models.CharField("Texto", max_length=300, blank=True)
    action_due_days = models.PositiveIntegerField("Prazo (dias)", default=2)
    conditions = models.JSONField(default=dict, blank=True)
    actions = models.JSONField(default=list, blank=True)
    canvas = models.JSONField(default=dict, blank=True)
    active = models.BooleanField("Ativa", default=True)
    run_count = models.PositiveIntegerField(default=0)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["-created_at"]
        verbose_name = "Automação"
        verbose_name_plural = "Automações"

    def __str__(self):
        return self.name

    def describe(self):
        trigger = dict(EVENT_CHOICES).get(self.trigger, self.trigger)
        if self.condition_stage:
            stage = (
                dict(_deal_stage_labels()).get(self.condition_stage)
                or dict(_contact_stage_labels()).get(self.condition_stage)
                or self.condition_stage
            )
            trigger = f"{trigger} em {stage}"
        actions = [self._action_label(a) for a in self.normalized_actions()]
        return f"Quando {trigger.lower()} → " + " → ".join(actions)

    def normalized_actions(self):
        if self.actions:
            return self.actions
        return [{
            "type": self.action,
            "text": self.action_text,
            "due_days": self.action_due_days,
        }]

    def _action_label(self, action):
        action_type = action.get("type")
        labels = dict(self.ACTION_CHOICES)
        label = labels.get(action_type, action_type)
        if action_type == "delay":
            if action.get("delay_until"):
                return f"aguardar ate {action.get('delay_until')}"
            amount = action.get("delay_amount") or action.get("delay_minutes") or 0
            unit = {
                "minutes": "min",
                "hours": "h",
                "days": "dia(s)",
            }.get(action.get("delay_unit"), "min")
            return f"aguardar {amount} {unit}"
        if action_type == "send_webhook":
            return "enviar webhook"
        if action_type == "http_request":
            method = action.get("http_method") or "POST"
            return f"HTTP {method}"
        stage = action.get("deal_stage") or action.get("contact_stage") or action.get("stage")
        if stage:
            stage_label = (
                dict(_deal_stage_labels()).get(stage)
                or dict(_contact_stage_labels()).get(stage)
                or stage
            )
            return f"{label.lower()} em {stage_label}"
        return label.lower()


class AutomationRun(models.Model):
    STATUS_CHOICES = [
        ("success", "Sucesso"),
        ("skipped", "Ignorada"),
        ("scheduled", "Agendada"),
        ("error", "Erro"),
        ("test", "Teste"),
    ]

    workspace = models.ForeignKey(Workspace, on_delete=models.CASCADE, related_name="automation_runs")
    automation = models.ForeignKey(Automation, on_delete=models.CASCADE, related_name="runs", null=True, blank=True)
    event = models.ForeignKey(Event, on_delete=models.SET_NULL, related_name="automation_runs", null=True, blank=True)
    status = models.CharField(max_length=12, choices=STATUS_CHOICES, default="success")
    object_model = models.CharField(max_length=40, blank=True)
    object_id = models.PositiveIntegerField(null=True, blank=True)
    object_repr = models.CharField(max_length=200, blank=True)
    summary = models.CharField(max_length=300, blank=True)
    path = models.JSONField(default=list, blank=True)
    scheduled_for = models.DateTimeField(null=True, blank=True)
    resume_node_id = models.CharField(max_length=80, blank=True)
    resumed_at = models.DateTimeField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["-created_at"]
        verbose_name = "Execução de automação"
        verbose_name_plural = "Execuções de automação"

    def __str__(self):
        name = self.automation.name if self.automation_id else "Teste"
        return f"{name} · {self.status}"


class IntegrationConnection(models.Model):
    PROVIDER_CHOICES = [
        ("facebook", "Facebook Lead Ads"),
        ("instagram", "Instagram"),
        ("forms", "Forms nativos"),
        ("whatsapp", "WhatsApp"),
        ("webhook", "Webhooks"),
        ("api", "API / HTTP"),
    ]
    STATUS_CHOICES = [
        ("disconnected", "Desconectada"),
        ("connected", "Conectada"),
    ]

    workspace = models.ForeignKey(Workspace, on_delete=models.CASCADE, related_name="integrations")
    provider = models.CharField(max_length=24, choices=PROVIDER_CHOICES)
    name = models.CharField(max_length=80)
    status = models.CharField(max_length=20, choices=STATUS_CHOICES, default="disconnected")
    config = models.JSONField(default=dict, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        unique_together = [("workspace", "provider")]
        ordering = ["provider"]
        verbose_name = "Integração"
        verbose_name_plural = "Integrações"

    def __str__(self):
        return f"{self.workspace} · {self.name}"


def _deal_stage_labels():
    from modules.crm.models import Deal
    return Deal.STAGE_CHOICES


def _contact_stage_labels():
    from modules.crm.models import Contact
    return Contact.STAGE_CHOICES
