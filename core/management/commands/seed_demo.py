"""Seed a demo workspace: an admin user, a workspace, and sample CRM data.

    python manage.py seed_demo

Idempotent — safe to run more than once.
"""
from datetime import date, timedelta

from django.contrib.auth import get_user_model
from django.core.management.base import BaseCommand

from core.events import emit
from core.models import Automation, CustomField, Membership, Workspace
from modules.crm.models import Activity, Company, Contact, Deal, Tag

COMPANIES = [
    {"name": "Acme Tecnologia", "domain": "acme.com.br", "industry": "SaaS", "employees": 120, "city": "São Paulo"},
    {"name": "Lumina Design", "domain": "lumina.co", "industry": "Agência", "employees": 24, "city": "Belo Horizonte"},
    {"name": "Nordeste Logística", "domain": "nordestelog.com", "industry": "Logística", "employees": 340, "city": "Recife"},
    {"name": "Verde Agro", "domain": "verdeagro.agr.br", "industry": "Agronegócio", "employees": 78, "city": "Goiânia"},
    {"name": "Pixel Studio", "domain": "pixel.studio", "industry": "Mídia", "employees": 12, "city": "Curitiba"},
]

CONTACTS = [
    {"first_name": "Marina", "last_name": "Alves", "email": "marina@acme.com.br", "job_title": "Head de Growth", "stage": "cliente", "company": "Acme Tecnologia"},
    {"first_name": "Rafael", "last_name": "Souza", "email": "rafael@lumina.co", "job_title": "Diretor de Arte", "stage": "qualificado", "company": "Lumina Design"},
    {"first_name": "Beatriz", "last_name": "Nunes", "email": "bia@nordestelog.com", "job_title": "Gerente Comercial", "stage": "lead", "company": "Nordeste Logística"},
    {"first_name": "Diego", "last_name": "Martins", "email": "diego@verdeagro.agr.br", "job_title": "CEO", "stage": "cliente", "company": "Verde Agro"},
    {"first_name": "Camila", "last_name": "Rocha", "email": "camila@pixel.studio", "job_title": "Fundadora", "stage": "lead", "company": "Pixel Studio"},
    {"first_name": "Thiago", "last_name": "Lima", "email": "thiago@acme.com.br", "job_title": "SDR", "stage": "inativo", "company": "Acme Tecnologia"},
]

DEALS = [
    {"title": "Implantação plataforma", "value": 48000, "stage": "proposta", "company": "Acme Tecnologia", "contact": "marina@acme.com.br", "days": 20},
    {"title": "Rebranding completo", "value": 22000, "stage": "negociacao", "company": "Lumina Design", "contact": "rafael@lumina.co", "days": 12},
    {"title": "Contrato logístico anual", "value": 96000, "stage": "qualificado", "company": "Nordeste Logística", "contact": "bia@nordestelog.com", "days": 35},
    {"title": "Consultoria agro 2026", "value": 31000, "stage": "novo", "company": "Verde Agro", "contact": "diego@verdeagro.agr.br", "days": 45},
    {"title": "Pacote de mídia trimestral", "value": 15000, "stage": "ganho", "company": "Pixel Studio", "contact": "camila@pixel.studio", "days": 5},
    {"title": "Expansão de licenças", "value": 27000, "stage": "novo", "company": "Acme Tecnologia", "contact": None, "days": 60},
    {"title": "Automação de campanhas", "value": 18500, "stage": "negociacao", "company": "Lumina Design", "contact": None, "days": 9},
    {"title": "Renovação anual Acme", "value": 36000, "stage": "ganho", "company": "Acme Tecnologia", "contact": None, "days": -12},
    {"title": "Projeto cancelado Verde", "value": 40000, "stage": "perdido", "company": "Verde Agro", "contact": None, "days": -6},
]


class Command(BaseCommand):
    help = "Seed a demo workspace (admin user + workspace + sample CRM data)."

    def handle(self, *args, **options):
        User = get_user_model()
        admin, created = User.objects.get_or_create(
            username="admin",
            defaults={"email": "admin@nucleo.local", "is_staff": True, "is_superuser": True},
        )
        if created:
            admin.set_password("admin")
            admin.save()
            self.stdout.write(self.style.SUCCESS("Usuário criado: admin / admin"))
        else:
            self.stdout.write("Usuário admin já existe.")

        workspace, _ = Workspace.objects.get_or_create(name="Núcleo Demo")
        Membership.objects.get_or_create(
            user=admin, workspace=workspace, defaults={"role": Membership.ROLE_OWNER}
        )

        companies = {}
        for data in COMPANIES:
            obj, _ = Company.objects.get_or_create(
                workspace=workspace, name=data["name"],
                defaults={k: v for k, v in data.items() if k != "name"},
            )
            companies[data["name"]] = obj

        contacts = {}
        for data in CONTACTS:
            d = dict(data)
            company = companies.get(d.pop("company"))
            obj, _ = Contact.objects.get_or_create(
                workspace=workspace, email=d["email"],
                defaults={**d, "company": company},
            )
            contacts[d["email"]] = obj

        for order, data in enumerate(DEALS):
            Deal.objects.get_or_create(
                workspace=workspace, title=data["title"],
                defaults={
                    "value": data["value"],
                    "stage": data["stage"],
                    "order": order,
                    "expected_close": date.today() + timedelta(days=data["days"]),
                    "company": companies.get(data["company"]),
                    "contact": contacts.get(data["contact"]) if data["contact"] else None,
                },
            )

        acme = companies.get("Acme Tecnologia")
        if acme and not acme.activities.exists():
            Activity.objects.create(workspace=workspace, kind="note", author=admin, company=acme,
                                    body="Reunião de kickoff realizada — cliente muito engajado.")
            Activity.objects.create(workspace=workspace, kind="task", author=admin, company=acme,
                                    body="Enviar proposta comercial revisada",
                                    due_date=date.today() + timedelta(days=2))
            Activity.objects.create(workspace=workspace, kind="task", author=admin, company=acme,
                                    body="Follow-up pós-demo (atrasado)",
                                    due_date=date.today() - timedelta(days=1))

        impl = Deal.objects.filter(workspace=workspace, title="Implantação plataforma").first()
        if impl and not impl.activities.exists():
            Activity.objects.create(workspace=workspace, kind="note", author=admin, deal=impl,
                                    body="Decisor confirmou orçamento. Negociação avançando bem.")
            Activity.objects.create(workspace=workspace, kind="task", author=admin, deal=impl,
                                    body="Agendar call de fechamento",
                                    due_date=date.today() + timedelta(days=3))

        # Metadata engine: sample custom fields + values
        def ensure_cf(object_type, key, label, field_type, options=None, order=0):
            obj, _ = CustomField.objects.get_or_create(
                workspace=workspace, object_type=object_type, key=key,
                defaults={"label": label, "field_type": field_type, "options": options or [], "order": order},
            )
            return obj

        ensure_cf("company", "origem", "Origem", "select", ["Indicação", "Inbound", "Outbound", "Evento"], 0)
        ensure_cf("company", "website", "Website", "text", order=1)
        ensure_cf("contact", "linkedin", "LinkedIn", "text", order=0)
        ensure_cf("deal", "probabilidade", "Probabilidade (%)", "number", order=0)

        lumina = companies.get("Lumina Design")
        if acme:
            acme.custom = {"origem": "Inbound", "website": "acme.com.br"}
            acme.save(update_fields=["custom"])
        if lumina:
            lumina.custom = {"origem": "Indicação", "website": "lumina.co"}
            lumina.save(update_fields=["custom"])
        marina = contacts.get("marina@acme.com.br")
        if marina:
            marina.custom = {"linkedin": "linkedin.com/in/marina-alves"}
            marina.save(update_fields=["custom"])
        if impl:
            impl.custom = {"probabilidade": 60}
            impl.save(update_fields=["custom"])

        # Automations (Phase 4)
        Automation.objects.get_or_create(
            workspace=workspace, name="Onboarding ao ganhar negócio",
            defaults={
                "trigger": "deal_stage_changed", "condition_stage": "ganho",
                "action": "create_task", "action_text": "Enviar contrato e iniciar onboarding do cliente",
                "action_due_days": 2, "active": True,
            },
        )
        Automation.objects.get_or_create(
            workspace=workspace, name="Pesquisar nova empresa",
            defaults={
                "trigger": "company_created", "action": "create_task",
                "action_text": "Pesquisar a empresa e enriquecer o cadastro", "action_due_days": 1, "active": True,
            },
        )

        # Fire the "won" automation once for the already-won deal, so the
        # timeline shows a real automation-created task from the start.
        pixel = Deal.objects.filter(workspace=workspace, title="Pacote de mídia trimestral").first()
        if pixel and not pixel.activities.filter(source="automation").exists():
            emit(workspace, "deal_stage_changed", pixel, {"stage": "ganho", "old_stage": "negociacao"})

        # F0 primitives: responsável + etiquetas + score de exemplo
        Company.all_objects.filter(workspace=workspace, owner__isnull=True).update(owner=admin)
        Contact.all_objects.filter(workspace=workspace, owner__isnull=True).update(owner=admin)
        Deal.all_objects.filter(workspace=workspace, owner__isnull=True).update(owner=admin)
        vip, _ = Tag.all_objects.get_or_create(workspace=workspace, name="VIP", defaults={"color": "#7c3aed"})
        inbound, _ = Tag.all_objects.get_or_create(workspace=workspace, name="Inbound", defaults={"color": "#059669"})
        if acme:
            acme.tags.add(vip)
        if lumina:
            lumina.tags.add(inbound)
        if marina:
            marina.score = 85
            marina.save(update_fields=["score"])

        self.stdout.write(self.style.SUCCESS(
            f"Seed pronto no workspace “{workspace.name}”: {workspace.companies.count()} empresas, "
            f"{workspace.contacts.count()} contatos, {workspace.deals.count()} negócios, "
            f"{workspace.activities.count()} atividades, {workspace.custom_fields.count()} campos personalizados, "
            f"{workspace.automations.count()} automações, {workspace.tags.count()} etiquetas."
        ))
