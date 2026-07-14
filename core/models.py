from django.conf import settings
from django.db import models
from django.utils.text import slugify
from django_tenants.models import DomainMixin, TenantMixin


def workspace_logo_path(instance, filename):
    """Store each tenant's uploads under its own folder (media isolation)."""
    return f"ws_{instance.pk or 'new'}/logo/{filename}"


class Workspace(TenantMixin):
    """A tenant. It OWNS a PostgreSQL schema; all CRM data lives inside it."""
    name = models.CharField("Nome", max_length=120)
    brand_color = models.CharField("Cor da marca", max_length=7, default="#2563eb")
    logo = models.FileField("Logo", upload_to=workspace_logo_path, blank=True, null=True)
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
                (Automation, "workspace_id"),
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
        ("select", "Seleção"),
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
    def is_checkbox(self):
        return self.field_type == "checkbox"


# Trigger/event vocabulary shared by Event (outbox) and Automation.
EVENT_CHOICES = [
    ("deal_created", "Negócio criado"),
    ("deal_stage_changed", "Negócio mudou de etapa"),
    ("company_created", "Empresa criada"),
    ("contact_created", "Contato criado"),
]


class Event(models.Model):
    """Outbox: an immutable record of something that happened in a workspace.
    Automations react to events; the log doubles as an audit trail.
    """
    workspace = models.ForeignKey(Workspace, on_delete=models.CASCADE, related_name="events")
    event_type = models.CharField(max_length=40, choices=EVENT_CHOICES)
    object_repr = models.CharField(max_length=200, blank=True)
    payload = models.JSONField(default=dict, blank=True)
    processed = models.BooleanField(default=False)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["-created_at"]
        verbose_name = "Evento"
        verbose_name_plural = "Eventos"

    def __str__(self):
        return f"{self.get_event_type_display()} · {self.object_repr}"


class Automation(models.Model):
    """When <trigger> happens (optionally matching a condition), do <action>."""
    ACTION_CHOICES = [
        ("create_task", "Criar tarefa"),
        ("create_note", "Adicionar nota"),
    ]

    workspace = models.ForeignKey(Workspace, on_delete=models.CASCADE, related_name="automations")
    name = models.CharField("Nome", max_length=120)
    trigger = models.CharField("Gatilho", max_length=40, choices=EVENT_CHOICES)
    condition_stage = models.CharField("Etapa (condição)", max_length=20, blank=True)
    action = models.CharField("Ação", max_length=20, choices=ACTION_CHOICES, default="create_task")
    action_text = models.CharField("Texto", max_length=300)
    action_due_days = models.PositiveIntegerField("Prazo (dias)", default=2)
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
        if self.trigger == "deal_stage_changed" and self.condition_stage:
            stage = dict(_deal_stage_labels()).get(self.condition_stage, self.condition_stage)
            trigger = f"Negócio movido para “{stage}”"
        action = dict(self.ACTION_CHOICES).get(self.action, self.action)
        return f"Quando {trigger.lower()} → {action.lower()}: “{self.action_text}”"


def _deal_stage_labels():
    from modules.crm.models import Deal
    return Deal.STAGE_CHOICES
