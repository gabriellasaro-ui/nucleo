from django.conf import settings
from django.core.validators import MaxValueValidator
from django.db import models
from django.urls import reverse

from core.tenancy import TenantManager


class TimestampedModel(models.Model):
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        abstract = True


class Tag(TimestampedModel):
    """A workspace-scoped label that can be attached to any CRM record."""
    workspace = models.ForeignKey("core.Workspace", on_delete=models.CASCADE, related_name="tags")
    name = models.CharField("Nome", max_length=60)
    color = models.CharField("Cor", max_length=7, default="#2563eb")

    objects = TenantManager()
    all_objects = models.Manager()

    class Meta:
        base_manager_name = "all_objects"
        ordering = ["name"]
        unique_together = [("workspace", "name")]
        verbose_name = "Etiqueta"
        verbose_name_plural = "Etiquetas"

    def __str__(self):
        return self.name


class Company(TimestampedModel):
    workspace = models.ForeignKey("core.Workspace", on_delete=models.CASCADE, related_name="companies")
    custom = models.JSONField(default=dict, blank=True)
    name = models.CharField("Nome", max_length=200)
    domain = models.CharField("Domínio", max_length=200, blank=True)
    industry = models.CharField("Segmento", max_length=120, blank=True)
    employees = models.PositiveIntegerField("Funcionários", null=True, blank=True)
    city = models.CharField("Cidade", max_length=120, blank=True)
    owner = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, blank=True,
        related_name="owned_companies", verbose_name="Responsável",
    )
    score = models.PositiveSmallIntegerField("Score", default=0, validators=[MaxValueValidator(100)])
    tags = models.ManyToManyField("Tag", blank=True, related_name="companies", verbose_name="Etiquetas")
    parent = models.ForeignKey(
        "self", on_delete=models.SET_NULL, null=True, blank=True,
        related_name="subsidiaries", verbose_name="Empresa matriz",
    )
    assignees = models.ManyToManyField(
        settings.AUTH_USER_MODEL, blank=True, related_name="assigned_companies",
        verbose_name="Responsáveis",
    )

    objects = TenantManager()
    all_objects = models.Manager()

    class Meta:
        base_manager_name = "all_objects"
        ordering = ["name"]
        verbose_name = "Empresa"
        verbose_name_plural = "Empresas"

    def __str__(self):
        return self.name

    def get_absolute_url(self):
        return reverse("crm:company_detail", args=[self.pk])

    @property
    def initials(self):
        parts = [p for p in self.name.split() if p]
        if not parts:
            return "?"
        if len(parts) == 1:
            return parts[0][:2].upper()
        return (parts[0][0] + parts[-1][0]).upper()


class Contact(TimestampedModel):
    STAGE_CHOICES = [
        ("lead", "Lead"),
        ("qualificado", "Qualificado"),
        ("cliente", "Cliente"),
        ("inativo", "Inativo"),
    ]

    workspace = models.ForeignKey("core.Workspace", on_delete=models.CASCADE, related_name="contacts")
    custom = models.JSONField(default=dict, blank=True)
    first_name = models.CharField("Nome", max_length=120)
    last_name = models.CharField("Sobrenome", max_length=120, blank=True)
    email = models.EmailField("E-mail", blank=True)
    phone = models.CharField("Telefone", max_length=40, blank=True)
    job_title = models.CharField("Cargo", max_length=120, blank=True)
    address_street = models.CharField("Endereço", max_length=180, blank=True)
    address_number = models.CharField("Número", max_length=30, blank=True)
    address_complement = models.CharField("Complemento", max_length=80, blank=True)
    district = models.CharField("Bairro", max_length=100, blank=True)
    city = models.CharField("Cidade", max_length=120, blank=True)
    state = models.CharField("UF", max_length=2, blank=True)
    zipcode = models.CharField("CEP", max_length=20, blank=True)
    stage = models.CharField("Estágio", max_length=20, choices=STAGE_CHOICES, default="lead")
    company = models.ForeignKey(
        Company,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="contacts",
        verbose_name="Empresa",
    )
    owner = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, blank=True,
        related_name="owned_contacts", verbose_name="Responsável",
    )
    score = models.PositiveSmallIntegerField("Score", default=0, validators=[MaxValueValidator(100)])
    tags = models.ManyToManyField("Tag", blank=True, related_name="contacts", verbose_name="Etiquetas")
    assignees = models.ManyToManyField(
        settings.AUTH_USER_MODEL, blank=True, related_name="assigned_contacts",
        verbose_name="Responsáveis",
    )

    objects = TenantManager()
    all_objects = models.Manager()

    class Meta:
        base_manager_name = "all_objects"
        ordering = ["first_name", "last_name"]
        verbose_name = "Contato"
        verbose_name_plural = "Contatos"

    def __str__(self):
        return self.full_name

    def get_absolute_url(self):
        return reverse("crm:contact_detail", args=[self.pk])

    @property
    def full_name(self):
        return f"{self.first_name} {self.last_name}".strip()

    @property
    def initials(self):
        first = self.first_name[:1]
        last = self.last_name[:1] if self.last_name else ""
        return (first + last).upper() or "?"


class WhatsAppConversation(TimestampedModel):
    STATUS_CHOICES = [("open", "Aberta"), ("archived", "Arquivada")]

    workspace = models.ForeignKey(
        "core.Workspace", on_delete=models.CASCADE, related_name="whatsapp_conversations",
    )
    contact = models.ForeignKey(
        Contact, on_delete=models.SET_NULL, null=True, blank=True,
        related_name="whatsapp_conversations",
    )
    instance_id = models.CharField(max_length=80)
    remote_jid = models.CharField(max_length=180)
    phone = models.CharField(max_length=40)
    name = models.CharField(max_length=160, blank=True)
    avatar_url = models.URLField(max_length=500, blank=True)
    is_history_import = models.BooleanField(default=False)
    status = models.CharField(max_length=12, choices=STATUS_CHOICES, default="open")
    unread_count = models.PositiveIntegerField(default=0)
    last_message = models.CharField(max_length=300, blank=True)
    last_message_at = models.DateTimeField(null=True, blank=True)

    objects = TenantManager()
    all_objects = models.Manager()

    class Meta:
        base_manager_name = "all_objects"
        ordering = ["-last_message_at", "-updated_at"]
        unique_together = [("workspace", "instance_id", "remote_jid")]
        indexes = [
            models.Index(fields=["workspace", "-last_message_at"]),
            models.Index(fields=["workspace", "phone"]),
        ]
        verbose_name = "Conversa do WhatsApp"
        verbose_name_plural = "Conversas do WhatsApp"

    def __str__(self):
        return self.name or self.phone


class WhatsAppMessage(TimestampedModel):
    DIRECTION_CHOICES = [("incoming", "Recebida"), ("outgoing", "Enviada")]

    workspace = models.ForeignKey(
        "core.Workspace", on_delete=models.CASCADE, related_name="whatsapp_messages",
    )
    conversation = models.ForeignKey(
        WhatsAppConversation, on_delete=models.CASCADE, related_name="messages",
    )
    instance_id = models.CharField(max_length=80)
    provider_message_id = models.CharField(max_length=180)
    direction = models.CharField(max_length=10, choices=DIRECTION_CHOICES)
    message_type = models.CharField(max_length=30, default="text")
    text = models.TextField(blank=True)
    media_url = models.URLField(max_length=700, blank=True)
    status = models.CharField(max_length=30, default="received")
    sent_at = models.DateTimeField()
    raw = models.JSONField(default=dict, blank=True)

    objects = TenantManager()
    all_objects = models.Manager()

    class Meta:
        base_manager_name = "all_objects"
        ordering = ["sent_at", "id"]
        unique_together = [("workspace", "instance_id", "provider_message_id")]
        indexes = [
            models.Index(fields=["conversation", "sent_at"]),
        ]
        verbose_name = "Mensagem do WhatsApp"
        verbose_name_plural = "Mensagens do WhatsApp"

    def __str__(self):
        return self.text[:60] or self.message_type


DEFAULT_STAGES = [
    # key, name, color, kind
    ("novo", "Novo", "#94a3b8", "open"),
    ("qualificado", "Qualificado", "#3b82f6", "open"),
    ("proposta", "Proposta", "#8b5cf6", "open"),
    ("negociacao", "Negociação", "#d97706", "open"),
    ("ganho", "Ganho", "#059669", "won"),
    ("perdido", "Perdido", "#dc2626", "lost"),
]

DEFAULT_STAGE_CARD_FIELDS = ["company", "value", "expected_close", "contact"]


def default_stage_card_fields():
    return DEFAULT_STAGE_CARD_FIELDS.copy()


class Pipeline(TimestampedModel):
    """A configurable sales pipeline. A workspace can have several."""
    workspace = models.ForeignKey("core.Workspace", on_delete=models.CASCADE, related_name="pipelines")
    name = models.CharField("Nome", max_length=120)
    order = models.PositiveIntegerField(default=0)
    is_default = models.BooleanField(default=False)

    class Meta:
        ordering = ["order", "id"]
        verbose_name = "Pipeline"
        verbose_name_plural = "Pipelines"

    def __str__(self):
        return self.name

    def ensure_stages(self):
        """Create the default stage set if this pipeline has none yet."""
        if not self.stages.exists():
            for i, (key, name, color, kind) in enumerate(DEFAULT_STAGES):
                self.stages.create(key=key, name=name, color=color, kind=kind, order=i)
        return self


class Stage(TimestampedModel):
    KIND_CHOICES = [
        ("open", "Em andamento"),
        ("won", "Ganho"),
        ("lost", "Perdido"),
        ("disqualified", "Desqualificado"),
    ]

    pipeline = models.ForeignKey(Pipeline, on_delete=models.CASCADE, related_name="stages")
    key = models.SlugField(max_length=40)
    name = models.CharField("Nome", max_length=60)
    color = models.CharField("Cor", max_length=7, default="#2563eb")
    kind = models.CharField("Tipo", max_length=20, choices=KIND_CHOICES, default="open")
    card_fields = models.JSONField(default=default_stage_card_fields, blank=True)
    order = models.PositiveIntegerField(default=0)

    class Meta:
        ordering = ["order", "id"]
        unique_together = [("pipeline", "key")]
        verbose_name = "Etapa"
        verbose_name_plural = "Etapas"

    def __str__(self):
        return f"{self.pipeline.name} · {self.name}"


class Deal(TimestampedModel):
    STAGE_CHOICES = [
        ("novo", "Novo"),
        ("qualificado", "Qualificado"),
        ("proposta", "Proposta"),
        ("negociacao", "Negociação"),
        ("ganho", "Ganho"),
        ("perdido", "Perdido"),
    ]
    OPEN_STAGES = ["novo", "qualificado", "proposta", "negociacao"]

    workspace = models.ForeignKey("core.Workspace", on_delete=models.CASCADE, related_name="deals")
    pipeline = models.ForeignKey(
        Pipeline, on_delete=models.SET_NULL, null=True, blank=True, related_name="deals",
        verbose_name="Pipeline",
    )
    stage_kind = models.CharField(max_length=20, default="open")  # denormalized status
    custom = models.JSONField(default=dict, blank=True)
    title = models.CharField("Título", max_length=200)
    value = models.DecimalField("Valor", max_digits=12, decimal_places=2, default=0)
    stage = models.CharField("Estágio", max_length=20, choices=STAGE_CHOICES, default="novo")
    order = models.PositiveIntegerField("Ordem", default=0)
    expected_close = models.DateField("Previsão de fechamento", null=True, blank=True)
    company = models.ForeignKey(
        Company, on_delete=models.SET_NULL, null=True, blank=True,
        related_name="deals", verbose_name="Empresa",
    )
    contact = models.ForeignKey(
        Contact, on_delete=models.SET_NULL, null=True, blank=True,
        related_name="deals", verbose_name="Contato",
    )
    owner = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, blank=True,
        related_name="owned_deals", verbose_name="Responsável",
    )
    tags = models.ManyToManyField("Tag", blank=True, related_name="deals", verbose_name="Etiquetas")
    assignees = models.ManyToManyField(
        settings.AUTH_USER_MODEL, blank=True, related_name="assigned_deals",
        verbose_name="Responsáveis",
    )

    objects = TenantManager()
    all_objects = models.Manager()

    class Meta:
        base_manager_name = "all_objects"
        ordering = ["order", "-created_at"]
        verbose_name = "Negócio"
        verbose_name_plural = "Negócios"

    def __str__(self):
        return self.title

    def get_absolute_url(self):
        return reverse("crm:deal_detail", args=[self.pk])

    @property
    def is_open(self):
        return self.stage_kind == "open"

    @property
    def stage_obj(self):
        if self.pipeline_id:
            for s in self.pipeline.stages.all():
                if s.key == self.stage:
                    return s
        return None

    @property
    def stage_display(self):
        obj = self.stage_obj
        if obj:
            return obj.name
        return dict((k, n) for k, n, _, _ in DEFAULT_STAGES).get(self.stage, self.stage)

    @property
    def stage_color(self):
        obj = self.stage_obj
        if obj:
            return obj.color
        return dict((k, c) for k, _, c, _ in DEFAULT_STAGES).get(self.stage, "#94a3b8")

    def sync_stage_kind(self):
        """Keep the denormalized stage_kind (open/won/lost) in sync with stage."""
        obj = self.stage_obj
        self.stage_kind = obj.kind if obj else dict(
            (k, kind) for k, _, _, kind in DEFAULT_STAGES
        ).get(self.stage, "open")


class Activity(TimestampedModel):
    KIND_CHOICES = [("note", "Nota"), ("task", "Tarefa")]

    SOURCE_CHOICES = [("manual", "Manual"), ("automation", "Automação")]

    workspace = models.ForeignKey("core.Workspace", on_delete=models.CASCADE, related_name="activities")
    kind = models.CharField("Tipo", max_length=10, choices=KIND_CHOICES, default="note")
    source = models.CharField(max_length=12, choices=SOURCE_CHOICES, default="manual")
    body = models.TextField("Conteúdo")
    due_date = models.DateField("Prazo", null=True, blank=True)
    done = models.BooleanField("Concluída", default=False)
    author = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, blank=True,
        related_name="crm_activities", verbose_name="Autor",
    )
    company = models.ForeignKey(
        Company, on_delete=models.CASCADE, null=True, blank=True,
        related_name="activities", verbose_name="Empresa",
    )
    contact = models.ForeignKey(
        Contact, on_delete=models.CASCADE, null=True, blank=True,
        related_name="activities", verbose_name="Contato",
    )
    deal = models.ForeignKey(
        Deal, on_delete=models.CASCADE, null=True, blank=True,
        related_name="activities", verbose_name="Negócio",
    )
    mentions = models.ManyToManyField(
        settings.AUTH_USER_MODEL, blank=True, related_name="mentioned_in_activities",
    )

    objects = TenantManager()
    all_objects = models.Manager()

    class Meta:
        base_manager_name = "all_objects"
        # Open tasks first (by due date), then everything newest-first.
        ordering = ["done", "-created_at"]
        verbose_name = "Atividade"
        verbose_name_plural = "Atividades"

    def __str__(self):
        return f"{self.get_kind_display()}: {self.body[:40]}"

    @property
    def is_task(self):
        return self.kind == "task"

    @property
    def is_overdue(self):
        from django.utils import timezone
        return bool(self.is_task and not self.done and self.due_date and self.due_date < timezone.localdate())


def attachment_path(instance, filename):
    """Per-tenant media: keep each workspace's files under its own folder."""
    return f"ws_{instance.workspace_id or 'x'}/attachments/{filename}"


class Attachment(TimestampedModel):
    """A file attached to a CRM record (company, contact or deal)."""
    workspace = models.ForeignKey("core.Workspace", on_delete=models.CASCADE, related_name="attachments")
    file = models.FileField("Arquivo", upload_to=attachment_path)
    name = models.CharField(max_length=255, blank=True)
    uploaded_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, blank=True,
        related_name="crm_uploads",
    )
    company = models.ForeignKey(Company, on_delete=models.CASCADE, null=True, blank=True, related_name="attachments")
    contact = models.ForeignKey(Contact, on_delete=models.CASCADE, null=True, blank=True, related_name="attachments")
    deal = models.ForeignKey(Deal, on_delete=models.CASCADE, null=True, blank=True, related_name="attachments")

    objects = TenantManager()
    all_objects = models.Manager()

    class Meta:
        base_manager_name = "all_objects"
        ordering = ["-created_at"]
        verbose_name = "Arquivo"
        verbose_name_plural = "Arquivos"

    def save(self, *args, **kwargs):
        if not self.name and self.file:
            self.name = self.file.name.rsplit("/", 1)[-1]
        super().save(*args, **kwargs)

    def __str__(self):
        return self.name or self.file.name

    @property
    def extension(self):
        base = self.name or self.file.name
        return base.rsplit(".", 1)[-1].lower() if "." in base else ""

    @property
    def size_kb(self):
        try:
            return round(self.file.size / 1024)
        except Exception:
            return 0
