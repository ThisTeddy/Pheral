"""
Django settings for Pheral.

Local development: SQLite + in-memory channel layer, plain http://localhost.
Production (Render etc.): set DATABASE_URL, REDIS_URL and DJANGO_DEBUG=False.

Put local values in a .env file next to manage.py, e.g.:
    DJANGO_DEBUG=True
    DJANGO_SECRET_KEY=something-long-and-random
"""

import os
from pathlib import Path

import cloudinary
from dotenv import load_dotenv

# ============================================================
# BASE
# ============================================================

BASE_DIR = Path(__file__).resolve().parent.parent

load_dotenv(BASE_DIR / ".env", override=True)


def env_bool(name, default=False):
    return os.environ.get(name, str(default)).strip().lower() in ("1", "true", "yes", "on")


def env_list(name):
    return [item.strip() for item in os.environ.get(name, "").split(",") if item.strip()]


# ============================================================
# SECURITY
# ============================================================

SECRET_KEY = os.environ.get(
    "DJANGO_SECRET_KEY",
    "django-insecure-change-this-before-production",
)

# Safe default: OFF. Set DJANGO_DEBUG=True in your local .env.
DEBUG = env_bool("DJANGO_DEBUG", False)

ENVIRONMENT = os.environ.get("DJANGO_ENVIRONMENT", "development")


# ============================================================
# HOSTS / CSRF
# (the WebSocket origin check also uses ALLOWED_HOSTS)
# ============================================================

ALLOWED_HOSTS = [
    "127.0.0.1",
    "localhost",
    "pheral.onrender.com",
    "pheral-o94i.onrender.com",
    ".ngrok-free.dev",
    "pheral.pxxlspace.cv",
] + env_list("DJANGO_ALLOWED_HOSTS")

CSRF_TRUSTED_ORIGINS = [
    "https://pheral.onrender.com",
    "https://pheral-o94i.onrender.com",
    "https://shivering-unscrew-quaintly.ngrok-free.dev",
    "https://pheral.pxxlspace.cv",
] + env_list("DJANGO_CSRF_TRUSTED_ORIGINS")


# ============================================================
# APPLICATIONS
# ============================================================

INSTALLED_APPS = [
    "daphne",  # must be first: makes `runserver` speak ASGI (WebSockets)

    "django.contrib.admin",
    "django.contrib.auth",
    "django.contrib.contenttypes",
    "django.contrib.sessions",
    "django.contrib.messages",
    "django.contrib.staticfiles",

    "channels",
    "cloudinary",

    "pheral",
]


# ============================================================
# MIDDLEWARE
# ============================================================

MIDDLEWARE = [
    "django.middleware.security.SecurityMiddleware",
    "whitenoise.middleware.WhiteNoiseMiddleware",
    "django.contrib.sessions.middleware.SessionMiddleware",
    "django.middleware.common.CommonMiddleware",
    "django.middleware.csrf.CsrfViewMiddleware",
    "django.contrib.auth.middleware.AuthenticationMiddleware",
    "django.contrib.messages.middleware.MessageMiddleware",
    "django.middleware.clickjacking.XFrameOptionsMiddleware",
]


# ============================================================
# URLS / TEMPLATES
# ============================================================

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
                "pheral.context_processors.unread_notifications",
            ],
        },
    },
]


# ============================================================
# WSGI / ASGI / CHANNELS
# ============================================================

WSGI_APPLICATION = "config.wsgi.application"
ASGI_APPLICATION = "config.asgi.application"

REDIS_URL = os.environ.get("REDIS_URL", "")

if REDIS_URL:
    CHANNEL_LAYERS = {
        "default": {
            "BACKEND": "channels_redis.core.RedisChannelLayer",
            "CONFIG": {"hosts": [REDIS_URL]},
        }
    }
else:
    # Development only: works inside ONE process. With 2+ workers/instances
    # (Render scaling, gunicorn workers) messages will not cross between them.
    CHANNEL_LAYERS = {
        "default": {"BACKEND": "channels.layers.InMemoryChannelLayer"},
    }


# ============================================================
# DATABASE
# ============================================================

DATABASE_URL = os.environ.get("DATABASE_URL", "")

if DATABASE_URL:
    import dj_database_url

    DATABASES = {
        "default": dj_database_url.parse(
            DATABASE_URL,
            conn_max_age=0,           # no long-lived connections (Channels uses threads)
            conn_health_checks=True,  # drop dead connections before reuse
            ssl_require=True,
        )
    }
    DATABASES["default"].setdefault("OPTIONS", {}).update({
        "keepalives": 1,
        "keepalives_idle": 30,
        "keepalives_interval": 10,
        "keepalives_count": 5,
    })
else:
    DATABASES = {
        "default": {
            "ENGINE": "django.db.backends.sqlite3",
            "NAME": BASE_DIR / "db.sqlite3",
        }
    }


# ============================================================
# PASSWORD VALIDATION
# ============================================================

AUTH_PASSWORD_VALIDATORS = [
    {"NAME": "django.contrib.auth.password_validation.UserAttributeSimilarityValidator"},
    {"NAME": "django.contrib.auth.password_validation.MinimumLengthValidator"},
    {"NAME": "django.contrib.auth.password_validation.CommonPasswordValidator"},
    {"NAME": "django.contrib.auth.password_validation.NumericPasswordValidator"},
]


# ============================================================
# INTERNATIONALIZATION
# ============================================================

LANGUAGE_CODE = "en-us"
TIME_ZONE = "Africa/Lagos"
USE_I18N = True
USE_TZ = True


# ============================================================
# STATIC / MEDIA FILES
# ============================================================

STATIC_URL = "/static/"
STATICFILES_DIRS = [BASE_DIR / "static"]
STATIC_ROOT = BASE_DIR / "staticfiles"

# The manifest storage fails on any missing static file, which is annoying in
# development, so it is only used when DEBUG is off.
STORAGES = {
    "default": {"BACKEND": "django.core.files.storage.FileSystemStorage"},
    "staticfiles": {
        "BACKEND": (
            "django.contrib.staticfiles.storage.StaticFilesStorage"
            if DEBUG
            else "whitenoise.storage.CompressedManifestStaticFilesStorage"
        ),
    },
}

MEDIA_URL = "/media/"
MEDIA_ROOT = BASE_DIR / "media"


# ============================================================
# CLOUDINARY
# ============================================================

CLOUDINARY_CLOUD_NAME = os.environ.get("CLOUDINARY_CLOUD_NAME", "")
CLOUDINARY_API_KEY = os.environ.get("CLOUDINARY_API_KEY", "")
CLOUDINARY_API_SECRET = os.environ.get("CLOUDINARY_API_SECRET", "")

cloudinary.config(
    cloud_name=CLOUDINARY_CLOUD_NAME,
    api_key=CLOUDINARY_API_KEY,
    api_secret=CLOUDINARY_API_SECRET,
    secure=True,
)


# ============================================================
# EMAIL
# ============================================================

EMAIL_BACKEND = os.environ.get("EMAIL_BACKEND", "django.core.mail.backends.console.EmailBackend")
EMAIL_HOST = os.environ.get("EMAIL_HOST", "")
EMAIL_PORT = int(os.environ.get("EMAIL_PORT", "587"))
EMAIL_USE_TLS = env_bool("EMAIL_USE_TLS", True)
EMAIL_HOST_USER = os.environ.get("EMAIL_HOST_USER", "")
EMAIL_HOST_PASSWORD = os.environ.get("EMAIL_HOST_PASSWORD", "")
DEFAULT_FROM_EMAIL = os.environ.get("DEFAULT_FROM_EMAIL", "Pheral <no-reply@pheral.app>")


# ============================================================
# FLUTTERWAVE
# ============================================================

FLW_PUBLIC_KEY = os.environ.get("FLW_PUBLIC_KEY", "")
FLW_SECRET_KEY = os.environ.get("FLW_SECRET_KEY", "")

# views.py reads FLW_WEBHOOK_HASH. FLW_SECRET_HASH is accepted as the old name,
# so your existing .env keeps working. Must equal the "secret hash" you set in
# the Flutterwave dashboard under Settings > Webhooks.
FLW_WEBHOOK_HASH = os.environ.get("FLW_WEBHOOK_HASH") or os.environ.get("FLW_SECRET_HASH", "")

# Optional static-IP proxy for calls Flutterwave requires from a whitelisted IP.
FLW_PROXY_URL = os.environ.get("FLW_PROXY_URL", "")

# SANDBOX ONLY. A suffix like "_PMCKDU_1" makes test transfers succeed. It must
# be EMPTY in production, or real transfer references get corrupted. Set it in
# your local .env only.
FLW_SANDBOX_REFERENCE_SUFFIX = os.environ.get("FLW_SANDBOX_REFERENCE_SUFFIX", "")

# Flat NGN fee added to bank transfers / withdrawals.
WITHDRAWAL_FEE = os.environ.get("WITHDRAWAL_FEE", "0")

# Percent of each currency conversion Pheral keeps (views.py reads this).
FX_MARGIN_PERCENT = os.environ.get("FX_MARGIN_PERCENT", "1.5")
FX_QUOTE_TTL_SECONDS = int(os.environ.get("FX_QUOTE_TTL_SECONDS", "45"))


# ============================================================
# SESSIONS / COOKIES
# ============================================================

SESSION_COOKIE_HTTPONLY = True
SESSION_COOKIE_SAMESITE = "Lax"
SESSION_COOKIE_SECURE = not DEBUG

CSRF_COOKIE_HTTPONLY = False
CSRF_COOKIE_SAMESITE = "Lax"
CSRF_COOKIE_SECURE = not DEBUG


# ============================================================
# SECURITY HEADERS / HTTPS
# ============================================================

SECURE_CONTENT_TYPE_NOSNIFF = True
SECURE_REFERRER_POLICY = "strict-origin-when-cross-origin"
X_FRAME_OPTIONS = "DENY"

# Only force HTTPS outside development (plain http://localhost must keep working).
SECURE_SSL_REDIRECT = not DEBUG
SECURE_HSTS_SECONDS = 0 if DEBUG else 31536000
SECURE_HSTS_INCLUDE_SUBDOMAINS = not DEBUG
SECURE_HSTS_PRELOAD = not DEBUG

# Render / ngrok terminate TLS and forward this header.
SECURE_PROXY_SSL_HEADER = ("HTTP_X_FORWARDED_PROTO", "https")


# ============================================================
# PWA
# ============================================================

PWA_APP_NAME = "Pheral"
PWA_APP_SHORT_NAME = "Pheral"
PWA_APP_DESCRIPTION = "Chat, Pay and Hire."
PWA_APP_THEME_COLOR = "#6C3BFF"
PWA_APP_BACKGROUND_COLOR = "#FFFFFF"
PWA_APP_DISPLAY = "standalone"


# ============================================================
# MODELS / AUTH
# ============================================================

DEFAULT_AUTO_FIELD = "django.db.models.BigAutoField"
AUTH_USER_MODEL = "pheral.User"

LOGIN_URL = "/login/"
LOGIN_REDIRECT_URL = "/chat/"
LOGOUT_REDIRECT_URL = "/login/"


# ============================================================
# TEMPORARY: master OTP bypass for testing before real SMS exists.
# Read from the environment so it is never committed. Leave it UNSET in
# production: anyone who knows the code can verify ANY phone number with it.
# Delete this feature once a real SMS provider is live.
# ============================================================

MASTER_OTP_CODE = os.environ.get("MASTER_OTP_CODE", "")


# ============================================================
# LOGGING
# ============================================================

LOGGING = {
    "version": 1,
    "disable_existing_loggers": False,
    "handlers": {"console": {"class": "logging.StreamHandler"}},
    "root": {"handlers": ["console"], "level": "INFO"},
    "loggers": {
        "django": {"handlers": ["console"], "level": "INFO", "propagate": False},
        "pheral": {"handlers": ["console"], "level": "DEBUG", "propagate": False},
    },
}