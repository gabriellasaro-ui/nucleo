# Checklist de Produção — Segurança / LGPD

> Coisas **manuais** (fora do código) pra fazer no deploy. O código já está pronto e testado.
> Marque conforme for concluindo.

---

## 1. Variáveis de ambiente no Easypanel ⭐ (o mais importante)

As travas de segurança **só ligam quando o app sabe que está em produção**. Isso se faz na tela
**Environment / Environment Variables** do app no Easypanel (a mesma onde já estão `DATABASE_URL`,
`FACEBOOK_APP_ID`, etc.).

Hoje está assim (inseguro):
```
DJANGO_DEBUG=1
DJANGO_SECRET_KEY=dev-insecure-change-me
```

Deixe assim:

| Variável | Valor | Pra que serve |
|---|---|---|
| `DJANGO_DEBUG` | `0` | Desliga o modo debug (que vaza detalhes internos e desliga proteções). `0` = produção segura. |
| `DJANGO_SECRET_KEY` | *(uma chave única — ver abaixo)* | "Senha mestra" que assina login/cookies. A padrão é pública; troque por uma secreta. |
| `DJANGO_ALLOWED_HOSTS` | `seu.dominio.com` | Domínios autorizados a servir o sistema. |
| `DJANGO_CSRF_ORIGINS` | `https://seu.dominio.com` | Faz formulários/login funcionarem com segurança sob HTTPS. |

**Gerar a chave secreta** (rodar no terminal, na pasta do projeto):
```
python -c "from django.core.management.utils import get_random_secret_key as g; print(g())"
```
Copie o resultado e cole em `DJANGO_SECRET_KEY`.

⚠️ **Importante:** com `DJANGO_DEBUG=0`, o app **exige** um `DJANGO_SECRET_KEY` de verdade — se
ficar o `dev-insecure-change-me`, ele **se recusa a subir** (proposital, pra não subir inseguro).
Por isso troque **as duas juntas**.

**HTTPS forçado (HSTS) — opcional, ligar só quando o HTTPS estiver 100%:**
```
DJANGO_HSTS_SECONDS=3600        # comece baixo (1h) e vá aumentando
DJANGO_HSTS_SUBDOMAINS=1        # (opcional) se todos os subdomínios são HTTPS
DJANGO_HSTS_PRELOAD=1           # (opcional, quase irreversível — só quando tiver certeza)
```

- [ ] `DJANGO_DEBUG=0`
- [ ] `DJANGO_SECRET_KEY` trocado por uma chave gerada
- [ ] `DJANGO_ALLOWED_HOSTS` com o domínio real
- [ ] `DJANGO_CSRF_ORIGINS` com `https://` + domínio real
- [ ] Redeploy

---

## 2. Rotacionar segredos expostos + SSL do banco

Estes valores apareceram no chat — troque por segurança:
- [ ] Senha do Postgres (a do `DATABASE_URL`)
- [ ] `FACEBOOK_APP_SECRET`
- [ ] `FACEBOOK_CLIENT_TOKEN`
- [ ] `EVOGO_GLOBAL_API_KEY`
- [ ] No `DATABASE_URL`, trocar `sslmode=disable` → `sslmode=require` (tráfego do banco cifrado)

---

## 3. Subir os commits

- [ ] `git push` (branch `primeiros_testes`) — é o que o Easypanel puxa no deploy.

---

## 4. Ligar a retenção automática (LGPD)

O expurgo (anonimizar contatos antigos) só roda quando alguém clica em "Aplicar agora"
**ou** por tarefa agendada. Pra automatizar:

- [ ] Definir a janela em **Configurações → Privacidade → Retenção (meses)** (0 = desligado)
- [ ] Agendar no Easypanel uma tarefa diária rodando:
  ```
  python manage.py anonymize_expired
  ```
  (pra testar sem alterar nada: `python manage.py anonymize_expired --dry-run`)

---

## Fase 5 (adiada) — criptografia de PII

Ficou pra depois. Se retomar, a **Opção A** (leve) é: ligar encryption-at-rest do Postgres/disco
+ `sslmode=require`. A **Opção B** (pesada) é cifrar campo a campo no app com blind index — cuidado
porque perder a chave = dado irrecuperável.
