from django.urls import re_path

from .consumers import ChatConsumer, GigsConsumer

websocket_urlpatterns = [
    re_path(r"^ws/chat/(?P<conversation_id>\d+)/$", ChatConsumer.as_asgi()),
    re_path(r"^ws/gigs/$", GigsConsumer.as_asgi()),
]