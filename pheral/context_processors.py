from .models import Notification


def unread_notifications(request):
    """
    Makes `unread_notifications` available in every template, globally —
    base.html's notification bell badge and the sidebar profile menu's badge
    dot both depend on this existing, and it's not something any individual
    view was ever setting. Without this registered as a context processor,
    those badges are dead code that silently never fires.
    """
    if not request.user.is_authenticated:
        return {"unread_notifications": 0}

    return {
        "unread_notifications": Notification.objects.filter(
            user=request.user, is_read=False,
        ).count()
    }