"""
ASGI config for config project.

It exposes the ASGI callable as a module-level variable named ``application``.
"""

import os

from channels.auth import AuthMiddlewareStack
from channels.routing import ProtocolTypeRouter, URLRouter
from django.core.asgi import get_asgi_application

from .routing import websocket_urlpatterns


os.environ.setdefault(
    "DJANGO_SETTINGS_MODULE",
    "config.settings",
)


django_asgi_application = get_asgi_application()


application = ProtocolTypeRouter(
    {
        "http": django_asgi_application,

        "websocket": AuthMiddlewareStack(
            URLRouter(
                websocket_urlpatterns
            )
        ),
    }
)