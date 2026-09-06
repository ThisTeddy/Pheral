from django.contrib import admin
from django.urls import include, path

from django.conf import settings
from django.conf.urls.static import static

from pheral import views


urlpatterns = [
    path(
        "admin/",
        admin.site.urls,
    ),

    path(
        "",
        include("pheral.urls"),
    ),

    path(
        "manifest.json",
        views.pwa_manifest,
        name="pwa_manifest",
    ),

    path(
        "service-worker.js",
        views.service_worker,
        name="service_worker",
    ),
]


# ============================================================
# MEDIA FILES — DEVELOPMENT
# ============================================================

if settings.DEBUG:
    urlpatterns += static(
        settings.MEDIA_URL,
        document_root=settings.MEDIA_ROOT,
    )