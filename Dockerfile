# Núcleo CRM — imagem de produção (Django + gunicorn)
FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PORT=8000

WORKDIR /app

# Dependências Python (psycopg[binary] já traz a libpq, sem apt extra)
COPY requirements.txt .
RUN pip install -r requirements.txt

# Código da aplicação
COPY . .

# O WhiteNoise serve os estáticos direto da pasta static/ (WHITENOISE_USE_FINDERS),
# então não é preciso rodar collectstatic no build.
EXPOSE 8000

# gunicorn na porta $PORT (default 8000). Ajuste --workers conforme a CPU do servidor.
CMD gunicorn config.wsgi --bind 0.0.0.0:${PORT:-8000} --workers 3 --timeout 60
