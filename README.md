# Núcleo

Business OS modular — CRM enterprise em **Django + HTMX + Alpine.js**.
Interface branca com detalhes azuis, no padrão Linear/Attio. Repositório limpo,
sem histórico do Twenty (arquivado no branch `archive/twenty-crm` do repo antigo).

## Rodar localmente

```bash
# 1. Ambiente
python -m venv .venv
.venv\Scripts\activate            # Windows (PowerShell/CMD)
pip install -r requirements.txt

# 2. Banco + dados demo
python manage.py migrate
python manage.py seed_demo        # cria admin/admin + empresas e contatos de exemplo

# 3. Servir
python manage.py runserver 127.0.0.1:8001
```

Abra **http://127.0.0.1:8001** e entre com **admin / admin**.

> A porta 8000 costuma estar ocupada por outro projeto na máquina — por isso 8001.

## Estrutura

```
config/            # settings, urls, wsgi/asgi (thin)
core/              # kernel: shell, dashboard, navegação, ⌘K, design system
modules/
  crm/             # primeiro módulo: Empresas + Contatos (CRUD via HTMX)
templates/         # base.html (shell) + telas por módulo
static/            # nucleo.css (design system) + htmx/alpine vendorizados
```

## Banco de dados

- **Padrão:** SQLite (zero config), ótimo para desenvolver.
- **PostgreSQL:** defina `DATABASE_URL` no `.env` (ex.: `postgres://nucleo:nucleo@localhost:5432/nucleo`).
  Necessário para o motor de dados schema-por-workspace previsto no blueprint.

## WhatsApp (EvoGo)

Configure estas variáveis no serviço do Núcleo:

```env
EVOGO_API_URL=https://seu-servidor-evogo.example
EVOGO_GLOBAL_API_KEY=sua-chave-global
# Opcional: fixa a URL pública usada nos webhooks. Sem ela, usa o host da requisição.
NUCLEO_PUBLIC_URL=https://crm.example.com
```

Depois execute `python manage.py migrate_schemas` e abra `/whatsapp/` para gerar
uma instância exclusiva do workspace e conectar o número por QR Code.

## Roadmap

Este é o **Fase 0 — fundação** (app shell + design system + primeiro módulo CRM).
Próximas fases: tenancy + provisionamento de schema por workspace, metadata engine
(campos/objetos dinâmicos), RBAC/ABAC, pipeline de negócios, automações (outbox) e
dashboards. Ver o [blueprint de arquitetura](https://claude.ai/code/artifact/2c4e96af-64f6-4600-b8fb-feb35a480dbf).
