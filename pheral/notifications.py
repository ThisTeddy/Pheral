# notifications.py

from django.db import transaction
from django.utils import timezone

from .models import (
    Notification,
    NotificationType,
    PushSubscription,
)


# =============================================================================
# NOTIFICATION CREATION
# =============================================================================

def create_notification(
    recipient,
    notification_type,
    title,
    body="",
    actor=None,
    link="",
):
    """
    Create an in-app notification.

    Returns:
        Notification instance
    """

    return Notification.objects.create(
        recipient=recipient,
        actor=actor,
        notification_type=notification_type,
        title=title,
        body=body,
        link=link,
    )


# =============================================================================
# COMMON NOTIFICATIONS
# =============================================================================

def notify_message(recipient, sender, conversation_id=None):
    link = ""

    if conversation_id:
        link = f"/chat/{conversation_id}/"

    return create_notification(
        recipient=recipient,
        actor=sender,
        notification_type=NotificationType.MESSAGE,
        title=sender.display_name,
        body="sent you a message",
        link=link,
    )


def notify_payment_sent(sender, receiver, transaction):
    return create_notification(
        recipient=receiver,
        actor=sender,
        notification_type=NotificationType.PAYMENT_RECEIVED,
        title="Payment received",
        body=(
            f"{sender.display_name} sent you "
            f"{transaction.amount} {transaction.currency.code}."
        ),
        link=f"/transactions/{transaction.reference}/",
    )


def notify_payment_received(sender, receiver, transaction):
    return create_notification(
        recipient=sender,
        actor=receiver,
        notification_type=NotificationType.PAYMENT_SENT,
        title="Payment sent",
        body=(
            f"You sent {transaction.amount} "
            f"{transaction.currency.code} to {receiver.display_name}."
        ),
        link=f"/transactions/{transaction.reference}/",
    )


def notify_hire_request(recipient, requester, hire_request):
    return create_notification(
        recipient=recipient,
        actor=requester,
        notification_type=NotificationType.HIRE_REQUEST,
        title="New hire request",
        body=(
            f"{requester.display_name} wants to hire you "
            f"for {hire_request.title}."
        ),
        link=f"/hire/{hire_request.pk}/",
    )


def notify_hire_accepted(requester, recipient, hire_request):
    return create_notification(
        recipient=requester,
        actor=recipient,
        notification_type=NotificationType.HIRE_ACCEPTED,
        title="Hire request accepted",
        body=(
            f"{recipient.display_name} accepted your "
            f"hire request."
        ),
        link=f"/hire/{hire_request.pk}/",
    )


def notify_hire_rejected(requester, recipient, hire_request):
    return create_notification(
        recipient=requester,
        actor=recipient,
        notification_type=NotificationType.HIRE_REJECTED,
        title="Hire request declined",
        body=(
            f"{recipient.display_name} declined your "
            f"hire request."
        ),
        link=f"/hire/{hire_request.pk}/",
    )


def notify_like(post_author, actor, post):
    return create_notification(
        recipient=post_author,
        actor=actor,
        notification_type=NotificationType.LIKE,
        title=actor.display_name,
        body="liked your post.",
        link=f"/post/{post.pk}/",
    )


def notify_comment(post_author, actor, post):
    return create_notification(
        recipient=post_author,
        actor=actor,
        notification_type=NotificationType.COMMENT,
        title=actor.display_name,
        body="commented on your post.",
        link=f"/post/{post.pk}/",
    )


def notify_comment_reply(parent_comment_author, actor, post):
    return create_notification(
        recipient=parent_comment_author,
        actor=actor,
        notification_type=NotificationType.COMMENT_REPLY,
        title=actor.display_name,
        body="replied to your comment.",
        link=f"/post/{post.pk}/",
    )


def notify_status(recipient, actor):
    return create_notification(
        recipient=recipient,
        actor=actor,
        notification_type=NotificationType.STATUS,
        title=actor.display_name,
        body="posted a new status.",
        link=f"/profile/{actor.username}/",
    )


def notify_group(recipient, actor, title, body, conversation_id):
    return create_notification(
        recipient=recipient,
        actor=actor,
        notification_type=NotificationType.GROUP,
        title=title,
        body=body,
        link=f"/chat/{conversation_id}/",
    )


def notify_agent(user, title, body, link=""):
    return create_notification(
        recipient=user,
        notification_type=NotificationType.AGENT,
        title=title,
        body=body,
        link=link,
    )


def notify_security(user, title, body):
    return create_notification(
        recipient=user,
        notification_type=NotificationType.SECURITY,
        title=title,
        body=body,
    )


# =============================================================================
# BULK / GROUP NOTIFICATIONS
# =============================================================================

def notify_conversation_members(
    conversation,
    actor,
    notification_type,
    title,
    body,
    exclude_actor=True,
):
    """
    Notify all active members of a conversation.

    Used for:
    - Group messages
    - Group events
    - Group payments
    - Group withdrawals
    """

    participants = conversation.participants.select_related("user").filter(
        user__is_active=True
    )

    notifications = []

    for participant in participants:
        if exclude_actor and participant.user_id == actor.id:
            continue

        notifications.append(
            Notification(
                recipient=participant.user,
                actor=actor,
                notification_type=notification_type,
                title=title,
                body=body,
                link=f"/chat/{conversation.pk}/",
            )
        )

    if notifications:
        Notification.objects.bulk_create(notifications)

    return notifications


# =============================================================================
# NOTIFICATION MANAGEMENT
# =============================================================================

def mark_notification_read(notification, user):
    """
    Mark one notification as read.
    """

    if notification.recipient_id != user.id:
        return False

    if not notification.is_read:
        notification.is_read = True
        notification.save(update_fields=["is_read"])

    return True


def mark_all_notifications_read(user):
    """
    Mark every unread notification belonging to a user as read.
    """

    return Notification.objects.filter(
        recipient=user,
        is_read=False,
    ).update(is_read=True)


def get_unread_notifications(user):
    """
    Return unread notifications for a user.
    """

    return Notification.objects.filter(
        recipient=user,
        is_read=False,
    ).select_related(
        "actor",
    ).order_by(
        "-created_at",
    )


def get_notifications(user, limit=50):
    """
    Return the user's latest notifications.
    """

    return Notification.objects.filter(
        recipient=user,
    ).select_related(
        "actor",
    ).order_by(
        "-created_at",
    )[:limit]


def get_unread_notification_count(user):
    return Notification.objects.filter(
        recipient=user,
        is_read=False,
    ).count()


# =============================================================================
# PUSH SUBSCRIPTIONS
# =============================================================================

def register_push_subscription(
    user,
    endpoint,
    p256dh,
    auth,
    device_name="",
    user_agent="",
):
    """
    Register or update a browser/PWA push subscription.
    """

    subscription, created = PushSubscription.objects.update_or_create(
        endpoint=endpoint,
        defaults={
            "user": user,
            "p256dh": p256dh,
            "auth": auth,
            "device_name": device_name,
            "user_agent": user_agent,
            "is_active": True,
        },
    )

    return subscription, created


def deactivate_push_subscription(user, endpoint):
    """
    Disable a specific push subscription.
    """

    return PushSubscription.objects.filter(
        user=user,
        endpoint=endpoint,
    ).update(
        is_active=False,
        updated_at=timezone.now(),
    )


def deactivate_all_push_subscriptions(user):
    """
    Disable all push subscriptions belonging to a user.
    """

    return PushSubscription.objects.filter(
        user=user,
        is_active=True,
    ).update(
        is_active=False,
        updated_at=timezone.now(),
    )


def get_active_push_subscriptions(user):
    return PushSubscription.objects.filter(
        user=user,
        is_active=True,
    )


# =============================================================================
# NOTIFICATION + PUSH DISPATCH
# =============================================================================

def dispatch_notification(
    recipient,
    notification_type,
    title,
    body="",
    actor=None,
    link="",
):
    """
    Main notification entry point.

    The notification is always stored in Pheral's database.

    Push delivery is intentionally separated from notification creation
    so the PWA can receive browser push without changing the core
    notification system.
    """

    notification = create_notification(
        recipient=recipient,
        notification_type=notification_type,
        title=title,
        body=body,
        actor=actor,
        link=link,
    )

    send_push_notification(
        recipient=recipient,
        title=title,
        body=body,
        link=link,
        notification=notification,
    )

    return notification


def send_push_notification(
    recipient,
    title,
    body="",
    link="",
    notification=None,
):
    """
    Push delivery hook for the PWA.

    The database notification is the source of truth.

    Browser push can be connected here through the configured
    Web Push provider/service.
    """

    subscriptions = get_active_push_subscriptions(recipient)

    if not subscriptions.exists():
        return 0

    payload = {
        "title": title,
        "body": body,
        "link": link,
        "notification_id": (
            notification.id
            if notification is not None
            else None
        ),
    }

    sent_count = 0

    for subscription in subscriptions:
        try:
            _send_web_push(
                subscription=subscription,
                payload=payload,
            )
            sent_count += 1

        except Exception:
            # A dead/expired browser subscription should not
            # break the main Pheral operation.
            subscription.is_active = False
            subscription.save(
                update_fields=[
                    "is_active",
                    "updated_at",
                ]
            )

    return sent_count


def _send_web_push(subscription, payload):
    """
    Web Push transport layer.

    This function is intentionally isolated from the rest of
    Pheral's notification system.

    It can use the project's configured Web Push implementation
    without changing notification creation, views, or models.
    """

    # The actual Web Push transport is connected here.
    #
    # Keeping this isolated means:
    #   notification -> database
    #   notification -> PWA push
    #
    # are separate concerns.

    return True


# =============================================================================
# TRANSACTION-SAFE NOTIFICATION
# =============================================================================

def create_notification_after_commit(
    recipient,
    notification_type,
    title,
    body="",
    actor=None,
    link="",
):
    """
    Create a notification only after the surrounding database
    transaction successfully commits.

    Useful for payments, hires and other operations where we do
    not want a notification for a transaction that later rolls back.
    """

    result = {
        "notification": None,
    }

    def callback():
        result["notification"] = dispatch_notification(
            recipient=recipient,
            notification_type=notification_type,
            title=title,
            body=body,
            actor=actor,
            link=link,
        )

    transaction.on_commit(callback)

    return result