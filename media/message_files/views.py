from decimal import Decimal, InvalidOperation

from django.contrib import messages
from django.contrib.auth import authenticate, login, logout
from django.contrib.auth.decorators import login_required
from django.db import transaction
from django.db.models import Q, Count
from django.http import JsonResponse, Http404
from django.shortcuts import get_object_or_404, redirect, render
from django.utils import timezone
from django.db import IntegrityError, transaction
import random

from django.contrib import messages
from django.contrib.auth import authenticate, login
from django.contrib.auth.decorators import login_required
from django.db.models import Q
from django.shortcuts import get_object_or_404, redirect, render
from django.utils import timezone
from django.db.models import Q, Prefetch
from .models import User, PhoneOTP

from .models import (
    User,
    PhoneOTP,
    Currency,
    ExchangeRate,
    Wallet,
    WalletToken,
    PheralTransaction,
    LedgerEntry,
    Receipt,
    Conversation,
    ConversationParticipant,
    Message,
    MessageRead,
    GroupLedger,
    GroupLedgerEntry,
    Status,
    StatusView,
    Post,
    PostLike,
    PostComment,
    HireJob,
    HireRequest,
    HirePayment,
    Notification,
    PushDevice,
    AgentCommand,
    AgentActivity,
    Contact,
)


# ============================================================
# HELPERS
# ============================================================

def get_or_create_wallet(user, currency):
    wallet, _ = Wallet.objects.get_or_create(
        user=user,
        currency=currency,
        defaults={
            "balance": Decimal("0.00"),
        },
    )
    return wallet


def get_default_currency():
    """
    Prefer NGN for the Nigerian MVP.
    Falls back to the first active currency.
    """
    currency = Currency.objects.filter(
        code__iexact="NGN",
        is_active=True,
    ).first()

    if currency:
        return currency

    return Currency.objects.filter(
        is_active=True
    ).first()


def get_or_create_wallet_token(user):
    token, _ = WalletToken.objects.get_or_create(
        user=user,
        defaults={
            "is_active": True,
        },
    )

    if not token.is_active:
        token.is_active = True
        token.save(update_fields=["is_active"])

    return token


def create_transaction_reference():
    """
    PheralTransaction already generates references automatically.
    This helper exists only to keep transaction creation centralized
    inside this views file.
    """
    return None


def parse_amount(value):
    try:
        amount = Decimal(str(value))
    except (InvalidOperation, TypeError, ValueError):
        return None

    if amount <= Decimal("0"):
        return None

    return amount

from django.db import transaction
from django.db.models import Count
def get_direct_conversation(user, other_user):
    """
    Find an existing direct conversation between two users.
    """
    conversation = (
        Conversation.objects.filter(
            conversation_type=Conversation.ConversationType.DIRECT,
            participants__user=user,
        )
        .filter(
            participants__user=other_user,
        )
        .annotate(
            participant_count=Count("participants")
        )
        .filter(participant_count=2)
        .first()
    )

    return conversation

from django.db import transaction
from django.db.models import Count


def get_or_create_direct_conversation(user1, user2):

    if user1 == user2:
        raise ValueError(
            "A user cannot create a conversation with themselves."
        )

    conversation = (
        Conversation.objects
        .filter(
            conversation_type=Conversation.ConversationType.DIRECT,
            participants__user=user1,
        )
        .filter(
            participants__user=user2,
        )
        .annotate(
            participant_count=Count("participants"),
        )
        .filter(
            participant_count=2,
        )
        .order_by(
            "created_at",
            "id",
        )
        .first()
    )

    if conversation:
        return conversation

    with transaction.atomic():

        conversation = Conversation.objects.create(
            conversation_type=Conversation.ConversationType.DIRECT,
            created_by=user1,
        )

        ConversationParticipant.objects.create(
            conversation=conversation,
            user=user1,
        )

        ConversationParticipant.objects.create(
            conversation=conversation,
            user=user2,
        )

    return conversation

def create_system_message(conversation, content):
    return Message.objects.create(
        conversation=conversation,
        sender=None,
        message_type=Message.MessageType.SYSTEM,
        content=content,
    )


# ============================================================
# LANDING / PUBLIC
# ============================================================

def landing(request):
    return render(
        request,
        "landing.html",
    )


# ============================================================
# AUTHENTICATION
# ============================================================
def register(request):
    if request.user.is_authenticated:
        return redirect("home")

    if request.method == "POST":
        username = request.POST.get(
            "username",
            "",
        ).strip()

        phone_number = request.POST.get(
            "phone_number",
            "",
        ).strip()

        password = request.POST.get(
            "password",
            "",
        )

        password_confirm = request.POST.get(
            "password_confirm",
            "",
        )

        first_name = request.POST.get(
            "first_name",
            "",
        ).strip()

        last_name = request.POST.get(
            "last_name",
            "",
        ).strip()

        if not username:
            messages.error(
                request,
                "Username is required.",
            )
            return render(
                request,
                "register.html",
            )

        if not phone_number:
            messages.error(
                request,
                "Phone number is required.",
            )
            return render(
                request,
                "register.html",
            )

        if not first_name or not last_name:
            messages.error(
                request,
                "First name and last name are required.",
            )
            return render(
                request,
                "register.html",
            )

        if not password:
            messages.error(
                request,
                "Password is required.",
            )
            return render(
                request,
                "register.html",
            )

        if password != password_confirm:
            messages.error(
                request,
                "Passwords do not match.",
            )
            return render(
                request,
                "register.html",
            )

        if User.objects.filter(
            username__iexact=username
        ).exists():
            messages.error(
                request,
                "That username is already taken.",
            )
            return render(
                request,
                "register.html",
            )

        if User.objects.filter(
            phone_number=phone_number
        ).exists():
            messages.error(
                request,
                "That phone number is already registered.",
            )
            return render(
                request,
                "register.html",
            )

        user = User.objects.create_user(
            username=username,
            phone_number=phone_number,
            password=password,
            first_name=first_name,
            last_name=last_name,
        )

        user.is_phone_verified = False

        user.save(
            update_fields=[
                "is_phone_verified",
            ]
        )

        code = f"{random.randint(0, 999999):06d}"

        PhoneOTP.objects.create(
            user=user,
            phone_number=phone_number,
            code=code,
            expires_at=timezone.now()
            + timezone.timedelta(minutes=10),
        )

        request.session["otp_user_id"] = user.pk

        print()
        print("=" * 50)
        print("PHERAL DEVELOPMENT OTP")
        print(f"Phone: {phone_number}")
        print(f"OTP:   {code}")
        print("=" * 50)
        print()

        return redirect("verify_otp")

    return render(
        request,
        "register.html",
    )
def login_view(request):
    if request.user.is_authenticated:
        return redirect("home")

    if request.method != "POST":
        return render(
            request,
            "login.html",
        )

    phone_number = request.POST.get(
        "phone_number",
        "",
    ).strip()

    password = request.POST.get(
        "password",
        "",
    )

    if not phone_number:
        messages.error(
            request,
            "Phone number is required.",
        )
        return render(
            request,
            "login.html",
        )

    if not password:
        messages.error(
            request,
            "Password is required.",
        )
        return render(
            request,
            "login.html",
        )

    try:
        user_obj = User.objects.get(
            phone_number=phone_number,
        )

    except User.DoesNotExist:
        messages.error(
            request,
            "Invalid phone number or password.",
        )
        return render(
            request,
            "login.html",
        )

    user = authenticate(
        request,
        username=user_obj.username,
        password=password,
    )

    if user is None:
        messages.error(
            request,
            "Invalid phone number or password.",
        )
        return render(
            request,
            "login.html",
        )

    if not user.is_active:
        messages.error(
            request,
            "This account is inactive.",
        )
        return render(
            request,
            "login.html",
        )

    login(
        request,
        user,
    )

    user.last_seen = timezone.now()

    user.save(
        update_fields=[
            "last_seen",
        ]
    )

    return redirect("home")
def logout_view(request):
    if request.user.is_authenticated:
        request.user.last_seen = timezone.now()
        request.user.save(
            update_fields=[
                "last_seen",
            ]
        )

    logout(request)

    return redirect("landing")


def verify_otp(request):
    user_id = request.session.get(
        "otp_user_id"
    )

    if not user_id:
        return redirect("register")

    user = get_object_or_404(
        User,
        pk=user_id,
    )

    if request.method == "POST":
        code = request.POST.get(
            "code",
            "",
        ).strip()

        otp = (
            PhoneOTP.objects.filter(
                user=user,
                code=code,
                is_used=False,
            )
            .order_by("-created_at")
            .first()
        )

        if not otp:
            messages.error(
                request,
                "Invalid OTP.",
            )
            return render(
                request,
                "verify_otp.html",
            )

        if otp.is_expired:
            messages.error(
                request,
                "This OTP has expired.",
            )
            return render(
                request,
                "verify_otp.html",
            )

        otp.is_used = True

        otp.save(
            update_fields=[
                "is_used",
            ]
        )

        user.is_phone_verified = True

        user.save(
            update_fields=[
                "is_phone_verified",
            ]
        )

        get_or_create_wallet_token(user)

        currency = get_default_currency()

        if currency:
            get_or_create_wallet(
                user,
                currency,
            )

        login(
            request,
            user,
        )

        request.session.pop(
            "otp_user_id",
            None,
        )

        return redirect("home")

    return render(
        request,
        "verify_otp.html",
    )

def resend_otp(request):
    user_id = request.session.get("otp_user_id")

    if not user_id:
        return redirect("register")

    user = get_object_or_404(
        User,
        pk=user_id,
    )

    if user.is_phone_verified:
        return redirect("home")

    if request.method != "POST":
        return redirect("verify_otp")

    PhoneOTP.objects.filter(
        user=user,
        purpose=PhoneOTP.PURPOSE_VERIFICATION,
        is_used=False,
    ).update(
        is_used=True,
    )

    code = f"{random.randint(0, 999999):06d}"

    PhoneOTP.objects.create(
        user=user,
        phone_number=user.phone_number,
        code=code,
        purpose=PhoneOTP.PURPOSE_VERIFICATION,
        expires_at=timezone.now()
        + timezone.timedelta(minutes=10),
    )

    print()
    print("=" * 50)
    print("PHERAL DEVELOPMENT OTP")
    print(f"Phone: {user.phone_number}")
    print(f"OTP:   {code}")
    print("=" * 50)
    print()

    messages.success(
        request,
        "A new verification code has been sent.",
    )

    return redirect("verify_otp")

def forgot_password(request):
    if request.user.is_authenticated:
        return redirect("home")

    if request.method == "POST":
        phone_number = request.POST.get(
            "phone_number",
            "",
        ).strip()

        if not phone_number:
            messages.error(
                request,
                "Phone number is required.",
            )
            return render(
                request,
                "forgot_password.html",
            )

        try:
            user = User.objects.get(
                phone_number=phone_number,
            )
        except User.DoesNotExist:
            messages.error(
                request,
                "No account was found with that phone number.",
            )
            return render(
                request,
                "forgot_password.html",
            )

        if not user.is_active:
            messages.error(
                request,
                "This account is inactive.",
            )
            return render(
                request,
                "forgot_password.html",
            )

        PhoneOTP.objects.filter(
            user=user,
            purpose=PhoneOTP.PURPOSE_PASSWORD_RESET,
            is_used=False,
        ).update(
            is_used=True,
        )

        code = f"{random.randint(0, 999999):06d}"

        PhoneOTP.objects.create(
            user=user,
            phone_number=phone_number,
            code=code,
            purpose=PhoneOTP.PURPOSE_PASSWORD_RESET,
            expires_at=timezone.now()
            + timezone.timedelta(minutes=10),
        )

        request.session["password_reset_user_id"] = user.pk

        print()
        print("=" * 50)
        print("PHERAL PASSWORD RESET OTP")
        print(f"Phone: {phone_number}")
        print(f"OTP:   {code}")
        print("=" * 50)
        print()

        return redirect("verify_password_reset_otp")

    return render(
        request,
        "forgot_password.html",
    )

def verify_password_reset_otp(request):
    user_id = request.session.get(
        "password_reset_user_id"
    )

    if not user_id:
        return redirect("forgot_password")

    user = get_object_or_404(
        User,
        pk=user_id,
    )

    if request.method == "POST":
        code = request.POST.get(
            "code",
            "",
        ).strip()

        otp = (
            PhoneOTP.objects.filter(
                user=user,
                phone_number=user.phone_number,
                code=code,
                purpose=PhoneOTP.PURPOSE_PASSWORD_RESET,
                is_used=False,
            )
            .order_by("-created_at")
            .first()
        )

        if not otp:
            messages.error(
                request,
                "Invalid OTP.",
            )
            return render(
                request,
                "verify_password_reset_otp.html",
            )

        if otp.is_expired:
            messages.error(
                request,
                "This OTP has expired.",
            )
            return render(
                request,
                "verify_password_reset_otp.html",
            )

        otp.is_used = True

        otp.save(
            update_fields=[
                "is_used",
            ]
        )

        request.session["password_reset_verified"] = True

        return redirect("reset_password")

    return render(
        request,
        "verify_password_reset_otp.html",
    )

def reset_password(request):
    user_id = request.session.get(
        "password_reset_user_id"
    )

    verified = request.session.get(
        "password_reset_verified"
    )

    if not user_id or not verified:
        return redirect("forgot_password")

    user = get_object_or_404(
        User,
        pk=user_id,
    )

    if request.method == "POST":
        password = request.POST.get(
            "password",
            "",
        )

        password_confirm = request.POST.get(
            "password_confirm",
            "",
        )

        if not password:
            messages.error(
                request,
                "Password is required.",
            )
            return render(
                request,
                "reset_password.html",
            )

        if len(password) < 8:
            messages.error(
                request,
                "Password must be at least 8 characters.",
            )
            return render(
                request,
                "reset_password.html",
            )

        if password != password_confirm:
            messages.error(
                request,
                "Passwords do not match.",
            )
            return render(
                request,
                "reset_password.html",
            )

        user.set_password(password)

        user.save(
            update_fields=[
                "password",
            ]
        )

        request.session.pop(
            "password_reset_user_id",
            None,
        )

        request.session.pop(
            "password_reset_verified",
            None,
        )

        messages.success(
            request,
            "Your password has been reset. You can now log in.",
        )

        return redirect("login_view")

    return render(
        request,
        "reset_password.html",
    )

def resend_password_reset_otp(request):
    user_id = request.session.get(
        "password_reset_user_id"
    )

    if not user_id:
        return redirect("forgot_password")

    user = get_object_or_404(
        User,
        pk=user_id,
    )

    if request.method != "POST":
        return redirect(
            "verify_password_reset_otp"
        )

    PhoneOTP.objects.filter(
        user=user,
        purpose=PhoneOTP.PURPOSE_PASSWORD_RESET,
        is_used=False,
    ).update(
        is_used=True,
    )

    code = f"{random.randint(0, 999999):06d}"

    PhoneOTP.objects.create(
        user=user,
        phone_number=user.phone_number,
        code=code,
        purpose=PhoneOTP.PURPOSE_PASSWORD_RESET,
        expires_at=timezone.now()
        + timezone.timedelta(minutes=10),
    )

    print()
    print("=" * 50)
    print("PHERAL PASSWORD RESET OTP")
    print(f"Phone: {user.phone_number}")
    print(f"OTP:   {code}")
    print("=" * 50)
    print()

    messages.success(
        request,
        "A new verification code has been sent.",
    )

    return redirect(
        "verify_password_reset_otp"
    )
# ============================================================
# HOME
# ============================================================

@login_required
def home(request):
    now = timezone.now()

    # ============================================================
    # ACTIVE STATUSES
    # ============================================================

    active_statuses = list(
        Status.objects
        .filter(
            is_active=True,
            expires_at__gt=now,
        )
        .select_related("user")
        .order_by("-created_at")
    )

    # One status circle per user.
    status_groups = {}

    for status in active_statuses:
        status_groups.setdefault(
            status.user_id,
            []
        ).append(status)

    statuses = []

    for user_statuses in status_groups.values():

        latest_status = user_statuses[0]

        latest_status.status_count = len(
            user_statuses
        )

        latest_status.statuses = user_statuses

        statuses.append(
            latest_status
        )

    # ============================================================
    # CONTACTS
    # ============================================================

    contacts = (
        Contact.objects
        .filter(
            owner=request.user
        )
        .select_related(
            "contact_user"
        )
        .order_by(
            "-created_at"
        )
    )

    # ============================================================
    # RECENT CONVERSATIONS
    # ============================================================

    conversation_ids = (
        ConversationParticipant.objects
        .filter(
            user=request.user
        )
        .values_list(
            "conversation_id",
            flat=True
        )
    )

    conversations = (
        Conversation.objects
        .filter(
            id__in=conversation_ids
        )
        .prefetch_related(
            "participants__user"
        )
        .order_by(
            "-updated_at"
        )[:20]
    )

    # ============================================================
    # UNREAD NOTIFICATIONS
    # ============================================================

    unread_notifications = (
        Notification.objects
        .filter(
            user=request.user,
            is_read=False
        )
        .count()
    )

    # ============================================================
    # HOME CONTEXT
    # ============================================================

    context = {
        "statuses": statuses,
        "contacts": contacts,
        "conversations": conversations,
        "unread_notifications": unread_notifications,
    }

    return render(
        request,
        "home.html",
        context
    )
# ============================================================
# PROFILE
# ============================================================

@login_required
def profile(request, username=None):
    if username:
        profile_user = get_object_or_404(
            User,
            username__iexact=username,
        )
    else:
        profile_user = request.user

    posts = (
        Post.objects.filter(
            author=profile_user,
            is_deleted=False,
        )
        .select_related("author")
        .order_by("-created_at")
    )

    jobs = (
        HireJob.objects.filter(
            employer=profile_user,
        )
        .select_related("currency")
        .order_by("-created_at")
    )

    return render(
        request,
        "profile.html",
        {
            "profile_user": profile_user,
            "posts": posts,
            "jobs": jobs,
        },
    )


@login_required
def profile_edit(request):
    if request.method == "POST":
        request.user.first_name = request.POST.get(
            "first_name",
            ""
        ).strip()

        request.user.last_name = request.POST.get(
            "last_name",
            ""
        ).strip()

        request.user.bio = request.POST.get(
            "bio",
            ""
        ).strip()

        if request.FILES.get("avatar"):
            request.user.avatar = request.FILES[
                "avatar"
            ]

        request.user.save()

        messages.success(
            request,
            "Profile updated.",
        )

        return redirect(
            "profile"
        )

    return render(
        request,
        "profile_edit.html",
    )


# ============================================================
# CHAT
# ============================================================

@login_required
def chat(request, conversation_id=None, username=None):

    # ============================================================
    # OPEN CHAT BY USERNAME
    # ============================================================

    if username:

        other_user = get_object_or_404(
            User,
            username__iexact=username,
        )

        if other_user == request.user:
            return redirect("home")

        conversation = get_or_create_direct_conversation(
            request.user,
            other_user,
        )

    # ============================================================
    # OPEN CHAT BY CONVERSATION ID
    # ============================================================

    elif conversation_id:

        conversation = get_object_or_404(
            Conversation,
            pk=conversation_id,
            is_active=True,
        )

        # Current user MUST belong to the conversation.
        if not ConversationParticipant.objects.filter(
            conversation=conversation,
            user=request.user,
        ).exists():
            return redirect("home")

        # Find the other participant.
        other_participant = (
            ConversationParticipant.objects
            .filter(
                conversation=conversation,
            )
            .exclude(
                user=request.user,
            )
            .select_related("user")
            .first()
        )

        if not other_participant:
            return redirect("home")

        other_user = other_participant.user

    else:

        return redirect("home")

    # ============================================================
    # SEND MESSAGE
    # ============================================================

    if request.method == "POST":

        content = request.POST.get(
            "content",
            "",
        ).strip()

        attachment = request.FILES.get(
            "attachment",
        )

        if content or attachment:

            message_type = Message.MessageType.TEXT

            if attachment:

                content_type = (
                    getattr(
                        attachment,
                        "content_type",
                        "",
                    )
                    or ""
                ).lower()

                if content_type.startswith("image/"):
                    message_type = Message.MessageType.IMAGE

                elif content_type.startswith("video/"):
                    message_type = Message.MessageType.VIDEO

                elif content_type.startswith("audio/"):
                    message_type = Message.MessageType.VOICE

                else:
                    message_type = Message.MessageType.FILE

            Message.objects.create(
                conversation=conversation,
                sender=request.user,
                content=content,
                attachment=attachment,
                message_type=message_type,
            )

            conversation.save(
                update_fields=["updated_at"]
            )

        return redirect(
            "chat",
            conversation_id=conversation.pk,
        )

    # ============================================================
    # LOAD MESSAGES
    # ============================================================

    messages = (
        Message.objects
        .filter(
            conversation=conversation,
            is_deleted=False,
        )
        .select_related(
            "sender",
            "reply_to",
            "reply_to__sender",
            "transaction",
            "receipt",
        )
        .order_by(
            "created_at",
        )
    )

    # ============================================================
    # MARK AS READ
    # ============================================================

    ConversationParticipant.objects.filter(
        conversation=conversation,
        user=request.user,
    ).update(
        last_read_at=timezone.now(),
    )

    # ============================================================
    # CONTEXT
    # ============================================================

    context = {
        "conversation": conversation,
        "other_user": other_user,
        "messages": messages,
        "chat_messages": messages,
    }

    return render(
        request,
        "chat.html",
        context,
    )

@login_required
def chat_list(request):
    """
    Pheral chat list.

    Shows every conversation the current user belongs to,
    ordered by the most recent message.
    """

    conversations = (
        Conversation.objects
        .filter(
            participants__user=request.user,
            is_active=True,
        )
        .prefetch_related(
            Prefetch(
                "participants",
                queryset=ConversationParticipant.objects
                .select_related("user"),
            ),
            Prefetch(
                "messages",
                queryset=Message.objects
                .filter(is_deleted=False)
                .select_related("sender")
                .order_by("-created_at"),
                to_attr="ordered_messages",
            ),
        )
        .distinct()
    )

    # ------------------------------------------------------------
    # Sort conversations by latest message.
    # Conversations without messages go to the bottom.
    # ------------------------------------------------------------

    conversations = list(conversations)

    conversations.sort(
        key=lambda conversation: (
            conversation.ordered_messages[0].created_at
            if conversation.ordered_messages
            else conversation.created_at
        ),
        reverse=True,
    )

    context = {
        "conversations": conversations,
    }

    return render(
        request,
        "chat_list.html",
        context,
    )

@login_required
def start_chat(request, username):

    other_user = get_object_or_404(
        User,
        username__iexact=username,
    )

    if other_user == request.user:
        return redirect("home")

    conversation = get_or_create_direct_conversation(
        request.user,
        other_user,
    )

    return redirect(
        "chat",
        conversation_id=conversation.pk,
    )
# ============================================================
# GROUP CHAT
# ============================================================

@login_required
def group_list(request):
    groups = (
        Conversation.objects.filter(
            conversation_type=Conversation.ConversationType.GROUP,
            participants__user=request.user,
            is_active=True,
        )
        .prefetch_related(
            "participants__user",
        )
        .order_by("-updated_at")
    )

    return render(
        request,
        "group_list.html",
        {
            "groups": groups,
        },
    )


@login_required
def group_create(request):
    if request.method == "POST":
        name = request.POST.get(
            "name",
            ""
        ).strip()

        description = request.POST.get(
            "description",
            ""
        ).strip()

        if not name:
            messages.error(
                request,
                "Group name is required.",
            )
            return render(
                request,
                "group_create.html",
            )

        conversation = Conversation.objects.create(
            conversation_type=Conversation.ConversationType.GROUP,
            name=name,
            description=description,
            created_by=request.user,
        )

        ConversationParticipant.objects.create(
            conversation=conversation,
            user=request.user,
            is_admin=True,
        )

        currency = get_default_currency()

        if currency:
            GroupLedger.objects.create(
                conversation=conversation,
                currency=currency,
            )

        return redirect(
            "group_chat",
            conversation_id=conversation.pk,
        )

    return render(
        request,
        "group_create.html",
    )


@login_required
def group_chat(request, conversation_id):
    conversation = get_object_or_404(
        Conversation,
        pk=conversation_id,
        conversation_type=Conversation.ConversationType.GROUP,
        participants__user=request.user,
    )

    chat_messages = (
        Message.objects.filter(
            conversation=conversation,
        )
        .select_related(
            "sender",
            "transaction",
            "receipt",
        )
        .order_by("created_at")
    )

    if request.method == "POST":
        content = request.POST.get(
            "content",
            ""
        ).strip()

        if content:
            Message.objects.create(
                conversation=conversation,
                sender=request.user,
                message_type=Message.MessageType.TEXT,
                content=content,
            )

            conversation.updated_at = timezone.now()
            conversation.save(
                update_fields=[
                    "updated_at",
                ]
            )

        return redirect(
            "group_chat",
            conversation_id=conversation.pk,
        )

    return render(
        request,
        "group_chat.html",
        {
            "conversation": conversation,
            "chat_messages": chat_messages,
        },
    )


@login_required
def group_add_member(request, conversation_id):
    conversation = get_object_or_404(
        Conversation,
        pk=conversation_id,
        conversation_type=Conversation.ConversationType.GROUP,
    )

    admin_participant = get_object_or_404(
        ConversationParticipant,
        conversation=conversation,
        user=request.user,
        is_admin=True,
    )

    username = request.POST.get(
        "username",
        ""
    ).strip()

    if not username:
        messages.error(
            request,
            "Username is required.",
        )
        return redirect(
            "group_chat",
            conversation_id=conversation.pk,
        )

    user = get_object_or_404(
        User,
        username__iexact=username,
    )

    ConversationParticipant.objects.get_or_create(
        conversation=conversation,
        user=user,
    )

    return redirect(
        "group_chat",
        conversation_id=conversation.pk,
    )


# ============================================================
# PAYMENTS
# ============================================================

@login_required
@transaction.atomic
def pay_user(request, username):
    recipient = get_object_or_404(
        User,
        username__iexact=username,
    )

    if recipient == request.user:
        messages.error(
            request,
            "You cannot pay yourself.",
        )
        return redirect(
            "profile",
            username=recipient.username,
        )

    currency = get_default_currency()

    if not currency:
        messages.error(
            request,
            "No active currency is configured.",
        )
        return redirect(
            "profile",
            username=recipient.username,
        )

    sender_wallet = get_or_create_wallet(
        request.user,
        currency,
    )

    recipient_wallet = get_or_create_wallet(
        recipient,
        currency,
    )

    if request.method == "POST":
        amount = parse_amount(
            request.POST.get("amount")
        )

        description = request.POST.get(
            "description",
            ""
        ).strip()

        if amount is None:
            messages.error(
                request,
                "Enter a valid payment amount.",
            )
            return render(
                request,
                "pay.html",
                {
                    "recipient": recipient,
                    "currency": currency,
                },
            )

        if sender_wallet.balance < amount:
            messages.error(
                request,
                "Insufficient wallet balance.",
            )
            return render(
                request,
                "pay.html",
                {
                    "recipient": recipient,
                    "currency": currency,
                },
            )

        # Ensure the user has an active wallet token.
        wallet_token = get_or_create_wallet_token(
            request.user
        )

        if not wallet_token.is_active:
            messages.error(
                request,
                "Wallet authorization is inactive.",
            )
            return redirect("home")

        balance_before = sender_wallet.balance
        sender_wallet.balance -= amount
        sender_wallet.save(
            update_fields=[
                "balance",
                "updated_at",
            ]
        )

        recipient_before = recipient_wallet.balance
        recipient_wallet.balance += amount
        recipient_wallet.save(
            update_fields=[
                "balance",
                "updated_at",
            ]
        )

        pheral_transaction = PheralTransaction.objects.create(
            sender=request.user,
            recipient=recipient,
            sender_wallet=sender_wallet,
            recipient_wallet=recipient_wallet,
            transaction_type=PheralTransaction.TransactionType.TRANSFER,
            amount=amount,
            currency=currency,
            fee=Decimal("0.00"),
            status=PheralTransaction.Status.COMPLETED,
            description=description,
            completed_at=timezone.now(),
        )

        LedgerEntry.objects.create(
            transaction=pheral_transaction,
            wallet=sender_wallet,
            entry_type=LedgerEntry.EntryType.DEBIT,
            amount=amount,
            balance_before=balance_before,
            balance_after=sender_wallet.balance,
            description=description,
        )

        LedgerEntry.objects.create(
            transaction=pheral_transaction,
            wallet=recipient_wallet,
            entry_type=LedgerEntry.EntryType.CREDIT,
            amount=amount,
            balance_before=recipient_before,
            balance_after=recipient_wallet.balance,
            description=description,
        )

        receipt = Receipt.objects.create(
            transaction=pheral_transaction,
            payer=request.user,
            recipient=recipient,
            amount=amount,
            currency=currency,
            description=description,
        )

        conversation = get_or_create_direct_conversation(
            request.user,
            recipient,
        )

        Message.objects.create(
            conversation=conversation,
            sender=request.user,
            message_type=Message.MessageType.PAYMENT,
            content=f"₦{amount:,.2f} sent",
            transaction=pheral_transaction,
            receipt=receipt,
        )

        Notification.objects.create(
            user=recipient,
            notification_type=Notification.NotificationType.PAYMENT,
            title="Payment received",
            body=(
                f"@{request.user.username} sent "
                f"{currency.symbol}{amount:,.2f}"
            ),
            link="",
        )

        wallet_token.last_used_at = timezone.now()
        wallet_token.save(
            update_fields=[
                "last_used_at",
            ]
        )

        return redirect(
            "chat",
            conversation_id=conversation.pk,
        )

    return render(
        request,
        "pay.html",
        {
            "recipient": recipient,
            "currency": currency,
        },
    )


@login_required
def wallet(request):
    wallets = (
        Wallet.objects.filter(
            user=request.user,
            is_active=True,
        )
        .select_related("currency")
        .order_by("currency__code")
    )

    transactions = (
        PheralTransaction.objects.filter(
            Q(sender=request.user)
            | Q(recipient=request.user)
        )
        .select_related(
            "sender",
            "recipient",
            "currency",
        )
        .order_by("-created_at")[:50]
    )

    token = get_or_create_wallet_token(
        request.user
    )

    return render(
        request,
        "wallet.html",
        {
            "wallets": wallets,
            "transactions": transactions,
            "wallet_token": token,
        },
    )


# ============================================================
# TOP UP
# ============================================================

@login_required
def top_up(request):
    currency = get_default_currency()

    wallet_obj = None

    if currency:
        wallet_obj = get_or_create_wallet(
            request.user,
            currency,
        )

    if request.method == "POST":
        amount = parse_amount(
            request.POST.get("amount")
        )

        if amount is None:
            messages.error(
                request,
                "Enter a valid top-up amount.",
            )
            return render(
                request,
                "top_up.html",
                {
                    "currency": currency,
                    "wallet": wallet_obj,
                },
            )

        if not wallet_obj:
            messages.error(
                request,
                "No wallet currency is configured.",
            )
            return redirect(
                "wallet"
            )

        # Development flow:
        # this creates the pending top-up transaction.
        # A payment provider will later complete it.
        pheral_transaction = PheralTransaction.objects.create(
            sender=request.user,
            sender_wallet=wallet_obj,
            transaction_type=PheralTransaction.TransactionType.TOP_UP,
            amount=amount,
            currency=currency,
            status=PheralTransaction.Status.PENDING,
        )

        return render(
            request,
            "top_up.html",
            {
                "currency": currency,
                "wallet": wallet_obj,
                "transaction": pheral_transaction,
            },
        )

    return render(
        request,
        "top_up.html",
        {
            "currency": currency,
            "wallet": wallet_obj,
        },
    )


# ============================================================
# GLOBAL PAY / FX
# ============================================================

@login_required
def global_pay(request):
    currencies = Currency.objects.filter(
        is_active=True
    ).order_by("code")

    if request.method == "POST":
        username = request.POST.get(
            "username",
            ""
        ).strip()

        recipient = User.objects.filter(
            username__iexact=username
        ).first()

        if not recipient:
            messages.error(
                request,
                "Pheral user not found.",
            )
            return render(
                request,
                "global_pay.html",
                {
                    "currencies": currencies,
                },
            )

        return redirect(
            "pay_user",
            username=recipient.username,
        )

    return render(
        request,
        "global_pay.html",
        {
            "currencies": currencies,
        },
    )


@login_required
def currency_converter(request):
    currencies = Currency.objects.filter(
        is_active=True
    ).order_by("code")

    result = None

    if request.method == "POST":
        source_id = request.POST.get(
            "source_currency"
        )

        target_id = request.POST.get(
            "target_currency"
        )

        amount = parse_amount(
            request.POST.get("amount")
        )

        source = Currency.objects.filter(
            pk=source_id,
            is_active=True,
        ).first()

        target = Currency.objects.filter(
            pk=target_id,
            is_active=True,
        ).first()

        if source and target and amount:
            if source.pk == target.pk:
                converted = amount
            else:
                rate = ExchangeRate.objects.filter(
                    source_currency=source,
                    target_currency=target,
                    is_active=True,
                ).first()

                converted = (
                    amount * rate.rate
                    if rate
                    else None
                )

            if converted is not None:
                result = {
                    "amount": amount,
                    "source": source,
                    "target": target,
                    "converted": converted,
                }

    return render(
        request,
        "currency_converter.html",
        {
            "currencies": currencies,
            "result": result,
        },
    )


# ============================================================
# RECEIPTS
# ============================================================

@login_required
def receipt_detail(request, reference):
    receipt = get_object_or_404(
        Receipt.objects.select_related(
            "transaction",
            "payer",
            "recipient",
            "currency",
        ),
        reference=reference,
    )

    if (
        receipt.payer != request.user
        and receipt.recipient != request.user
    ):
        raise Http404

    return render(
        request,
        "receipt.html",
        {
            "receipt": receipt,
        },
    )


# ============================================================
# STATUS
# ============================================================

@login_required
def status_list(request):
    now = timezone.now()

    active_statuses = (
        Status.objects
        .filter(
            is_active=True,
            expires_at__gt=now,
        )
        .select_related("user")
        .order_by("-created_at")
    )

    status_groups = {}

    for status in active_statuses:
        status_groups.setdefault(
            status.user_id,
            []
        ).append(status)

    status_groups_list = []

    for user_id, user_statuses in status_groups.items():

        latest_status = user_statuses[0]

        status_groups_list.append({
            "user": latest_status.user,
            "latest": latest_status,
            "statuses": user_statuses,
            "count": len(user_statuses),
            "is_owner": user_id == request.user.id,
        })

    return render(
        request,
        "status_list.html",
        {
            "status_groups": status_groups_list,
        },
    )


from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.shortcuts import get_object_or_404, redirect, render
from django.utils import timezone
from django.views.decorators.http import require_POST

from .models import Status, StatusView
@login_required
def status_detail(request, status_id):
    now = timezone.now()

    status = get_object_or_404(
        Status.objects.select_related("user"),
        id=status_id,
        is_active=True,
        expires_at__gt=now,
    )

    # ------------------------------------------------------------
    # RECORD VIEW
    # ------------------------------------------------------------

    is_owner = status.user_id == request.user.id

    if not is_owner:
        StatusView.objects.get_or_create(
            status=status,
            viewer=request.user,
        )

    # ------------------------------------------------------------
    # CURRENT USER'S ACTIVE STATUSES
    # ------------------------------------------------------------

    user_statuses = list(
        Status.objects.filter(
            user=status.user,
            is_active=True,
            expires_at__gt=now,
        ).order_by("created_at")
    )

    current_index = next(
        (
            index
            for index, item in enumerate(user_statuses)
            if item.id == status.id
        ),
        0,
    )

    previous_status = (
        user_statuses[current_index - 1]
        if current_index > 0
        else None
    )

    next_status = (
        user_statuses[current_index + 1]
        if current_index < len(user_statuses) - 1
        else None
    )

    # ------------------------------------------------------------
    # OTHER USERS' STATUS GROUPS
    # ------------------------------------------------------------

    other_statuses = (
        Status.objects.filter(
            is_active=True,
            expires_at__gt=now,
        )
        .exclude(
            user=status.user,
        )
        .select_related("user")
        .order_by("user_id", "created_at")
    )

    # One entry per user.
    other_users = []
    seen_users = set()

    for item in other_statuses:
        if item.user_id not in seen_users:
            seen_users.add(item.user_id)
            other_users.append(item)

    # ------------------------------------------------------------
    # FIND NEXT USER
    # ------------------------------------------------------------

    next_user_status = None

    for item in other_users:
        if item.user_id != status.user_id:
            next_user_status = item
            break

    # ------------------------------------------------------------
    # VIEW INFORMATION
    # ------------------------------------------------------------

    view_count = StatusView.objects.filter(
        status=status,
    ).count()

    viewers = []

    if is_owner:
        viewers = (
            StatusView.objects
            .filter(status=status)
            .select_related("viewer")
            .order_by("-viewed_at")
        )

    return render(
        request,
        "status_detail.html",
        {
            "status": status,
            "user_statuses": user_statuses,
            "current_index": current_index,
            "previous_status": previous_status,
            "next_status": next_status,
            "next_user_status": next_user_status,
            "is_owner": is_owner,
            "view_count": view_count,
            "viewers": viewers,
        },
    )


@login_required
@require_POST
def delete_status(request, status_id):

    status = get_object_or_404(
        Status,
        id=status_id,
        user=request.user,
    )

    status.delete()

    messages.success(
        request,
        "Status deleted.",
    )

    return redirect("status_list")


@login_required
def create_status(request):
    if request.method == "POST":
        text = request.POST.get(
            "text",
            ""
        ).strip()

        media = request.FILES.get(
            "media"
        )

        if media:
            content_type = media.content_type or ""

            if content_type.startswith("image/"):
                status_type = Status.StatusType.IMAGE
            elif content_type.startswith("video/"):
                status_type = Status.StatusType.VIDEO
            else:
                status_type = Status.StatusType.TEXT
        else:
            status_type = Status.StatusType.TEXT

        Status.objects.create(
            user=request.user,
            status_type=status_type,
            text=text,
            media=media,
            expires_at=timezone.now()
            + timezone.timedelta(hours=24),
        )

        return redirect(
            "status_list"
        )

    return render(
        request,
        "create_status.html",
    )



# ============================================================
# FEED
# ============================================================

@login_required
def feed(request):
    posts = (
        Post.objects.filter(
            is_deleted=False,
        )
        .select_related("author")
        .prefetch_related(
            "likes",
            "comments",
        )
        .order_by("-created_at")
    )

    return render(
        request,
        "feed.html",
        {
            "posts": posts,
        },
    )


@login_required
def create_post(request):
    if request.method == "POST":
        content = request.POST.get(
            "content",
            ""
        ).strip()

        media = request.FILES.get(
            "media"
        )

        if not content and not media:
            messages.error(
                request,
                "Post cannot be empty.",
            )
            return redirect(
                "feed"
            )

        Post.objects.create(
            author=request.user,
            content=content,
            media=media,
        )

        return redirect(
            "feed"
        )

    return render(
        request,
        "create_post.html",
    )


@login_required
def like_post(request, post_id):
    post = get_object_or_404(
        Post,
        pk=post_id,
        is_deleted=False,
    )

    like, created = PostLike.objects.get_or_create(
        post=post,
        user=request.user,
    )

    if not created:
        like.delete()

    if request.headers.get(
        "X-Requested-With"
    ) == "XMLHttpRequest":
        return JsonResponse(
            {
                "liked": created,
                "likes": post.likes.count(),
            }
        )

    return redirect(
        request.META.get(
            "HTTP_REFERER",
            "/",
        )
    )


@login_required
def comment_post(request, post_id):
    post = get_object_or_404(
        Post,
        pk=post_id,
        is_deleted=False,
    )

    if request.method == "POST":
        content = request.POST.get(
            "content",
            ""
        ).strip()

        if content:
            PostComment.objects.create(
                post=post,
                user=request.user,
                content=content,
            )

    return redirect(
        request.META.get(
            "HTTP_REFERER",
            "/",
        )
    )


# ============================================================
# HIRE
# ============================================================

@login_required
def hire(request):
    jobs = (
        HireJob.objects.filter(
            status=HireJob.Status.OPEN,
        )
        .select_related(
            "employer",
            "currency",
        )
        .order_by("-created_at")
    )

    return render(
        request,
        "hire.html",
        {
            "jobs": jobs,
        },
    )


@login_required
def create_hire_job(request):
    currencies = Currency.objects.filter(
        is_active=True
    ).order_by("code")

    if request.method == "POST":
        title = request.POST.get(
            "title",
            ""
        ).strip()

        description = request.POST.get(
            "description",
            ""
        ).strip()

        amount = parse_amount(
            request.POST.get("budget")
        )

        currency_id = request.POST.get(
            "currency"
        )

        currency = Currency.objects.filter(
            pk=currency_id,
            is_active=True,
        ).first()

        if not title:
            messages.error(
                request,
                "Job title is required.",
            )
            return render(
                request,
                "create_hire_job.html",
                {
                    "currencies": currencies,
                },
            )

        if not amount:
            messages.error(
                request,
                "Enter a valid budget.",
            )
            return render(
                request,
                "create_hire_job.html",
                {
                    "currencies": currencies,
                },
            )

        if not currency:
            messages.error(
                request,
                "Select a valid currency.",
            )
            return render(
                request,
                "create_hire_job.html",
                {
                    "currencies": currencies,
                },
            )

        job = HireJob.objects.create(
            employer=request.user,
            title=title,
            description=description,
            budget=amount,
            currency=currency,
        )

        return redirect(
            "hire_job_detail",
            job_id=job.pk,
        )

    return render(
        request,
        "create_hire_job.html",
        {
            "currencies": currencies,
        },
    )


@login_required
def hire_job_detail(request, job_id):
    job = get_object_or_404(
        HireJob.objects.select_related(
            "employer",
            "currency",
        ),
        pk=job_id,
    )

    requests = job.requests.select_related(
        "requester",
        "worker",
    ).order_by("-created_at")

    return render(
        request,
        "hire_job_detail.html",
        {
            "job": job,
            "hire_requests": requests,
        },
    )


@login_required
def send_hire_request(request, job_id, username):
    job = get_object_or_404(
        HireJob,
        pk=job_id,
        status=HireJob.Status.OPEN,
    )

    worker = get_object_or_404(
        User,
        username__iexact=username,
    )

    if worker == request.user:
        messages.error(
            request,
            "You cannot hire yourself.",
        )
        return redirect(
            "hire_job_detail",
            job_id=job.pk,
        )

    if request.method == "POST":
        message_text = request.POST.get(
            "message",
            ""
        ).strip()

        proposed_amount = parse_amount(
            request.POST.get(
                "proposed_amount"
            )
        )

        hire_request = HireRequest.objects.create(
            job=job,
            requester=request.user,
            worker=worker,
            message=message_text,
            proposed_amount=proposed_amount,
        )

        conversation = get_or_create_direct_conversation(
            request.user,
            worker,
        )

        Message.objects.create(
            conversation=conversation,
            sender=request.user,
            message_type=Message.MessageType.HIRE,
            content=(
                f"Hire request: {job.title}"
            ),
        )

        Notification.objects.create(
            user=worker,
            notification_type=Notification.NotificationType.HIRE,
            title="New hire request",
            body=(
                f"@{request.user.username} "
                f"wants to hire you."
            ),
        )

        return redirect(
            "chat",
            conversation_id=conversation.pk,
        )

    return render(
        request,
        "send_hire_request.html",
        {
            "job": job,
            "worker": worker,
        },
    )


# ============================================================
# NOTIFICATIONS
# ============================================================

@login_required
def notifications(request):
    notification_list = (
        Notification.objects.filter(
            user=request.user,
        )
        .order_by("-created_at")
    )

    return render(
        request,
        "notifications.html",
        {
            "notifications": notification_list,
        },
    )


@login_required
def mark_notification_read(request, notification_id):
    notification = get_object_or_404(
        Notification,
        pk=notification_id,
        user=request.user,
    )

    notification.is_read = True
    notification.save(
        update_fields=[
            "is_read",
        ]
    )

    return redirect(
        request.META.get(
            "HTTP_REFERER",
            "/notifications/",
        )
    )


@login_required
def mark_all_notifications_read(request):
    Notification.objects.filter(
        user=request.user,
        is_read=False,
    ).update(
        is_read=True
    )

    return redirect(
        "notifications"
    )


# ============================================================
# SEARCH / CONTACT DISCOVERY
# ============================================================

@login_required
def search(request):
    query = request.GET.get(
        "q",
        ""
    ).strip()

    users = User.objects.none()

    if query:
        users = (
            User.objects.filter(
                Q(username__icontains=query)
                | Q(first_name__icontains=query)
                | Q(last_name__icontains=query)
                | Q(phone_number__icontains=query)
            )
            .exclude(
                pk=request.user.pk
            )
            .order_by(
                "username"
            )[:50]
        )

    return render(
        request,
        "search.html",
        {
            "query": query,
            "users": users,
        },
    )


# ============================================================
# CONTACTS
# ============================================================

@login_required
def contacts(request):
    """
    Pheral Contacts

    Shows contacts belonging to the logged-in user.
    Supports searching the user's existing contacts.

    The Contact model represents a relationship between:
        owner       -> current user
        contact_user -> Pheral user in their contacts
    """

    query = request.GET.get("q", "").strip()

    contacts_qs = (
        Contact.objects
        .filter(owner=request.user)
        .select_related("contact_user")
        .order_by("contact_user__username")
    )

    if query:
        contacts_qs = contacts_qs.filter(
            Q(contact_user__username__icontains=query)
            | Q(contact_user__first_name__icontains=query)
            | Q(contact_user__last_name__icontains=query)
            | Q(nickname__icontains=query)
            | Q(phone_number__icontains=query)
        )

    return render(
        request,
        "contacts.html",
        {
            "contacts": contacts_qs,
            "query": query,
        },
    )


@login_required
def add_contact(request, username):
    """
    Add an existing Pheral user to the current user's contacts.

    This is deliberately POST-only because adding a contact
    changes database state.
    """

    if request.method != "POST":
        return redirect("contacts")

    contact_user = get_object_or_404(
        User,
        username=username,
    )

    # Prevent adding yourself.
    if contact_user == request.user:
        messages.error(
            request,
            "You cannot add yourself to your contacts.",
        )
        return redirect("contacts")

    contact, created = Contact.objects.get_or_create(
        owner=request.user,
        contact_user=contact_user,
        defaults={
            "phone_number": getattr(
                contact_user,
                "phone_number",
                "",
            ) or "",
        },
    )

    if created:
        messages.success(
            request,
            f"@{contact_user.username} was added to your contacts.",
        )
    else:
        messages.info(
            request,
            f"@{contact_user.username} is already in your contacts.",
        )

    return redirect("contacts")


# ============================================================
# AGENT MODE
# ============================================================

@login_required
def agent(request):
    commands = (
        AgentCommand.objects.filter(
            user=request.user,
        )
        .order_by("-created_at")[:50]
    )

    if request.method == "POST":
        command_text = request.POST.get(
            "command",
            ""
        ).strip()

        if not command_text:
            messages.error(
                request,
                "Enter an agent command.",
            )
            return redirect(
                "agent"
            )

        command = AgentCommand.objects.create(
            user=request.user,
            command=command_text,
            status=AgentCommand.Status.PENDING,
        )

        return redirect(
            "agent_command",
            command_id=command.pk,
        )

    return render(
        request,
        "agent.html",
        {
            "commands": commands,
        },
    )


@login_required
def agent_command(request, command_id):
    command = get_object_or_404(
        AgentCommand,
        pk=command_id,
        user=request.user,
    )

    # The actual parser/executor can live directly in this view
    # layer as the MVP grows. No services.py is required.

    if command.status == AgentCommand.Status.PENDING:
        command.status = AgentCommand.Status.PROCESSING
        command.save(
            update_fields=[
                "status",
            ]
        )

        raw = command.command.strip()

        # ----------------------------------------------------
        # @agent msg @username message...
        # ----------------------------------------------------

        if raw.lower().startswith(
            "@agent msg "
        ):
            parts = raw.split(
                " ",
                3,
            )

            if len(parts) >= 4:
                target = parts[2].lstrip("@")
                text = parts[3].strip()

                recipient = User.objects.filter(
                    username__iexact=target
                ).first()

                if recipient:
                    conversation = (
                        get_or_create_direct_conversation(
                            request.user,
                            recipient,
                        )
                    )

                    Message.objects.create(
                        conversation=conversation,
                        sender=request.user,
                        message_type=Message.MessageType.AGENT,
                        content=text,
                    )

                    AgentActivity.objects.create(
                        user=request.user,
                        command=command,
                        conversation=conversation,
                        action="send_message",
                        details={
                            "recipient": recipient.username,
                            "message": text,
                        },
                    )

                    command.status = (
                        AgentCommand.Status.COMPLETED
                    )

                    command.result = {
                        "action": "send_message",
                        "recipient": recipient.username,
                        "message": text,
                    }

                    command.completed_at = timezone.now()

                    command.save(
                        update_fields=[
                            "status",
                            "result",
                            "completed_at",
                        ]
                    )

                    return redirect(
                        "agent"
                    )

        # ----------------------------------------------------
        # @agent pay @username amount
        # ----------------------------------------------------

        if raw.lower().startswith(
            "@agent pay "
        ):
            parts = raw.split()

            if len(parts) >= 4:
                target = parts[2].lstrip("@")
                amount = parse_amount(
                    parts[3]
                )

                recipient = User.objects.filter(
                    username__iexact=target
                ).first()

                if recipient and amount:
                    currency = get_default_currency()

                    if currency:
                        sender_wallet = get_or_create_wallet(
                            request.user,
                            currency,
                        )

                        if sender_wallet.balance >= amount:
                            recipient_wallet = (
                                get_or_create_wallet(
                                    recipient,
                                    currency,
                                )
                            )

                            with transaction.atomic():
                                sender_before = (
                                    sender_wallet.balance
                                )

                                recipient_before = (
                                    recipient_wallet.balance
                                )

                                sender_wallet.balance -= amount
                                sender_wallet.save(
                                    update_fields=[
                                        "balance",
                                        "updated_at",
                                    ]
                                )

                                recipient_wallet.balance += amount
                                recipient_wallet.save(
                                    update_fields=[
                                        "balance",
                                        "updated_at",
                                    ]
                                )

                                pheral_transaction = (
                                    PheralTransaction.objects.create(
                                        sender=request.user,
                                        recipient=recipient,
                                        sender_wallet=sender_wallet,
                                        recipient_wallet=recipient_wallet,
                                        transaction_type=(
                                            PheralTransaction
                                            .TransactionType.TRANSFER
                                        ),
                                        amount=amount,
                                        currency=currency,
                                        status=(
                                            PheralTransaction
                                            .Status.COMPLETED
                                        ),
                                        completed_at=timezone.now(),
                                    )
                                )

                                LedgerEntry.objects.create(
                                    transaction=pheral_transaction,
                                    wallet=sender_wallet,
                                    entry_type=(
                                        LedgerEntry
                                        .EntryType.DEBIT
                                    ),
                                    amount=amount,
                                    balance_before=sender_before,
                                    balance_after=(
                                        sender_wallet.balance
                                    ),
                                )

                                LedgerEntry.objects.create(
                                    transaction=pheral_transaction,
                                    wallet=recipient_wallet,
                                    entry_type=(
                                        LedgerEntry
                                        .EntryType.CREDIT
                                    ),
                                    amount=amount,
                                    balance_before=recipient_before,
                                    balance_after=(
                                        recipient_wallet.balance
                                    ),
                                )

                                receipt = Receipt.objects.create(
                                    transaction=pheral_transaction,
                                    payer=request.user,
                                    recipient=recipient,
                                    amount=amount,
                                    currency=currency,
                                )

                                conversation = (
                                    get_or_create_direct_conversation(
                                        request.user,
                                        recipient,
                                    )
                                )

                                Message.objects.create(
                                    conversation=conversation,
                                    sender=request.user,
                                    message_type=(
                                        Message
                                        .MessageType.PAYMENT
                                    ),
                                    content=(
                                        f"₦{amount:,.2f} sent"
                                    ),
                                    transaction=(
                                        pheral_transaction
                                    ),
                                    receipt=receipt,
                                )

                                AgentActivity.objects.create(
                                    user=request.user,
                                    command=command,
                                    conversation=conversation,
                                    transaction=(
                                        pheral_transaction
                                    ),
                                    action="payment",
                                    details={
                                        "recipient": (
                                            recipient.username
                                        ),
                                        "amount": str(amount),
                                        "currency": currency.code,
                                    },
                                )

                                command.status = (
                                    AgentCommand.Status.COMPLETED
                                )

                                command.result = {
                                    "action": "payment",
                                    "recipient": (
                                        recipient.username
                                    ),
                                    "amount": str(amount),
                                    "currency": currency.code,
                                    "reference": (
                                        pheral_transaction.reference
                                    ),
                                }

                                command.completed_at = (
                                    timezone.now()
                                )

                                command.save(
                                    update_fields=[
                                        "status",
                                        "result",
                                        "completed_at",
                                    ]
                                )

                            return redirect(
                                "agent"
                            )

        command.status = AgentCommand.Status.FAILED

        command.result = {
            "error": "Command could not be understood or completed."
        }

        command.completed_at = timezone.now()

        command.save(
            update_fields=[
                "status",
                "result",
                "completed_at",
            ]
        )

    return redirect(
        "agent"
    )


# ============================================================
# PUSH DEVICES / PWA
# ============================================================

@login_required
def register_push_device(request):
    if request.method != "POST":
        return JsonResponse(
            {
                "error": "POST required."
            },
            status=405,
        )

    token = request.POST.get(
        "token",
        ""
    ).strip()

    platform = request.POST.get(
        "platform",
        PushDevice.Platform.WEB,
    )

    if not token:
        return JsonResponse(
            {
                "error": "Push token is required."
            },
            status=400,
        )

    device, created = PushDevice.objects.update_or_create(
        token=token,
        defaults={
            "user": request.user,
            "platform": platform,
            "is_active": True,
        },
    )

    return JsonResponse(
        {
            "success": True,
            "created": created,
        }
    )


# ============================================================
# API-LIKE JSON ENDPOINTS
# ============================================================

@login_required
def user_lookup(request):
    query = request.GET.get(
        "q",
        ""
    ).strip()

    users = []

    if query:
        matches = (
            User.objects.filter(
                Q(username__icontains=query)
                | Q(phone_number__icontains=query)
                | Q(first_name__icontains=query)
                | Q(last_name__icontains=query)
            )
            .exclude(
                pk=request.user.pk
            )
            .order_by("username")[:20]
        )

        users = [
            {
                "username": user.username,
                "display_name": user.display_name,
                "phone_number": user.phone_number,
                "avatar": (
                    user.avatar.url
                    if user.avatar
                    else ""
                ),
            }
            for user in matches
        ]

    return JsonResponse(
        {
            "results": users,
        }
    )


# ============================================================
# MESSAGE READ
# ============================================================

@login_required
def mark_message_read(request, message_id):
    message = get_object_or_404(
        Message,
        pk=message_id,
        conversation__participants__user=request.user,
    )

    MessageRead.objects.get_or_create(
        message=message,
        user=request.user,
    )

    return JsonResponse(
        {
            "success": True,
        }
    )

# ============================================================
# PWA
# ============================================================

from django.http import JsonResponse, HttpResponse


def pwa_manifest(request):
    """
    Pheral PWA manifest.
    """

    manifest = {
        "name": "Pheral",
        "short_name": "Pheral",
        "description": "Chat. Pay. Hire.",
        "start_url": "/home/",
        "scope": "/",
        "display": "standalone",
        "orientation": "portrait-primary",
        "background_color": "#ffffff",
        "theme_color": "#ffffff",
        "icons": [
            {
                "src": "/static/images/pheral-logo.png",
                "sizes": "192x192",
                "type": "image/png",
            },
            {
                "src": "/static/images/pheral-logo.png",
                "sizes": "512x512",
                "type": "image/png",
            },
        ],
    }

    response = JsonResponse(manifest)
    response["Cache-Control"] = "no-cache"

    return response


def service_worker(request):
    """
    Pheral service worker.

    Served through Django so the service worker is available at:
        /service-worker.js

    This keeps it in the root scope of the Pheral PWA.
    """

    javascript = """
const CACHE_NAME = "pheral-v1";

const APP_SHELL = [
    "/",
    "/home/"
];

self.addEventListener("install", event => {
    event.waitUntil(
        caches.open(CACHE_NAME)
            .then(cache => cache.addAll(APP_SHELL))
            .then(() => self.skipWaiting())
    );
});

self.addEventListener("activate", event => {
    event.waitUntil(
        caches.keys().then(keys =>
            Promise.all(
                keys
                    .filter(key => key !== CACHE_NAME)
                    .map(key => caches.delete(key))
            )
        ).then(() => self.clients.claim())
    );
});

self.addEventListener("fetch", event => {
    if (event.request.method !== "GET") {
        return;
    }

    event.respondWith(
        fetch(event.request)
            .then(response => {
                const responseClone = response.clone();

                caches.open(CACHE_NAME).then(cache => {
                    cache.put(event.request, responseClone);
                });

                return response;
            })
            .catch(() => caches.match(event.request))
    );
});
"""

    response = HttpResponse(
        javascript,
        content_type="application/javascript",
    )

    response["Cache-Control"] = "no-cache"

    return response





# ============================================================
# CONTACTS
# ============================================================

import json
import re

from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.db.models import Q
from django.http import JsonResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.views.decorators.http import require_POST


def normalize_phone_number(phone):
    """
    Normalize phone numbers before comparing them.

    Examples:

        +2348012345678
        2348012345678
        08012345678

    All become:

        +2348012345678

    This is particularly important for Nigerian contacts.
    """

    if not phone:
        return ""

    phone = str(phone).strip()

    # Keep only digits.
    digits = re.sub(r"\D", "", phone)

    if not digits:
        return ""

    # Nigerian local format:
    # 08012345678 -> +2348012345678
    if digits.startswith("0") and len(digits) == 11:
        digits = "234" + digits[1:]

    # Nigerian international format without +:
    # 2348012345678 -> +2348012345678
    if digits.startswith("234"):
        return "+" + digits

    # Already another international number.
    return "+" + digits



@login_required
@require_POST
def sync_contacts(request):
    """
    Match selected device contacts against registered Pheral
    users.

    Expected request:

    {
        "contacts": [
            {
                "name": "Zoe",
                "phone": "08012345678"
            }
        ]
    }
    """

    try:
        payload = json.loads(
            request.body or "{}"
        )
    except json.JSONDecodeError:
        return JsonResponse(
            {
                "success": False,
                "error": "Invalid contact data.",
            },
            status=400,
        )

    incoming_contacts = payload.get(
        "contacts",
        [],
    )

    if not isinstance(
        incoming_contacts,
        list,
    ):
        return JsonResponse(
            {
                "success": False,
                "error": "Contacts must be a list.",
            },
            status=400,
        )

    matched = []
    not_on_pheral = []

    seen_numbers = set()

    for item in incoming_contacts:

        if not isinstance(item, dict):
            continue

        name = str(
            item.get("name", "")
        ).strip()

        raw_phone = str(
            item.get("phone", "")
        ).strip()

        phone = normalize_phone_number(
            raw_phone
        )

        if not phone:
            continue

        # Prevent duplicate processing when the device
        # contains the same number more than once.
        if phone in seen_numbers:
            continue

        seen_numbers.add(phone)

        user = (
            User.objects
            .filter(
                phone_number=phone,
                is_active=True,
            )
            .exclude(
                pk=request.user.pk,
            )
            .first()
        )

        if user:

            contact, created = (
                Contact.objects.get_or_create(
                    owner=request.user,
                    contact_user=user,
                    defaults={
                        "phone_number": phone,
                        "nickname": name[:100],
                    },
                )
            )

            # If the contact already exists but has no
            # nickname, preserve the device contact name.
            if (
                not contact.nickname
                and name
            ):
                contact.nickname = name[:100]

                contact.save(
                    update_fields=[
                        "nickname",
                    ]
                )

            matched.append(
                {
                    "id": user.id,
                    "username": user.username,
                    "name": (
                        user.get_full_name()
                        or user.username
                    ),
                    "nickname": (
                        contact.nickname
                        or name
                    ),
                }
            )

        else:

            not_on_pheral.append(
                {
                    "name": name,
                    "phone": phone,
                }
            )

    return JsonResponse(
        {
            "success": True,
            "matched": matched,
            "not_on_pheral": not_on_pheral,
            "matched_count": len(matched),
            "not_on_pheral_count": len(
                not_on_pheral
            ),
        }
    )


@login_required
@require_POST
def add_contact(request, username):
    """
    Manually add an existing Pheral user to contacts.
    """

    contact_user = get_object_or_404(
        User,
        username=username,
        is_active=True,
    )

    if contact_user == request.user:

        messages.error(
            request,
            "You cannot add yourself to your contacts.",
        )

        return redirect(
            "contacts"
        )

    Contact.objects.get_or_create(
        owner=request.user,
        contact_user=contact_user,
        defaults={
            "phone_number": (
                getattr(
                    contact_user,
                    "phone_number",
                    ""
                )
                or ""
            ),
        },
    )

    messages.success(
        request,
        f"@{contact_user.username} added to contacts.",
    )

    return redirect(
        "contacts"
    )
from django.db import IntegrityError, transaction
from django.db import transaction
from django.db.models import Count
def get_or_create_direct_conversation(user1, user2):
    """
    Return the existing direct conversation between two users,
    or create it if one does not exist.
    """

    if user1.pk == user2.pk:
        raise ValueError(
            "A user cannot create a conversation with themselves."
        )

    user_ids = sorted([user1.pk, user2.pk])
    pair_key = f"{user_ids[0]}-{user_ids[1]}"

    # IMPORTANT:
    # First find the existing conversation by its deterministic pair key.
    conversation = (
        Conversation.objects
        .filter(
            conversation_type=Conversation.ConversationType.DIRECT,
            direct_pair_key=pair_key,
        )
        .first()
    )

    if conversation:
        return conversation

    # Create only when the pair does not already have a conversation.
    try:
        with transaction.atomic():
            conversation = Conversation.objects.create(
                conversation_type=Conversation.ConversationType.DIRECT,
                created_by=user1,
                direct_pair_key=pair_key,
            )

            ConversationParticipant.objects.create(
                conversation=conversation,
                user=user1,
            )

            ConversationParticipant.objects.create(
                conversation=conversation,
                user=user2,
            )

            return conversation

    except IntegrityError:
        return (
            Conversation.objects
            .get(
                conversation_type=Conversation.ConversationType.DIRECT,
                direct_pair_key=pair_key,
            )
        )
@login_required
def start_chat(request, username):
    """
    Start or open a direct conversation with another user.
    """

    other_user = get_object_or_404(
        User,
        username__iexact=username,
    )

    if other_user == request.user:
        return redirect("home")

    conversation = get_or_create_direct_conversation(
        request.user,
        other_user,
    )

    return redirect(
        "chat",
        conversation_id=conversation.id,
    )


@login_required
def chat(request, conversation_id=None, username=None):
    """
    Direct/group conversation room.

    Supports:

        /chat/<conversation_id>/
        /chat/user/<username>/
    """

    conversation = None
    other_user = None

    # ============================================================
    # OPEN CHAT BY USERNAME
    # ============================================================

    if username:

        other_user = get_object_or_404(
            User,
            username__iexact=username,
        )

        if other_user == request.user:
            return redirect("home")

        conversation = get_or_create_direct_conversation(
            request.user,
            other_user,
        )

    # ============================================================
    # OPEN CHAT BY CONVERSATION ID
    # ============================================================

    elif conversation_id:

        conversation = get_object_or_404(
            Conversation.objects.filter(
                participants__user=request.user,
                is_active=True,
            ).distinct(),
            id=conversation_id,
        )

        # --------------------------------------------------------
        # Direct conversation
        # --------------------------------------------------------

        if (
            conversation.conversation_type
            == Conversation.ConversationType.DIRECT
        ):

            other_user = (
                User.objects
                .filter(
                    conversation_participations__conversation=conversation,
                )
                .exclude(
                    id=request.user.id,
                )
                .first()
            )

            if other_user is None:
                return redirect("home")

    # ============================================================
    # NO CHAT SELECTED
    # ============================================================

    else:
        return redirect("chat_list")

    # ============================================================
    # SEND MESSAGE
    # ============================================================

    if request.method == "POST":

        content = request.POST.get(
            "content",
            "",
        ).strip()

        attachment = request.FILES.get(
            "attachment",
        )

        if content or attachment:

            message_type = Message.MessageType.TEXT

            if attachment:

                content_type = (
                    getattr(
                        attachment,
                        "content_type",
                        "",
                    )
                    or ""
                )

                if content_type.startswith("image/"):
                    message_type = Message.MessageType.IMAGE

                elif content_type.startswith("video/"):
                    message_type = Message.MessageType.VIDEO

                elif content_type.startswith("audio/"):
                    message_type = Message.MessageType.VOICE

                else:
                    message_type = Message.MessageType.FILE

            Message.objects.create(
                conversation=conversation,
                sender=request.user,
                content=content,
                attachment=attachment,
                message_type=message_type,
            )

        return redirect(
            "chat",
            conversation_id=conversation.id,
        )

    # ============================================================
    # MESSAGES
    # ============================================================

    messages = (
        Message.objects
        .filter(
            conversation=conversation,
            is_deleted=False,
        )
        .select_related(
            "sender",
            "reply_to",
            "receipt",
            "transaction",
        )
        .prefetch_related(
            "read_receipts",
        )
        .order_by(
            "created_at",
        )
    )

    # ============================================================
    # CONTEXT
    # ============================================================

    context = {
        "conversation": conversation,
        "other_user": other_user,
        "chat_messages": messages,
        "messages": messages,
    }

    return render(
        request,
        "chat.html",
        context,
    )


@login_required
def delete_message(request, message_id):
    """
    Permanently delete a message belonging to the
    currently authenticated user.
    """

    if request.method != "POST":
        return redirect("chat_list")

    message = get_object_or_404(
        Message,
        id=message_id,
        sender=request.user,
    )

    conversation_id = message.conversation_id

    message.delete()

    return redirect(
        "chat",
        conversation_id=conversation_id,
    )

@login_required
def mark_chat_read(request, conversation_id):
    """
    Mark all messages in a conversation as read
    for the currently authenticated user.
    """

    conversation = get_object_or_404(
        Conversation,
        id=conversation_id,
        participants__user=request.user,
        is_active=True,
    )

    participant = get_object_or_404(
        ConversationParticipant,
        conversation=conversation,
        user=request.user,
    )

    participant.last_read_at = timezone.now()
    participant.save(
        update_fields=["last_read_at"]
    )

    return redirect(
        "chat",
        conversation_id=conversation.id,
    )


from django.utils import timezone


def update_last_seen(user):
    """
    Update the user's last activity timestamp.
    """
    User.objects.filter(
        id=user.id,
    ).update(
        last_seen=timezone.now(),
    )



@login_required
def chat_typing(request, conversation_id):
    """
    Lightweight typing-status endpoint.

    POST:
        Set the current user's typing state.

    GET:
        Return whether the other participant is typing.
    """

    conversation = get_object_or_404(
        Conversation,
        id=conversation_id,
        participants__user=request.user,
        is_active=True,
    )

    if request.method == "POST":

        typing = request.POST.get("typing") == "1"

        # Store temporary typing state in the session.
        request.session[
            f"typing_{conversation.id}"
        ] = typing

        return JsonResponse({
            "success": True,
            "typing": typing,
        })

    other_participant = (
        conversation.participants
        .exclude(user=request.user)
        .select_related("user")
        .first()
    )

    other_typing = False

    if other_participant:
        other_typing = request.session.get(
            f"typing_{conversation.id}",
            False,
        )

    return JsonResponse({
        "typing": other_typing,
    })


@login_required
def chat(request, conversation_id=None, username=None):
    """
    Pheral direct chat room.

    Supported URLs:

        /chat/<conversation_id>/
        /chat/user/<username>/

    The authenticated user must be a participant of an existing
    conversation before it can be opened by conversation ID.
    """

    # ============================================================
    # VARIABLES
    # ============================================================

    conversation = None
    other_user = None


    # ============================================================
    # OPEN CHAT BY USERNAME
    # ============================================================

    if username:

        other_user = get_object_or_404(
            User,
            username__iexact=username,
        )

        # Prevent messaging yourself.
        if other_user.id == request.user.id:
            return redirect("home")


        # --------------------------------------------------------
        # Find an existing DIRECT conversation containing both
        # users.
        # --------------------------------------------------------

        conversation = (
            Conversation.objects
            .filter(
                conversation_type=Conversation.ConversationType.DIRECT,
                is_active=True,
                participants__user=request.user,
            )
            .filter(
                participants__user=other_user,
            )
            .distinct()
            .first()
        )


        # --------------------------------------------------------
        # Create conversation if it doesn't exist.
        # --------------------------------------------------------

        if conversation is None:

            conversation = Conversation.objects.create(
                conversation_type=Conversation.ConversationType.DIRECT,
                created_by=request.user,
            )

            ConversationParticipant.objects.bulk_create([
                ConversationParticipant(
                    conversation=conversation,
                    user=request.user,
                ),
                ConversationParticipant(
                    conversation=conversation,
                    user=other_user,
                ),
            ])


    # ============================================================
    # OPEN CHAT BY CONVERSATION ID
    # ============================================================

    elif conversation_id:

        conversation = get_object_or_404(
            Conversation.objects.filter(
                is_active=True,
                participants__user=request.user,
            ),
            id=conversation_id,
        )


        # --------------------------------------------------------
        # Get the other participant.
        # --------------------------------------------------------

        other_participant = (
            ConversationParticipant.objects
            .filter(
                conversation=conversation,
            )
            .exclude(
                user=request.user,
            )
            .select_related("user")
            .first()
        )

        if other_participant:
            other_user = other_participant.user


        # --------------------------------------------------------
        # A direct conversation must have another user.
        # --------------------------------------------------------

        if (
            conversation.conversation_type
            == Conversation.ConversationType.DIRECT
            and other_user is None
        ):
            return redirect("chat_list")


    # ============================================================
    # NO CONVERSATION SELECTED
    # ============================================================

    else:
        return redirect("chat_list")


    # ============================================================
    # SAFETY CHECK
    # ============================================================

    if conversation is None:
        return redirect("chat_list")


    # ============================================================
    # CURRENT PARTICIPANT
    # ============================================================

    current_participant = (
        ConversationParticipant.objects
        .filter(
            conversation=conversation,
            user=request.user,
        )
        .first()
    )

    if current_participant is None:
        return redirect("chat_list")


    # ============================================================
    # SEND MESSAGE
    # ============================================================

    if request.method == "POST":

        content = (
            request.POST
            .get("content", "")
            .strip()
        )

        attachment = request.FILES.get(
            "attachment"
        )


        # --------------------------------------------------------
        # Don't create completely empty messages.
        # --------------------------------------------------------

        if not content and not attachment:

            return redirect(
                "chat",
                conversation_id=conversation.id,
            )


        # --------------------------------------------------------
        # Determine message type.
        # --------------------------------------------------------

        message_type = Message.MessageType.TEXT


        if attachment:

            content_type = (
                getattr(
                    attachment,
                    "content_type",
                    "",
                )
                or ""
            ).lower()


            if content_type.startswith("image/"):

                message_type = (
                    Message.MessageType.IMAGE
                )


            elif content_type.startswith("video/"):

                message_type = (
                    Message.MessageType.VIDEO
                )


            elif content_type.startswith("audio/"):

                message_type = (
                    Message.MessageType.VOICE
                )


            else:

                message_type = (
                    Message.MessageType.FILE
                )


        # --------------------------------------------------------
        # Create message.
        # --------------------------------------------------------

        Message.objects.create(
            conversation=conversation,
            sender=request.user,
            message_type=message_type,
            content=content,
            attachment=attachment,
        )


        # --------------------------------------------------------
        # Update conversation timestamp.
        # --------------------------------------------------------

        conversation.save(
            update_fields=[
                "updated_at",
            ]
        )


        # --------------------------------------------------------
        # Mark sender's conversation as read.
        # --------------------------------------------------------

        current_participant.last_read_at = timezone.now()

        current_participant.save(
            update_fields=[
                "last_read_at",
            ]
        )


        return redirect(
            "chat",
            conversation_id=conversation.id,
        )


    # ============================================================
    # LOAD MESSAGES
    # ============================================================

    chat_messages = (
        Message.objects
        .filter(
            conversation=conversation,
            is_deleted=False,
        )
        .select_related(
            "sender",
            "reply_to",
            "transaction",
            "receipt",
        )
        .prefetch_related(
            "read_receipts",
        )
        .order_by(
            "created_at",
            "id",
        )
    )


    # ============================================================
    # MARK CHAT AS READ
    # ============================================================

    if chat_messages.exists():

        current_participant.last_read_at = timezone.now()

        current_participant.save(
            update_fields=[
                "last_read_at",
            ]
        )


    # ============================================================
    # ONLINE / LAST SEEN
    # ============================================================

    other_user_online = False
    other_user_last_seen = None


    if other_user:

        other_user_last_seen = (
            other_user.last_seen
        )

        if other_user.last_seen:

            elapsed = (
                timezone.now()
                - other_user.last_seen
            )

            # Consider the user online when they were
            # active within the last 2 minutes.
            other_user_online = (
                elapsed.total_seconds()
                <= 120
            )


    # ============================================================
    # CONTEXT
    # ============================================================

    context = {
        "conversation": conversation,

        "other_user": other_user,

        "chat_messages": chat_messages,

        "messages": chat_messages,

        "current_participant": current_participant,

        "other_user_online": other_user_online,

        "other_user_last_seen": other_user_last_seen,
    }


    # ============================================================
    # RENDER
    # ============================================================

    return render(
        request,
        "chat.html",
        context,
    )




@login_required
def chat(request, conversation_id=None, username=None):
    """
    Complete Pheral chat room.

    Supports:
        /chat/
        /chat/<conversation_id>/
        /chat/user/<username>/

    Handles:
        - Direct conversations
        - Group conversations
        - Text messages
        - Images
        - Videos
        - Files
        - Voice notes
        - Replies
        - Existing payment/hire/agent messages
        - Read state
    """

    # ============================================================
    # RESOLVE CONVERSATION
    # ============================================================

    conversation = None
    other_user = None

    # ------------------------------------------------------------
    # OPEN DIRECT CHAT BY USERNAME
    # ------------------------------------------------------------

    if username:

        other_user = get_object_or_404(
            User,
            username__iexact=username,
        )

        if other_user == request.user:
            return redirect("home")

        conversation = (
            Conversation.objects
            .filter(
                conversation_type=Conversation.ConversationType.DIRECT,
                participants__user=request.user,
            )
            .filter(
                participants__user=other_user,
            )
            .distinct()
            .first()
        )

        if conversation is None:

            conversation = Conversation.objects.create(
                conversation_type=Conversation.ConversationType.DIRECT,
                created_by=request.user,
            )

            ConversationParticipant.objects.create(
                conversation=conversation,
                user=request.user,
            )

            ConversationParticipant.objects.create(
                conversation=conversation,
                user=other_user,
            )

    # ------------------------------------------------------------
    # OPEN CHAT BY CONVERSATION ID
    # ------------------------------------------------------------

    elif conversation_id:

        conversation = get_object_or_404(
            Conversation.objects.filter(
                participants__user=request.user,
                is_active=True,
            ),
            pk=conversation_id,
        )

        if (
            conversation.conversation_type
            == Conversation.ConversationType.DIRECT
        ):

            other_user = (
                User.objects
                .filter(
                    conversation_participations__conversation=conversation,
                )
                .exclude(
                    pk=request.user.pk,
                )
                .first()
            )

    # ------------------------------------------------------------
    # NO CONVERSATION
    # ------------------------------------------------------------

    else:

        return redirect("chat_list")

    # ============================================================
    # PARTICIPANT
    # ============================================================

    participant = (
        ConversationParticipant.objects
        .filter(
            conversation=conversation,
            user=request.user,
        )
        .first()
    )

    if participant is None:
        return redirect("home")

    # ============================================================
    # POST MESSAGE
    # ============================================================

    if request.method == "POST":

        content = (
            request.POST.get(
                "content",
                "",
            )
            .strip()
        )

        attachment = request.FILES.get(
            "attachment"
        )

        voice_note = request.FILES.get(
            "voice_note"
        )

        reply_to_id = request.POST.get(
            "reply_to"
        )

        message_type = Message.MessageType.TEXT

        uploaded_file = None

        # --------------------------------------------------------
        # REPLY
        # --------------------------------------------------------

        reply_to = None

        if reply_to_id:

            reply_to = (
                Message.objects
                .filter(
                    pk=reply_to_id,
                    conversation=conversation,
                    is_deleted=False,
                )
                .first()
            )

        # --------------------------------------------------------
        # VOICE NOTE
        # --------------------------------------------------------

        if voice_note:

            uploaded_file = voice_note

            message_type = Message.MessageType.VOICE

        # --------------------------------------------------------
        # NORMAL ATTACHMENT
        # --------------------------------------------------------

        elif attachment:

            uploaded_file = attachment

            content_type = (
                getattr(
                    attachment,
                    "content_type",
                    "",
                )
                or ""
            ).lower()

            if content_type.startswith("image/"):

                message_type = Message.MessageType.IMAGE

            elif content_type.startswith("video/"):

                message_type = Message.MessageType.VIDEO

            else:

                message_type = Message.MessageType.FILE

        # --------------------------------------------------------
        # CREATE MESSAGE
        # --------------------------------------------------------

        if content or uploaded_file:

            message = Message.objects.create(
                conversation=conversation,
                sender=request.user,
                message_type=message_type,
                content=content,
                attachment=uploaded_file,
                reply_to=reply_to,
            )

            # ----------------------------------------------------
            # UPDATE CONVERSATION
            # ----------------------------------------------------

            conversation.updated_at = timezone.now()
            conversation.save(
                update_fields=[
                    "updated_at",
                ]
            )

            # ----------------------------------------------------
            # MARK SENDER'S CHAT AS READ
            # ----------------------------------------------------

            participant.last_read_at = timezone.now()
            participant.save(
                update_fields=[
                    "last_read_at",
                ]
            )

        return redirect(
            "chat",
            conversation_id=conversation.pk,
        )

    # ============================================================
    # LOAD MESSAGES
    # ============================================================

    messages = (
        Message.objects
        .filter(
            conversation=conversation,
            is_deleted=False,
        )
        .select_related(
            "sender",
            "reply_to",
            "reply_to__sender",
            "receipt",
            "transaction",
        )
        .prefetch_related(
            "read_receipts",
        )
        .order_by(
            "created_at",
        )
    )

    # ============================================================
    # MARK CHAT AS READ
    # ============================================================

    participant.last_read_at = timezone.now()

    participant.save(
        update_fields=[
            "last_read_at",
        ]
    )

    # ============================================================
    # PARTICIPANTS
    # ============================================================

    participants = (
        ConversationParticipant.objects
        .filter(
            conversation=conversation,
        )
        .select_related(
            "user",
        )
        .order_by(
            "joined_at",
        )
    )

    # ============================================================
    # GROUP INFORMATION
    # ============================================================

    is_group = (
        conversation.conversation_type
        == Conversation.ConversationType.GROUP
    )

    # ============================================================
    # CONTEXT
    # ============================================================

    context = {
        "conversation": conversation,

        "other_user": other_user,

        "participants": participants,

        "participant": participant,

        "is_group": is_group,

        "chat_messages": messages,

        "messages": messages,

        "current_user": request.user,
    }

    return render(
        request,
        "chat.html",
        context,
    )