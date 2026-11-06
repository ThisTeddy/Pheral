"""
ASGI entrypoint: HTTP goes to Django, WebSockets go to Channels.

Run with:  daphne -b 0.0.0.0 -p $PORT config.asgi:application
(plain `gunicorn config.wsgi` cannot serve WebSockets)
"""
import os

from django.core.asgi import get_asgi_application

os.environ.setdefault("DJANGO_SETTINGS_MODULE", "config.settings")

# Must run BEFORE importing anything that touches models (routing -> consumers -> models).
django_asgi_app = get_asgi_application()

from channels.auth import AuthMiddlewareStack  # noqa: E402
from channels.routing import ProtocolTypeRouter, URLRouter  # noqa: E402
from channels.security.websocket import AllowedHostsOriginValidator  # noqa: E402

from pheral.routing import websocket_urlpatterns  # noqa: E402

application = ProtocolTypeRouter({
    "http": django_asgi_app,
    "websocket": AllowedHostsOriginValidator(
        AuthMiddlewareStack(URLRouter(websocket_urlpatterns))
    ),
})