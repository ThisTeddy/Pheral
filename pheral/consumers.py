import json

from channels.db import database_sync_to_async
from channels.generic.websocket import AsyncWebsocketConsumer
from django.utils import timezone

from .models import Conversation, ConversationParticipant, Message, MessageRead


class ChatConsumer(AsyncWebsocketConsumer):
    """
    Shared by both direct and group chat — a conversation is a
    conversation either way. Handles three inbound event types from
    the client (chat_message, typing, read_receipt) and broadcasts
    matching outbound events to every other tab in the same group.
    Attachments/voice notes are NOT sent here — see chat.html's
    onFormSubmit, which falls back to a real HTTP POST for anything
    with a file attached.
    """

    async def connect(self):
        self.conversation_id = self.scope["url_route"]["kwargs"]["conversation_id"]
        self.group_name = f"chat_{self.conversation_id}"
        user = self.scope["user"]

        if not user.is_authenticated:
            await self.close(code=4001)
            return

        is_participant = await self.user_is_participant(user.id, self.conversation_id)
        if not is_participant:
            await self.close(code=4003)
            return

        self.user_id = user.id
        self.username = user.username

        await self.channel_layer.group_add(self.group_name, self.channel_name)
        await self.accept()

    async def disconnect(self, close_code):
        if hasattr(self, "group_name"):
            # Let everyone else know this user stopped typing if the
            # tab closes mid-type, so the indicator doesn't get stuck.
            await self.channel_layer.group_send(
                self.group_name,
                {"type": "typing_event", "user_id": self.user_id, "username": self.username, "typing": False},
            )
            await self.channel_layer.group_discard(self.group_name, self.channel_name)

    async def receive(self, text_data):
        try:
            data = json.loads(text_data)
        except (ValueError, TypeError):
            return

        event_type = data.get("type")

        if event_type == "chat_message":
            await self.handle_chat_message(data)
        elif event_type == "typing":
            await self.handle_typing(data)
        elif event_type == "read_receipt":
            await self.handle_read_receipt(data)

    # ------------------------------------------------------------
    # Inbound handlers
    # ------------------------------------------------------------

    async def handle_chat_message(self, data):
        content = (data.get("content") or "").strip()
        reply_to_id = data.get("reply_to")

        if not content:
            return

        message = await self.create_message(self.user_id, self.conversation_id, content, reply_to_id)
        if message is None:
            return

        await self.channel_layer.group_send(
            self.group_name,
            {
                "type": "chat_message_event",
                "id": message["id"],
                "sender_id": message["sender_id"],
                "sender_username": message["sender_username"],
                "sender_first_name": message["sender_first_name"],
                "content": message["content"],
                "created_at": message["created_at"],
                "reply_to_id": message["reply_to_id"],
                "reply_to_preview": message["reply_to_preview"],
                "reply_to_sender": message["reply_to_sender"],
            },
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
        message_id = data.get("message_id")
        if not message_id:
            return

        created = await self.mark_read(message_id, self.user_id)
        if not created:
            return

        await self.channel_layer.group_send(
            self.group_name,
            {
                "type": "read_receipt_event",
                "message_id": message_id,
                "reader_id": self.user_id,
            },
        )

    # ------------------------------------------------------------
    # Outbound — one method per event "type", called by Channels
    # when a group_send with that type lands on this consumer.
    # ------------------------------------------------------------

    async def chat_message_event(self, event):
        await self.send(text_data=json.dumps(event))

    async def typing_event(self, event):
        # Don't echo a user's own typing state back to themselves.
        if event["user_id"] == self.user_id:
            return
        await self.send(text_data=json.dumps(event))

    async def read_receipt_event(self, event):
        # Don't tell a reader that they read their own message.
        if event["reader_id"] == self.user_id:
            return
        await self.send(text_data=json.dumps(event))

    # ------------------------------------------------------------
    # DB access
    # ------------------------------------------------------------

    @database_sync_to_async
    def user_is_participant(self, user_id, conversation_id):
        return ConversationParticipant.objects.filter(
            conversation_id=conversation_id, user_id=user_id,
        ).exists()

    @database_sync_to_async
    def create_message(self, user_id, conversation_id, content, reply_to_id):
        try:
            conversation = Conversation.objects.get(pk=conversation_id, is_active=True)
        except Conversation.DoesNotExist:
            return None

        reply_to = None
        if reply_to_id:
            reply_to = Message.objects.filter(
                pk=reply_to_id, conversation=conversation, is_deleted=False,
            ).select_related("sender").first()

        message = Message.objects.create(
            conversation=conversation, sender_id=user_id,
            message_type=Message.MessageType.TEXT, content=content, reply_to=reply_to,
        )

        conversation.updated_at = timezone.now()
        conversation.save(update_fields=["updated_at"])

        sender = message.sender

        return {
            "id": message.id,
            "sender_id": user_id,
            "sender_username": sender.username if sender else "",
            "sender_first_name": sender.first_name if sender else "",
            "content": message.content,
            "created_at": message.created_at.strftime("%I:%M %p").lstrip("0"),
            "reply_to_id": reply_to.id if reply_to else None,
            "reply_to_preview": (reply_to.content[:60] if reply_to and reply_to.content else "Attachment") if reply_to else None,
            "reply_to_sender": reply_to.sender.username if reply_to and reply_to.sender else None,
        }

    @database_sync_to_async
    def mark_read(self, message_id, user_id):
        try:
            message = Message.objects.get(pk=message_id, conversation_id=self.conversation_id)
        except Message.DoesNotExist:
            return False

        if message.sender_id == user_id:
            return False  # no point marking your own message read

        _, created = MessageRead.objects.get_or_create(message=message, user_id=user_id)
        return created