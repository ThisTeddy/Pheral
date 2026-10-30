import json
import logging
import os

from asgiref.sync import async_to_sync
from channels.db import database_sync_to_async
from channels.generic.websocket import AsyncWebsocketConsumer
from channels.layers import get_channel_layer
from django.urls import reverse
from django.utils import timezone

from .models import Conversation, ConversationParticipant, Message, MessageRead

logger = logging.getLogger("pheral")


# ----------------------------------------------------------------
# Shared helpers
# ----------------------------------------------------------------

_PREVIEWS = {
    "image": "📷 Photo",
    "video": "🎥 Video",
    "voice": "🎤 Voice message",
    "file": "📄 Document",
    "payment": "💸 Payment",
    "hire": "💼 Hire request",
}


def serialize_message(message):
    """
    One place that turns a Message into the JSON the browser expects.
    Sync-only (touches the ORM). Used by the consumer AND by HTTP views
    that save attachments, so every client renders the same payload.
    """
    sender = message.sender
    reply_to = message.reply_to

    attachment_url, file_name = "", ""
    if message.attachment:
        try:
            attachment_url = message.attachment.url
            file_name = os.path.basename(str(message.attachment.name or "")) or "Document"
        except Exception:
            logger.exception("Could not resolve attachment url for message %s", message.pk)

    receipt_url = ""
    try:
        receipt = getattr(message, "receipt", None)
        if receipt is not None:
            receipt_url = reverse("receipt_detail", args=[receipt.reference])
    except Exception:
        pass

    reply_preview = reply_sender = reply_sender_id = None
    if reply_to:
        reply_preview = _PREVIEWS.get(reply_to.message_type) or (reply_to.content or "")[:80]
        if reply_to.sender:
            reply_sender = reply_to.sender.first_name or reply_to.sender.username
            reply_sender_id = reply_to.sender_id

    return {
        "id": message.id,
        "sender_id": message.sender_id,
        "sender_username": sender.username if sender else "",
        "sender_first_name": sender.first_name if sender else "",
        "message_type": message.message_type,
        "content": message.content or "",
        "attachment_url": attachment_url,
        "file_name": file_name,
        "receipt_url": receipt_url,
        # Local time (Africa/Lagos), not UTC.
        "created_at": timezone.localtime(message.created_at).strftime("%I:%M %p").lstrip("0"),
        "reply_to_id": reply_to.id if reply_to else None,
        "reply_to_preview": reply_preview,
        "reply_to_sender": reply_sender,
        "reply_to_sender_id": reply_sender_id,
    }


def broadcast_message(message):
    """
    Call this from any normal (sync) Django view after saving a Message,
    e.g. the HTTP POST that handles attachments / voice notes / payments:

        from .consumers import broadcast_message
        broadcast_message(message)
    """
    layer = get_channel_layer()
    if layer is None:
        return
    try:
        async_to_sync(layer.group_send)(
            f"chat_{message.conversation_id}",
            {"type": "chat_message_event", **serialize_message(message)},
        )
    except Exception:
        logger.exception("broadcast_message failed for message %s", message.pk)


# ----------------------------------------------------------------
# Consumer
# ----------------------------------------------------------------

class ChatConsumer(AsyncWebsocketConsumer):
    """
    Handles three inbound client events (chat_message, typing, read_receipt)
    and fans them out to everyone in the conversation group.

    Text messages go over the socket. Files / voice notes are saved by the
    HTTP view, which then calls broadcast_message().

    connect() accepts the socket FIRST and validates afterwards, so a slow
    database can never leave the browser stuck in "handshaking". Rejected
    clients are closed with 4001 (not logged in) or 4003 (not a participant);
    the page does not retry on codes >= 4000.
    """

    async def connect(self):
        self.user_id = None
        self.username = ""
        self.joined = False
        self.conversation_id = int(self.scope["url_route"]["kwargs"]["conversation_id"])
        self.group_name = f"chat_{self.conversation_id}"

        await self.accept()

        user = self.scope.get("user")
        if user is None or not user.is_authenticated:
            await self.close(code=4001)
            return

        try:
            allowed = await self.user_is_participant(user.id, self.conversation_id)
        except Exception:
            logger.exception("Participant check failed")
            await self.close(code=1011)  # transient: the page will retry
            return

        if not allowed:
            await self.close(code=4003)
            return

        self.user_id = user.id
        self.username = user.username
        await self.channel_layer.group_add(self.group_name, self.channel_name)
        self.joined = True

    async def disconnect(self, close_code):
        if not self.joined:
            return
        self.joined = False
        try:
            # Clear the "typing…" indicator if the tab closes mid-type.
            await self.channel_layer.group_send(
                self.group_name,
                {"type": "typing_event", "user_id": self.user_id, "username": self.username, "typing": False},
            )
        except Exception:
            pass
        await self.channel_layer.group_discard(self.group_name, self.channel_name)

    async def receive(self, text_data=None, bytes_data=None):
        if not self.joined or not text_data:
            return
        try:
            data = json.loads(text_data)
        except (ValueError, TypeError):
            return
        if not isinstance(data, dict):
            return

        try:
            kind = data.get("type")
            if kind == "chat_message":
                await self.handle_chat_message(data)
            elif kind == "typing":
                await self.handle_typing(data)
            elif kind == "read_receipt":
                await self.handle_read_receipt(data)
        except Exception:
            # Never let one bad event kill the socket.
            logger.exception("Error handling websocket event %r", data.get("type"))

    # ---------------- inbound ----------------

    async def handle_chat_message(self, data):
        content = (data.get("content") or "").strip()
        if not content:
            return

        payload = await self.create_message(
            self.user_id, self.conversation_id, content, data.get("reply_to")
        )
        if payload is None:
            return

        # Sent from here (not from a signal), so each message is broadcast exactly once.
        await self.channel_layer.group_send(
            self.group_name, {"type": "chat_message_event", **payload}
        )

    async def handle_typing(self, data):
        await self.channel_layer.group_send(
            self.group_name,
            {
                "type": "typing_event",
                "user_id": self.user_id,
                "username": self.username,
                "typing": bool(data.get("typing")),
            },
        )

    async def handle_read_receipt(self, data):
        try:
            message_id = int(data.get("message_id"))
        except (TypeError, ValueError):
            return

        # Always broadcast: reading N means everything up to N is read, and
        # the sender may have missed the first receipt.
        await self.mark_read(message_id, self.user_id)
        await self.channel_layer.group_send(
            self.group_name,
            {"type": "read_receipt_event", "message_id": message_id, "reader_id": self.user_id},
        )

    # ---------------- outbound (called by Channels per group_send) ----------------

    async def chat_message_event(self, event):
        await self.send(text_data=json.dumps(event))

    async def typing_event(self, event):
        if event.get("user_id") == self.user_id:
            return
        await self.send(text_data=json.dumps(event))

    async def read_receipt_event(self, event):
        if event.get("reader_id") == self.user_id:
            return
        await self.send(text_data=json.dumps(event))

    # ---------------- DB ----------------

    @database_sync_to_async
    def user_is_participant(self, user_id, conversation_id):
        return ConversationParticipant.objects.filter(
            conversation_id=conversation_id, user_id=user_id
        ).exists()

    @database_sync_to_async
    def create_message(self, user_id, conversation_id, content, reply_to_id):
        try:
            conversation = Conversation.objects.get(pk=conversation_id, is_active=True)
        except Conversation.DoesNotExist:
            return None

        reply_to = None
        if reply_to_id:
            reply_to = (
                Message.objects.filter(pk=reply_to_id, conversation=conversation, is_deleted=False)
                .select_related("sender")
                .first()
            )

        message = Message.objects.create(
            conversation=conversation,
            sender_id=user_id,
            message_type=Message.MessageType.TEXT,
            content=content,
            reply_to=reply_to,
        )

        conversation.updated_at = timezone.now()
        conversation.save(update_fields=["updated_at"])

        return serialize_message(message)

    @database_sync_to_async
    def mark_read(self, message_id, user_id):
        message = Message.objects.filter(
            pk=message_id, conversation_id=self.conversation_id
        ).first()
        if message is None or message.sender_id == user_id:
            return False
        _, created = MessageRead.objects.get_or_create(message=message, user_id=user_id)
        return created