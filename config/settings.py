"""
Django settings for Pheral.

Development-first configuration.
SQLite is used for now.
Cloudinary is configured through the normal Cloudinary Python SDK.
"""

import os
from pathlib import Path

import cloudinary

import os
from pathlib import Path

import cloudinary
from dotenv import load_dotenv
# ============================================================
# BASE
# ============================================================

BASE_DIR = Path(__file__).resolve().parent.parent

load_dotenv(BASE_DIR / ".env", override=True)
# ============================================================
# SECURITY
# ============================================================

SECRET_KEY = os.environ.get(
    "DJANGO_SECRET_KEY",
    "django-insecure-change-this-before-production",
)

DEBUG = os.environ.get("DJANGO_DEBUG", "True").lower() == "true"

ENVIRONMENT = os.environ.get(
    "DJANGO_ENVIRONMENT",
    "development",
)


# ============================================================
# HOSTS / CSRF
# ============================================================

ALLOWED_HOSTS = [
    "127.0.0.1",
    "localhost",
    "pheral.onrender.com",
    ".ngrok-free.dev",
]

CSRF_TRUSTED_ORIGINS = [
    "https://pheral.onrender.com",
    "https://shivering-unscrew-quaintly.ngrok-free.dev",
]


# ============================================================
# APPLICATIONS
# ============================================================

INSTALLED_APPS = [
    "django.contrib.admin",
    "django.contrib.auth",
    "django.contrib.contenttypes",
    "django.contrib.sessions",
    "django.contrib.messages",
    "django.contrib.staticfiles",

    # Cloudinary
    "cloudinary",

    # Pheral
    "pheral",
]


# ============================================================
# MIDDLEWARE
# ============================================================

MIDDLEWARE = [
    "django.middleware.security.SecurityMiddleware",

    "django.contrib.sessions.middleware.SessionMiddleware",

    "whitenoise.middleware.WhiteNoiseMiddleware",

    "django.middleware.common.CommonMiddleware",

    "django.middleware.csrf.CsrfViewMiddleware",

    "django.contrib.auth.middleware.AuthenticationMiddleware",

    "django.contrib.messages.middleware.MessageMiddleware",

    "django.middleware.clickjacking.XFrameOptionsMiddleware",
]


# ============================================================
# URL CONFIGURATION
# ============================================================

ROOT_URLCONF = "config.urls"


# ============================================================
# TEMPLATES
# ============================================================

TEMPLATES = [
    {
        "BACKEND": "django.template.backends.django.DjangoTemplates",

        "DIRS": [
            BASE_DIR / "templates",
        ],

        "APP_DIRS": True,

        "OPTIONS": {
            "context_processors": [
                "django.template.context_processors.request",

                "django.contrib.auth.context_processors.auth",

                "django.contrib.messages.context_processors.messages",
            ],
        },
    },
]


# ============================================================
# WSGI / ASGI
# ============================================================

WSGI_APPLICATION = "config.wsgi.application"

ASGI_APPLICATION = "config.asgi.application"


# ============================================================
# DATABASE
# ============================================================

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
    {
        "NAME": (
            "django.contrib.auth.password_validation."
            "UserAttributeSimilarityValidator"
        ),
    },

    {
        "NAME": (
            "django.contrib.auth.password_validation."
            "MinimumLengthValidator"
        ),
    },

    {
        "NAME": (
            "django.contrib.auth.password_validation."
            "CommonPasswordValidator"
        ),
    },

    {
        "NAME": (
            "django.contrib.auth.password_validation."
            "NumericPasswordValidator"
        ),
    },
]


# ============================================================
# INTERNATIONALIZATION
# ============================================================

LANGUAGE_CODE = "en-us"

TIME_ZONE = "Africa/Lagos"

USE_I18N = True

USE_TZ = True


# ============================================================
# STATIC FILES
# ============================================================

STATIC_URL = "/static/"

STATICFILES_DIRS = [
    BASE_DIR / "static",
]

STATIC_ROOT = BASE_DIR / "staticfiles"

STATIC_ROOT = BASE_DIR / "staticfiles"

STATICFILES_STORAGE = (
    "whitenoise.storage.CompressedManifestStaticFilesStorage"
)
# ============================================================
# MEDIA FILES
# ============================================================

MEDIA_URL = "/media/"

MEDIA_ROOT = BASE_DIR / "media"


# ============================================================
# CLOUDINARY
# ============================================================

CLOUDINARY_CLOUD_NAME = os.environ.get(
    "CLOUDINARY_CLOUD_NAME",
    "",
)

CLOUDINARY_API_KEY = os.environ.get(
    "CLOUDINARY_API_KEY",
    "",
)

CLOUDINARY_API_SECRET = os.environ.get(
    "CLOUDINARY_API_SECRET",
    "",
)


# Configure Cloudinary directly.
cloudinary.config(
    cloud_name=CLOUDINARY_CLOUD_NAME,
    api_key=CLOUDINARY_API_KEY,
    api_secret=CLOUDINARY_API_SECRET,
    secure=True,
)


# ============================================================
# EMAIL
# ============================================================

EMAIL_BACKEND = os.environ.get(
    "EMAIL_BACKEND",
    "django.core.mail.backends.console.EmailBackend",
)

EMAIL_HOST = os.environ.get(
    "EMAIL_HOST",
    "",
)

EMAIL_PORT = int(
    os.environ.get(
        "EMAIL_PORT",
        "587",
    )
)

EMAIL_USE_TLS = (
    os.environ.get(
        "EMAIL_USE_TLS",
        "True",
    ).lower()
    == "true"
)

EMAIL_HOST_USER = os.environ.get(
    "EMAIL_HOST_USER",
    "",
)

EMAIL_HOST_PASSWORD = os.environ.get(
    "EMAIL_HOST_PASSWORD",
    "",
)

DEFAULT_FROM_EMAIL = os.environ.get(
    "DEFAULT_FROM_EMAIL",
    "Pheral <no-reply@pheral.app>",
)



# ============================================================
# FLUTTERWAVE
# ============================================================

FLW_SECRET_KEY = os.environ.get(
    "FLW_SECRET_KEY",
    "",
)

FLW_SECRET_HASH = os.environ.get(
    "FLW_SECRET_HASH",
    "",
)
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
# SECURITY HEADERS
# ============================================================

SECURE_CONTENT_TYPE_NOSNIFF = True

SECURE_REFERRER_POLICY = "strict-origin-when-cross-origin"

X_FRAME_OPTIONS = "DENY"


# Only force HTTPS outside development.
SECURE_SSL_REDIRECT = not DEBUG


# HSTS
SECURE_HSTS_SECONDS = 0 if DEBUG else 31536000

SECURE_HSTS_INCLUDE_SUBDOMAINS = not DEBUG

SECURE_HSTS_PRELOAD = not DEBUG


# ============================================================
# PROXY / HTTPS
# ============================================================

SECURE_PROXY_SSL_HEADER = (
    "HTTP_X_FORWARDED_PROTO",
    "https",
)


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
# DEFAULT PRIMARY KEY
# ============================================================

DEFAULT_AUTO_FIELD = "django.db.models.BigAutoField"


# ============================================================
# CUSTOM USER MODEL
# ============================================================

AUTH_USER_MODEL = "pheral.User"


# ============================================================
# AUTHENTICATION REDIRECTS
# ============================================================

LOGIN_URL = "/login/"

LOGIN_REDIRECT_URL = "/chat/"

LOGOUT_REDIRECT_URL = "/login/"


# ============================================================
# LOGGING
# ============================================================

LOGGING = {
    "version": 1,

    "disable_existing_loggers": False,

    "handlers": {
        "console": {
            "class": "logging.StreamHandler",
        },
    },

    "root": {
        "handlers": ["console"],
        "level": "INFO",
    },

    "loggers": {
        "django": {
            "handlers": ["console"],
            "level": "INFO",
            "propagate": False,
        },

        "pheral": {
            "handlers": ["console"],
            "level": "DEBUG",
            "propagate": False,
        },
    },
}


# Master OTP bypass — TEMPORARY, for testing before a real SMS
# provider is wired up. REMOVE THIS ENTIRE FEATURE once real
# SMS is live and you have paying users with wallet balances.
# Added: [today's date] — reminder to rip this out.
MASTER_OTP_CODE = "555555"  # change this to your own random string


FLW_PUBLIC_KEY = "FLWPUBK_TEST-7d395f0a3f750ac66e4b302fe069b894-X"