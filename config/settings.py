"""
Django settings for Núcleo.

Modular monolith: a small kernel (`core`) plus feature modules under
`modules/*`. Config stays deliberately thin and environment-driven.
"""
from pathlib import Path
from urllib.parse import urlparse

BASE_DIR = Path(__file__).resolve().parent.parent


def env(key, default=None):
    import os
    return os.environ.get(key, default)


# Load a local .env if present (tiny parser, no dependency).
_env_file = BASE_DIR / ".env"
if _env_file.exists():
    import os
    for _line in _env_file.read_text(encoding="utf-8").splitlines():
        _line = _line.strip()
        if not _line or _line.startswith("#") or "=" not in _line:
            continue
        _k, _, _v = _line.partition("=")
        os.environ.setdefault(_k.strip(), _v.strip())


SECRET_KEY = env("DJANGO_SECRET_KEY", "dev-insecure-change-me")
DEBUG = env("DJANGO_DEBUG", "1") == "1"
# Set these in production (Docker / self-hosted) via env vars, e.g.:
#   DJANGO_ALLOWED_HOSTS=crm.gabriellasaro.cloud
#   DJANGO_CSRF_ORIGINS=https://crm.gabriellasaro.cloud
ALLOWED_HOSTS = env("DJANGO_ALLOWED_HOSTS", "localhost,127.0.0.1").split(",")
CSRF_TRUSTED_ORIGINS = [o for o in env("DJANGO_CSRF_ORIGINS", "").split(",") if o]

# Multi-tenant (schema-per-workspace via django-tenants).
# SHARED_APPS live in the `public` schema (auth, the tenant model, shared config).
# TENANT_APPS get their tables created inside each workspace's own schema.
SHARED_APPS = [
    "django_tenants",
    "core",  # holds the tenant model (Workspace) + Domain + shared config
    "django.contrib.admin",
    "django.contrib.auth",
    "django.contrib.contenttypes",
    "django.contrib.sessions",
    "django.contrib.messages",
    "django.contrib.staticfiles",
    "django.contrib.humanize",
]
TENANT_APPS = [
    "django.contrib.contenttypes",
    "modules.crm",  # per-workspace CRM data lives in each tenant schema
]
INSTALLED_APPS = list(SHARED_APPS) + [a for a in TENANT_APPS if a not in SHARED_APPS]

TENANT_MODEL = "core.Workspace"
TENANT_DOMAIN_MODEL = "core.Domain"
DATABASE_ROUTERS = ("django_tenants.routers.TenantSyncRouter",)

MIDDLEWARE = [
    "django.middleware.security.SecurityMiddleware",
    "whitenoise.middleware.WhiteNoiseMiddleware",  # serve static files (CSS/JS) in prod
    "django.contrib.sessions.middleware.SessionMiddleware",
    "django.middleware.common.CommonMiddleware",
    "django.middleware.csrf.CsrfViewMiddleware",
    "django.contrib.auth.middleware.AuthenticationMiddleware",
    "core.middleware.WorkspaceMiddleware",
    "django.contrib.messages.middleware.MessageMiddleware",
    "django.middleware.clickjacking.XFrameOptionsMiddleware",
]

ROOT_URLCONF = "config.urls"

TEMPLATES = [
    {
        "BACKEND": "django.template.backends.django.DjangoTemplates",
        "DIRS": [BASE_DIR / "templates"],
        "APP_DIRS": True,
        "OPTIONS": {
            "context_processors": [
                "django.template.context_processors.request",
                "django.contrib.auth.context_processors.auth",
                "django.contrib.messages.context_processors.messages",
                "core.context_processors.navigation",
                "core.context_processors.branding",
            ],
        },
    },
]

WSGI_APPLICATION = "config.wsgi.application"
ASGI_APPLICATION = "config.asgi.application"


# Database: SQLite by default; PostgreSQL when DATABASE_URL is set.
def _database_from_url(url):
    parsed = urlparse(url)
    return {
        "ENGINE": "django_tenants.postgresql_backend",
        "NAME": parsed.path.lstrip("/"),
        "USER": parsed.username or "",
        "PASSWORD": parsed.password or "",
        "HOST": parsed.hostname or "",
        "PORT": str(parsed.port or ""),
    }


_database_url = env("DATABASE_URL")
if _database_url:
    DATABASES = {"default": _database_from_url(_database_url)}
else:
    DATABASES = {
        "default": {
            "ENGINE": "django.db.backends.sqlite3",
            "NAME": BASE_DIR / "db.sqlite3",
        }
    }

# Tests run against a throwaway `test_nucleodb` database that Django creates and
# drops around the suite — NEVER the real nucleodb, and NEVER MunduDB. The
# django-tenants runner sets up the public schema and a disposable test tenant so
# schema-per-workspace behaviour can be exercised.
if _database_url:
    DATABASES["default"].setdefault("TEST", {})["NAME"] = "test_nucleodb"

AUTH_PASSWORD_VALIDATORS = [
    {"NAME": "django.contrib.auth.password_validation.UserAttributeSimilarityValidator"},
    {"NAME": "django.contrib.auth.password_validation.MinimumLengthValidator"},
    {"NAME": "django.contrib.auth.password_validation.CommonPasswordValidator"},
    {"NAME": "django.contrib.auth.password_validation.NumericPasswordValidator"},
]

LANGUAGE_CODE = "pt-br"
TIME_ZONE = "America/Sao_Paulo"
USE_I18N = True
USE_TZ = True

STATIC_URL = "static/"
STATICFILES_DIRS = [BASE_DIR / "static"]
STATIC_ROOT = BASE_DIR / "staticfiles"
# Serve straight from the finders (the static/ dir) so static works without a
# collectstatic build step (handy for the Docker image).
WHITENOISE_USE_FINDERS = True
STORAGES = {
    "default": {"BACKEND": "django.core.files.storage.FileSystemStorage"},
    "staticfiles": {"BACKEND": "whitenoise.storage.CompressedStaticFilesStorage"},
}

MEDIA_URL = "media/"
MEDIA_ROOT = BASE_DIR / "media"

DEFAULT_AUTO_FIELD = "django.db.models.BigAutoField"

LOGIN_URL = "login"
LOGIN_REDIRECT_URL = "dashboard"
LOGOUT_REDIRECT_URL = "login"

MESSAGE_TAGS = {
    10: "debug",
    20: "info",
    25: "success",
    30: "warning",
    40: "error",
}

# Facebook Lead Ads — OAuth "connect page" flow. Fill these from your Meta app
# (developers.facebook.com). Without them, the Connect button explains what's missing.
FACEBOOK_APP_ID = env("FACEBOOK_APP_ID", "")
FACEBOOK_APP_SECRET = env("FACEBOOK_APP_SECRET", "")
FACEBOOK_GRAPH_VERSION = env("FACEBOOK_GRAPH_VERSION", "v19.0")
