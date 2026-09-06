# consumers.py

import json

from channels.db import database_sync_to_async
from channels.generic.websocket import AsyncJsonWebsocketConsumer
from django.utils import timezone

from .models import (
    Conversation,
    ConversationParticipant,
    Message,
    MessageRead,
    MessageType,
    User,
)


# =============================================================================
# HELPERS
# =============================================================================

def serialize_message(message):
    return {
        "id": message.id,
        "conversation_id": message.conversation_id,
        "sender": {
            "id": message.sender_id,
            "username": message.sender.username,
            "display_name": message.sender.display_name,
            "avatar_url": message.sender.avatar_url,
        },
        "message_type": message.message_type,
        "content": message.content,
        "media_url": message.media_url,
        "media_type": message.media_type,
        "media_duration": message.media_duration,
        "media_size": message.media_size,
        "transaction_id": message.transaction_id,
        "hire_request_id": message.hire_request_id,
        "agent_action_id": message.agent_action_id,
        "reply_to_id": message.reply_to_id,
        "is_edited": message.is_edited,
        "is_deleted": message.is_deleted,
        "created_at": message.created_at.isoformat(),
        "updated_at": message.updated_at.isoformat(),
    }


# =============================================================================
# CHAT CONSUMER
# =============================================================================

class ChatConsumer(AsyncJsonWebsocketConsumer):
    """
    Main realtime consumer for Pheral conversations.

    Handles:

    - Direct messages
    - Group messages
    - Typing indicators
    - Online presence
    - Read receipts
    - Message deletion
    - Message editing
    - Realtime system events
    """

    async def connect(self):
        self.user = self.scope.get("user")

        if not self.user or self.user.is_anonymous:
            await self.close(code=4001)
            return

        self.conversation_id = self.scope["url_route"]["kwargs"].get(
            "conversation_id"
        )

        if not self.conversation_id:
            await self.close(code=4002)
            return

        is_member = await self.is_conversation_member()

        if not is_member:
            await self.close(code=4003)
            return

        self.room_group_name = (
            f"conversation_{self.conversation_id}"
        )

        await self.channel_layer.group_add(
            self.room_group_name,
            self.channel_name,
        )

        await self.mark_user_online()

        await self.accept()

        await self.send_json(
            {
                "event": "connected",
                "conversation_id": self.conversation_id,
            }
        )

        await self.channel_layer.group_send(
            self.room_group_name,
            {
                "type": "presence_event",
                "event": "user_online",
                "user_id": self.user.id,
            },
        )

    async def disconnect(self, close_code):
        if not hasattr(self, "room_group_name"):
            return

        await self.channel_layer.group_discard(
            self.room_group_name,
            self.channel_name,
        )

        if hasattr(self, "user") and self.user:
            await self.mark_user_offline()

            await self.channel_layer.group_send(
                self.room_group_name,
                {
                    "type": "presence_event",
                    "event": "user_offline",
                    "user_id": self.user.id,
                    "last_seen": timezone.now().isoformat(),
                },
            )

    # =========================================================================
    # RECEIVE
    # =========================================================================

    async def receive_json(self, content, **kwargs):
        event = content.get("event")

        if not event:
            await self.send_error(
                "Missing event."
            )
            return

        handlers = {
            "send_message": self.handle_send_message,
            "typing_start": self.handle_typing_start,
            "typing_stop": self.handle_typing_stop,
            "mark_read": self.handle_mark_read,
            "delete_message": self.handle_delete_message,
            "edit_message": self.handle_edit_message,
            "ping": self.handle_ping,
        }

        handler = handlers.get(event)

        if not handler:
            await self.send_error(
                "Unknown realtime event."
            )
            return

        await handler(content)

    # =========================================================================
    # SEND MESSAGE
    # =========================================================================

    async def handle_send_message(self, content):
        message_content = content.get("content", "").strip()

        message_type = content.get(
            "message_type",
            MessageType.TEXT,
        )

        media_url = content.get("media_url")
        media_type = content.get("media_type", "")
        media_duration = content.get("media_duration")
        media_size = content.get("media_size")

        reply_to_id = content.get("reply_to_id")

        allowed_types = {
            choice[0]
            for choice in MessageType.choices
        }

        if message_type not in allowed_types:
            await self.send_error(
                "Invalid message type."
            )
            return

        if (
            message_type == MessageType.TEXT
            and not message_content
        ):
            await self.send_error(
                "Message cannot be empty."
            )
            return

        if (
            message_type in {
                MessageType.IMAGE,
                MessageType.VIDEO,
                MessageType.VOICE,
                MessageType.FILE,
            }
            and not media_url
        ):
            await self.send_error(
                "Media URL is required."
            )
            return

        message = await self.create_message(
            content=message_content,
            message_type=message_type,
            media_url=media_url,
            media_type=media_type,
            media_duration=media_duration,
            media_size=media_size,
            reply_to_id=reply_to_id,
        )

        if not message:
            await self.send_error(
                "Unable to create message."
            )
            return

        message_data = await self.get_message_data(
            message.id
        )

        await self.channel_layer.group_send(
            self.room_group_name,
            {
                "type": "message_event",
                "event": "new_message",
                "message": message_data,
            },
        )

    # =========================================================================
    # TYPING
    # =========================================================================

    async def handle_typing_start(self, content):
        await self.channel_layer.group_send(
            self.room_group_name,
            {
                "type": "typing_event",
                "event": "typing_start",
                "user_id": self.user.id,
                "username": self.user.username,
            },
        )

    async def handle_typing_stop(self, content):
        await self.channel_layer.group_send(
            self.room_group_name,
            {
                "type": "typing_event",
                "event": "typing_stop",
                "user_id": self.user.id,
                "username": self.user.username,
            },
        )

    # =========================================================================
    # READ RECEIPTS
    # =========================================================================

    async def handle_mark_read(self, content):
        message_id = content.get("message_id")

        if not message_id:
            await self.send_error(
                "message_id is required."
            )
            return

        message = await self.get_message(
            message_id
        )

        if not message:
            await self.send_error(
                "Message not found."
            )
            return

        if message.conversation_id != int(
            self.conversation_id
        ):
            await self.send_error(
                "Message does not belong to this conversation."
            )
            return

        read = await self.mark_message_read(
            message_id
        )

        if not read:
            return

        await self.channel_layer.group_send(
            self.room_group_name,
            {
                "type": "read_event",
                "event": "message_read",
                "message_id": message_id,
                "user_id": self.user.id,
                "read_at": timezone.now().isoformat(),
            },
        )

    # =========================================================================
    # EDIT MESSAGE
    # =========================================================================

    async def handle_edit_message(self, content):
        message_id = content.get("message_id")
        new_content = content.get("content", "").strip()

        if not message_id:
            await self.send_error(
                "message_id is required."
            )
            return

        if not new_content:
            await self.send_error(
                "Message cannot be empty."
            )
            return

        message = await self.edit_message(
            message_id,
            new_content,
        )

        if not message:
            await self.send_error(
                "Message cannot be edited."
            )
            return

        message_data = await self.get_message_data(
            message.id
        )

        await self.channel_layer.group_send(
            self.room_group_name,
            {
                "type": "message_event",
                "event": "message_edited",
                "message": message_data,
            },
        )

    # =========================================================================
    # DELETE MESSAGE
    # =========================================================================

    async def handle_delete_message(self, content):
        message_id = content.get("message_id")

        if not message_id:
            await self.send_error(
                "message_id is required."
            )
            return

        deleted = await self.delete_message(
            message_id
        )

        if not deleted:
            await self.send_error(
                "Message cannot be deleted."
            )
            return

        await self.channel_layer.group_send(
            self.room_group_name,
            {
                "type": "message_event",
                "event": "message_deleted",
                "message_id": message_id,
                "deleted_by": self.user.id,
            },
        )

    # =========================================================================
    # PING
    # =========================================================================

    async def handle_ping(self, content):
        await self.send_json(
            {
                "event": "pong",
                "timestamp": timezone.now().isoformat(),
            }
        )

    # =========================================================================
    # CHANNEL LAYER EVENT HANDLERS
    # =========================================================================

    async def message_event(self, event):
        await self.send_json(
            {
                "event": event["event"],
                "message": event.get("message"),
                "message_id": event.get("message_id"),
                "deleted_by": event.get("deleted_by"),
            }
        )

    async def typing_event(self, event):
        # Don't send a user's own typing indicator back to them.
        if event["user_id"] == self.user.id:
            return

        await self.send_json(
            {
                "event": event["event"],
                "user_id": event["user_id"],
                "username": event["username"],
            }
        )

    async def read_event(self, event):
        await self.send_json(
            {
                "event": event["event"],
                "message_id": event["message_id"],
                "user_id": event["user_id"],
                "read_at": event["read_at"],
            }
        )

    async def presence_event(self, event):
        # Don't send the user's own presence event back to them.
        if event["user_id"] == self.user.id:
            return

        await self.send_json(
            {
                "event": event["event"],
                "user_id": event["user_id"],
                "last_seen": event.get("last_seen"),
            }
        )

    # =========================================================================
    # DATABASE
    # =========================================================================

    @database_sync_to_async
    def is_conversation_member(self):
        return ConversationParticipant.objects.filter(
            conversation_id=self.conversation_id,
            user=self.user,
        ).exists()

    @database_sync_to_async
    def create_message(
        self,
        content,
        message_type,
        media_url=None,
        media_type="",
        media_duration=None,
        media_size=None,
        reply_to_id=None,
    ):
        conversation = Conversation.objects.filter(
            id=self.conversation_id,
        ).first()

        if not conversation:
            return None

        reply_to = None

        if reply_to_id:
            reply_to = Message.objects.filter(
                id=reply_to_id,
                conversation=conversation,
            ).first()

        return Message.objects.create(
            conversation=conversation,
            sender=self.user,
            message_type=message_type,
            content=content,
            media_url=media_url,
            media_type=media_type,
            media_duration=media_duration,
            media_size=media_size,
            reply_to=reply_to,
        )

    @database_sync_to_async
    def get_message(self, message_id):
        return Message.objects.filter(
            id=message_id,
        ).first()

    @database_sync_to_async
    def get_message_data(self, message_id):
        message = (
            Message.objects
            .select_related(
                "sender",
            )
            .filter(id=message_id)
            .first()
        )

        if not message:
            return None

        return serialize_message(message)

    @database_sync_to_async
    def mark_message_read(self, message_id):
        message = Message.objects.filter(
            id=message_id,
            conversation_id=self.conversation_id,
        ).first()

        if not message:
            return False

        MessageRead.objects.get_or_create(
            message=message,
            user=self.user,
        )

        ConversationParticipant.objects.filter(
            conversation_id=self.conversation_id,
            user=self.user,
        ).update(
            last_read_at=timezone.now()
        )

        return True

    @database_sync_to_async
    def edit_message(self, message_id, new_content):
        message = Message.objects.filter(
            id=message_id,
            conversation_id=self.conversation_id,
            sender=self.user,
            is_deleted=False,
        ).first()

        if not message:
            return None

        # Only text messages can be edited.
        if message.message_type != MessageType.TEXT:
            return None

        message.content = new_content
        message.is_edited = True
        message.save(
            update_fields=[
                "content",
                "is_edited",
                "updated_at",
            ]
        )

        return message

    @database_sync_to_async
    def delete_message(self, message_id):
        message = Message.objects.filter(
            id=message_id,
            conversation_id=self.conversation_id,
            sender=self.user,
            is_deleted=False,
        ).first()

        if not message:
            return False

        message.is_deleted = True
        message.content = ""
        message.media_url = None

        message.save(
            update_fields=[
                "is_deleted",
                "content",
                "media_url",
                "updated_at",
            ]
        )

        return True

    @database_sync_to_async
    def mark_user_online(self):
        User.objects.filter(
            id=self.user.id
        ).update(
            is_online=True,
            last_seen=timezone.now(),
        )

    @database_sync_to_async
    def mark_user_offline(self):
        User.objects.filter(
            id=self.user.id
        ).update(
            is_online=False,
            last_seen=timezone.now(),
        )

    # =========================================================================
    # ERROR
    # =========================================================================

    async def send_error(self, message):
        await self.send_json(
            {
                "event": "error",
                "message": message,
            }
        )