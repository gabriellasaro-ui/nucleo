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
        return self.stage in self.OPEN_STAGES


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
