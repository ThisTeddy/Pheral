import os

os.environ.setdefault("DJANGO_SETTINGS_MODULE", "config.settings")  # adjust to your actual settings path

from django.core.asgi import get_asgi_application
django_asgi_app = get_asgi_application()

from channels.auth import AuthMiddlewareStack
from channels.routing import ProtocolTypeRouter, URLRouter

import pheral.routing

application = ProtocolTypeRouter({
    "http": django_asgi_app,
    "websocket": AuthMiddlewareStack(
        URLRouter(pheral.routing.websocket_urlpatterns)
    ),
})