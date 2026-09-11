import hashlib
import hmac, base64
import json
import random
import re
from decimal import Decimal, InvalidOperation

import requests

from django.conf import settings
from django.contrib import messages
from django.contrib.auth import authenticate, login, logout
from django.contrib.auth.decorators import login_required
from django.db import transaction
from django.db.models import Count, Prefetch, Q
from django.http import Http404, HttpResponse, JsonResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse
from django.utils import timezone
from django.views.decorators.csrf import csrf_exempt
from django.views.decorators.http import require_POST

from .models import (
    AgentActivity,
    AgentCommand,
    BankAccount,
    Contact,
    Conversation,
    ConversationParticipant,
    Currency,
    ExchangeRate,
    GroupLedger,
    GroupLedgerEntry,
    HireJob,
    HirePayment,
    HireRequest,
    LedgerEntry,
    Message,
    MessageRead,
    Notification,
    PhoneOTP,
    Post,
    PostComment,
    PostLike,
    PheralTransaction,
    PushDevice,
    Receipt,
    Status,
    StatusView,
    User,
    UserPresence,
    Wallet,
    WalletToken,
)

from django.db.models import Count, Exists, OuterRef, Q

PAYSTACK_BASE_URL = "https://api.paystack.co"


# ============================================================
# HELPERS
# ============================================================

def get_or_create_wallet(user, currency):
    wallet, _ = Wallet.objects.get_or_create(
        user=user, currency=currency, defaults={"balance": Decimal("0.00")}
    )
    return wallet


def get_default_currency():
    """
    Prefer NGN for the Nigerian MVP. Falls back to the first
    active currency.
    """
    currency = Currency.objects.filter(code__iexact="NGN", is_active=True).first()
    if currency:
        return currency
    return Currency.objects.filter(is_active=True).first()


def get_or_create_wallet_token(user):
    token, _ = WalletToken.objects.get_or_create(user=user, defaults={"is_active": True})
    if not token.is_active:
        token.is_active = True
        token.save(update_fields=["is_active"])
    return token


def parse_amount(value):
    try:
        amount = Decimal(str(value))
    except (InvalidOperation, TypeError, ValueError):
        return None
    if amount <= Decimal("0"):
        return None
    return amount


def get_or_create_direct_conversation(user1, user2):
    """
    Return the single direct conversation shared by user1 and
    user2, creating one atomically if it doesn't exist yet.
    """

    if user1 == user2:
        raise ValueError("A user cannot create a conversation with themselves.")

    conversation = (
        Conversation.objects
        .filter(conversation_type=Conversation.ConversationType.DIRECT, participants__user=user1)
        .filter(participants__user=user2)
        .annotate(participant_count=Count("participants"))
        .filter(participant_count=2)
        .order_by("created_at", "id")
        .first()
    )

    if conversation:
        return conversation

    with transaction.atomic():
        conversation = Conversation.objects.create(
            conversation_type=Conversation.ConversationType.DIRECT,
            created_by=user1,
        )
        ConversationParticipant.objects.create(conversation=conversation, user=user1)
        ConversationParticipant.objects.create(conversation=conversation, user=user2)

    return conversation


def create_system_message(conversation, content):
    return Message.objects.create(
        conversation=conversation,
        sender=None,
        message_type=Message.MessageType.SYSTEM,
        content=content,
    )


def normalize_phone_number(phone):
    """
    Normalize phone numbers before comparing them.
    08012345678 / 2348012345678 / +2348012345678 all become
    +2348012345678.
    """

    if not phone:
        return ""

    phone = str(phone).strip()
    digits = re.sub(r"\D", "", phone)

    if not digits:
        return ""

    if digits.startswith("0") and len(digits) == 11:
        digits = "234" + digits[1:]

    if digits.startswith("234"):
        return "+" + digits

    return "+" + digits


# ============================================================
# PRESENCE HELPERS
# ============================================================

def set_user_online(user):
    presence, _ = UserPresence.objects.get_or_create(user=user)
    presence.is_online = True
    presence.last_seen_at = timezone.now()
    presence.save(update_fields=["is_online", "last_seen_at", "updated_at"])
    return presence


def set_user_offline(user):
    presence, _ = UserPresence.objects.get_or_create(user=user)
    presence.is_online = False
    presence.last_seen_at = timezone.now()
    presence.save(update_fields=["is_online", "last_seen_at", "updated_at"])
    return presence


def get_user_presence(user):
    if not user or not user.is_authenticated:
        return None
    presence, _ = UserPresence.objects.get_or_create(user=user, defaults={"is_online": False})
    return presence


def update_last_seen(user):
    User.objects.filter(id=user.id).update(last_seen=timezone.now())


# ============================================================
# PAYSTACK HELPERS (top-up + withdrawal completion)
# ============================================================

def _complete_top_up(pheral_transaction):
    """
    Idempotently mark a pending top-up as completed and credit
    the wallet. Safe to call more than once — from the webhook
    AND the callback — only the first call that finds the
    transaction still PENDING actually moves any money.
    """

    with transaction.atomic():

        locked_txn = PheralTransaction.objects.select_for_update().get(pk=pheral_transaction.pk)

        if locked_txn.status != PheralTransaction.Status.PENDING:
            return locked_txn

        wallet_obj = Wallet.objects.select_for_update().get(pk=locked_txn.sender_wallet_id)

        balance_before = wallet_obj.balance
        wallet_obj.balance += locked_txn.amount
        wallet_obj.save(update_fields=["balance", "updated_at"])

        locked_txn.status = PheralTransaction.Status.COMPLETED
        locked_txn.completed_at = timezone.now()
        locked_txn.save(update_fields=["status", "completed_at"])

        LedgerEntry.objects.create(
            transaction=locked_txn,
            wallet=wallet_obj,
            entry_type=LedgerEntry.EntryType.CREDIT,
            amount=locked_txn.amount,
            balance_before=balance_before,
            balance_after=wallet_obj.balance,
            description="Wallet top-up via Paystack",
        )

        return locked_txn


def _fail_top_up(pheral_transaction):
    with transaction.atomic():
        locked_txn = PheralTransaction.objects.select_for_update().get(pk=pheral_transaction.pk)
        if locked_txn.status == PheralTransaction.Status.PENDING:
            locked_txn.status = PheralTransaction.Status.FAILED
            locked_txn.completed_at = timezone.now()
            locked_txn.save(update_fields=["status", "completed_at"])
        return locked_txn

def convert_amount(amount, source_currency, target_currency):
    """
    Convert `amount` from source_currency to target_currency,
    rounding to target_currency's own decimal_places (so JPY/UGX/XOF
    — seeded with 0 decimal places — don't get fake fractional
    amounts). Returns None if no rate path exists.
    """
    if source_currency.pk == target_currency.pk:
        return amount

    def direct_rate(src, tgt):
        row = ExchangeRate.objects.filter(source_currency=src, target_currency=tgt, is_active=True).first()
        if row:
            return row.rate
        inverse = ExchangeRate.objects.filter(source_currency=tgt, target_currency=src, is_active=True).first()
        if inverse and inverse.rate:
            return Decimal("1") / inverse.rate
        return None

    quantize_to = Decimal("1").scaleb(-target_currency.decimal_places)

    rate = direct_rate(source_currency, target_currency)
    if rate is not None:
        return (amount * rate).quantize(quantize_to)

    if source_currency.code != "NGN" and target_currency.code != "NGN":
        ngn = Currency.objects.filter(code="NGN").first()
        if ngn:
            leg1 = direct_rate(source_currency, ngn)
            leg2 = direct_rate(ngn, target_currency)
            if leg1 is not None and leg2 is not None:
                return (amount * leg1 * leg2).quantize(quantize_to)

    return None

# ============================================================
# LANDING / PUBLIC
# ============================================================

def landing(request):
    return render(request, "landing.html")

def normalize_phone_number(phone_number):
    phone = phone_number.strip().replace(" ", "").replace("-", "")

    if phone.startswith("+234"):
        phone = "0" + phone[4:]
    elif phone.startswith("234"):
        phone = "0" + phone[3:]

    return phone
# ============================================================
# AUTHENTICATION
# ============================================================

def register(request):
    if request.user.is_authenticated:
        return redirect("chat")

    if request.method == "POST":
        username = request.POST.get("username", "").strip()
        phone_number = request.POST.get("phone_number", "").strip()
        password = request.POST.get("password", "")
        password_confirm = request.POST.get("password_confirm", "")
        first_name = request.POST.get("first_name", "").strip()
        last_name = request.POST.get("last_name", "").strip()

        if not username:
            messages.error(request, "Username is required.")
            return render(request, "register.html")

        if not phone_number:
            messages.error(request, "Phone number is required.")
            return render(request, "register.html")

        if not first_name or not last_name:
            messages.error(request, "First name and last name are required.")
            return render(request, "register.html")

        if not password:
            messages.error(request, "Password is required.")
            return render(request, "register.html")

        if password != password_confirm:
            messages.error(request, "Passwords do not match.")
            return render(request, "register.html")

        # Normalize phone number before any database operation
        phone_number = normalize_phone_number(phone_number)

        if User.objects.filter(username__iexact=username).exists():
            messages.error(request, "That username is already taken.")
            return render(request, "register.html")

        if User.objects.filter(phone_number=phone_number).exists():
            messages.error(request, "That phone number is already registered.")
            return render(request, "register.html")

        user = User.objects.create_user(
            username=username,
            phone_number=phone_number,
            password=password,
            first_name=first_name,
            last_name=last_name,
        )

        user.is_phone_verified = False
        user.save(update_fields=["is_phone_verified"])

        code = f"{random.randint(0, 999999):06d}"

        PhoneOTP.objects.create(
            user=user,
            phone_number=phone_number,
            code=code,
            expires_at=timezone.now() + timezone.timedelta(minutes=10),
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

    return render(request, "register.html")

def normalize_phone_number(phone_number):
    phone = phone_number.strip().replace(" ", "").replace("-", "")

    if phone.startswith("+234"):
        phone = "0" + phone[4:]
    elif phone.startswith("234"):
        phone = "0" + phone[3:]

    return phone


def login_view(request):
    if request.user.is_authenticated:
        return redirect("chat")

    if request.method != "POST":
        return render(request, "login.html")

    phone_number = request.POST.get("phone_number", "").strip()
    password = request.POST.get("password", "")

    if not phone_number:
        messages.error(request, "Phone number is required.")
        return render(request, "login.html")

    if not password:
        messages.error(request, "Password is required.")
        return render(request, "login.html")

    phone_number = normalize_phone_number(phone_number)

    try:
        user_obj = User.objects.get(phone_number=phone_number)
    except User.DoesNotExist:
        messages.error(request, "Invalid phone number or password.")
        return render(request, "login.html")

    user = authenticate(
        request,
        username=user_obj.username,
        password=password,
    )

    if user is None:
        messages.error(request, "Invalid phone number or password.")
        return render(request, "login.html")

    if not user.is_active:
        messages.error(request, "This account is inactive.")
        return render(request, "login.html")

    login(request, user)

    user.last_seen = timezone.now()
    user.save(update_fields=["last_seen"])

    return redirect("chat")

def logout_view(request):
    if request.user.is_authenticated:
        request.user.last_seen = timezone.now()
        request.user.save(update_fields=["last_seen"])
    logout(request)
    return redirect("landing")


def verify_otp(request):
    user_id = request.session.get("otp_user_id")

    if not user_id:
        return redirect("register")

    user = get_object_or_404(User, pk=user_id)

    if request.method == "POST":
        code = request.POST.get("code", "").strip()

        otp = (
            PhoneOTP.objects.filter(user=user, code=code, is_used=False)
            .order_by("-created_at").first()
        )

        if not otp:
            messages.error(request, "Invalid OTP.")
            return render(request, "verify_otp.html")

        if otp.is_expired:
            messages.error(request, "This OTP has expired.")
            return render(request, "verify_otp.html")

        otp.is_used = True
        otp.save(update_fields=["is_used"])

        user.is_phone_verified = True
        user.save(update_fields=["is_phone_verified"])

        get_or_create_wallet_token(user)

        currency = get_default_currency()
        if currency:
            get_or_create_wallet(user, currency)

        login(request, user)
        request.session.pop("otp_user_id", None)

        return redirect("chat")

    return render(request, "verify_otp.html")


def resend_otp(request):
    user_id = request.session.get("otp_user_id")

    if not user_id:
        return redirect("register")

    user = get_object_or_404(User, pk=user_id)

    if user.is_phone_verified:
        return redirect("home")

    if request.method != "POST":
        return redirect("verify_otp")

    PhoneOTP.objects.filter(
        user=user, purpose=PhoneOTP.PURPOSE_VERIFICATION, is_used=False
    ).update(is_used=True)

    code = f"{random.randint(0, 999999):06d}"
    PhoneOTP.objects.create(
        user=user, phone_number=user.phone_number, code=code,
        purpose=PhoneOTP.PURPOSE_VERIFICATION,
        expires_at=timezone.now() + timezone.timedelta(minutes=10),
    )

    print()
    print("=" * 50)
    print("PHERAL DEVELOPMENT OTP")
    print(f"Phone: {user.phone_number}")
    print(f"OTP:   {code}")
    print("=" * 50)
    print()

    messages.success(request, "A new verification code has been sent.")
    return redirect("verify_otp")

def forgot_password(request):
    if request.user.is_authenticated:
        return redirect("home")

    if request.method == "POST":
        phone_number = request.POST.get("phone_number", "").strip()

        if not phone_number:
            messages.error(request, "Phone number is required.")
            return render(request, "forgot_password.html")

        phone_number = normalize_phone_number(phone_number)

        try:
            user = User.objects.get(phone_number=phone_number)
        except User.DoesNotExist:
            messages.error(
                request,
                "No account was found with that phone number."
            )
            return render(request, "forgot_password.html")

        if not user.is_active:
            messages.error(request, "This account is inactive.")
            return render(request, "forgot_password.html")

        PhoneOTP.objects.filter(
            user=user,
            purpose=PhoneOTP.PURPOSE_PASSWORD_RESET,
            is_used=False,
        ).update(is_used=True)

        code = f"{random.randint(0, 999999):06d}"

        PhoneOTP.objects.create(
            user=user,
            phone_number=user.phone_number,
            code=code,
            purpose=PhoneOTP.PURPOSE_PASSWORD_RESET,
            expires_at=timezone.now() + timezone.timedelta(minutes=10),
        )

        request.session["password_reset_user_id"] = user.pk

        print()
        print("=" * 50)
        print("PHERAL PASSWORD RESET OTP")
        print(f"Phone: {user.phone_number}")
        print(f"OTP:   {code}")
        print("=" * 50)
        print()

        return redirect("verify_password_reset_otp")

    return render(request, "forgot_password.html")

def verify_password_reset_otp(request):
    user_id = request.session.get("password_reset_user_id")

    if not user_id:
        return redirect("forgot_password")

    user = get_object_or_404(User, pk=user_id)

    if request.method == "POST":
        code = request.POST.get("code", "").strip()

        otp = (
            PhoneOTP.objects.filter(
                user=user, phone_number=user.phone_number, code=code,
                purpose=PhoneOTP.PURPOSE_PASSWORD_RESET, is_used=False,
            ).order_by("-created_at").first()
        )

        if not otp:
            messages.error(request, "Invalid OTP.")
            return render(request, "verify_password_reset_otp.html")

        if otp.is_expired:
            messages.error(request, "This OTP has expired.")
            return render(request, "verify_password_reset_otp.html")

        otp.is_used = True
        otp.save(update_fields=["is_used"])

        request.session["password_reset_verified"] = True
        return redirect("reset_password")

    return render(request, "verify_password_reset_otp.html")


def reset_password(request):
    user_id = request.session.get("password_reset_user_id")
    verified = request.session.get("password_reset_verified")

    if not user_id or not verified:
        return redirect("forgot_password")

    user = get_object_or_404(User, pk=user_id)

    if request.method == "POST":
        password = request.POST.get("password", "")
        password_confirm = request.POST.get("password_confirm", "")

        if not password:
            messages.error(request, "Password is required.")
            return render(request, "reset_password.html")

        if len(password) < 8:
            messages.error(request, "Password must be at least 8 characters.")
            return render(request, "reset_password.html")

        if password != password_confirm:
            messages.error(request, "Passwords do not match.")
            return render(request, "reset_password.html")

        user.set_password(password)
        user.save(update_fields=["password"])

        request.session.pop("password_reset_user_id", None)
        request.session.pop("password_reset_verified", None)

        messages.success(request, "Your password has been reset. You can now log in.")
        return redirect("login_view")

    return render(request, "reset_password.html")


def resend_password_reset_otp(request):
    user_id = request.session.get("password_reset_user_id")

    if not user_id:
        return redirect("forgot_password")

    user = get_object_or_404(User, pk=user_id)

    if request.method != "POST":
        return redirect("verify_password_reset_otp")

    PhoneOTP.objects.filter(
        user=user, purpose=PhoneOTP.PURPOSE_PASSWORD_RESET, is_used=False
    ).update(is_used=True)

    code = f"{random.randint(0, 999999):06d}"
    PhoneOTP.objects.create(
        user=user, phone_number=user.phone_number, code=code,
        purpose=PhoneOTP.PURPOSE_PASSWORD_RESET,
        expires_at=timezone.now() + timezone.timedelta(minutes=10),
    )

    print()
    print("=" * 50)
    print("PHERAL PASSWORD RESET OTP")
    print(f"Phone: {user.phone_number}")
    print(f"OTP:   {code}")
    print("=" * 50)
    print()

    messages.success(request, "A new verification code has been sent.")
    return redirect("verify_password_reset_otp")


# ============================================================
# HOME
# ============================================================


# ============================================================
# PROFILE
# ============================================================

@login_required
def profile(request, username=None):
    profile_user = get_object_or_404(User, username__iexact=username) if username else request.user

    posts = (
        Post.objects.filter(author=profile_user, is_deleted=False)
        .select_related("author").order_by("-created_at")
    )

    jobs = HireJob.objects.filter(employer=profile_user).select_related("currency").order_by("-created_at")

    return render(request, "profile.html", {
        "profile_user": profile_user,
        "posts": posts,
        "jobs": jobs,
    })


@login_required
def profile_edit(request):
    if request.method == "POST":
        request.user.first_name = request.POST.get("first_name", "").strip()
        request.user.last_name = request.POST.get("last_name", "").strip()
        request.user.bio = request.POST.get("bio", "").strip()

        if request.FILES.get("avatar"):
            request.user.avatar = request.FILES["avatar"]

        request.user.save()
        messages.success(request, "Profile updated.")
        return redirect("profile")

    return render(request, "profile_edit.html")


# ============================================================
# CHAT (direct conversations only — groups use group_chat)
# ============================================================


@login_required
def chat(request, conversation_id=None, username=None):

    set_user_online(request.user)
    conversation = None
    other_user = None

    if username:
        other_user = get_object_or_404(User, username__iexact=username)

        if other_user.pk == request.user.pk:
            return redirect("home")

        conversation = get_or_create_direct_conversation(request.user, other_user)

    elif conversation_id:
        conversation = get_object_or_404(
            Conversation.objects.filter(
                pk=conversation_id, is_active=True, participants__user=request.user,
            ).distinct()
        )

        if conversation.conversation_type == Conversation.ConversationType.GROUP:
            return redirect("group_chat", conversation_id=conversation.pk)

        other_user = (
            User.objects.filter(conversation_participations__conversation=conversation)
            .exclude(pk=request.user.pk).first()
        )

        if other_user is None:
            messages.error(request, "This conversation is no longer available.")
            return redirect("chat_list")

    else:
        return redirect("chat_list")

    participant = get_object_or_404(ConversationParticipant, conversation=conversation, user=request.user)

    if request.method == "POST":
        content = request.POST.get("content", "").strip()
        attachment = request.FILES.get("attachment")
        voice_note = request.FILES.get("voice_note")
        reply_to_id = request.POST.get("reply_to")

        reply_to = None
        if reply_to_id:
            reply_to = (
                Message.objects.filter(pk=reply_to_id, conversation=conversation, is_deleted=False)
                .select_related("sender").first()
            )

        message_type = Message.MessageType.TEXT
        uploaded_file = None

        if voice_note:
            uploaded_file = voice_note
            message_type = Message.MessageType.VOICE
        elif attachment:
            uploaded_file = attachment
            content_type = (getattr(attachment, "content_type", "") or "").lower()

            if content_type.startswith("image/"):
                message_type = Message.MessageType.IMAGE
            elif content_type.startswith("video/"):
                message_type = Message.MessageType.VIDEO
            else:
                message_type = Message.MessageType.FILE

        if content or uploaded_file:
            Message.objects.create(
                conversation=conversation, sender=request.user, message_type=message_type,
                content=content, attachment=uploaded_file, reply_to=reply_to,
            )

            conversation.updated_at = timezone.now()
            conversation.save(update_fields=["updated_at"])

            participant.last_read_at = timezone.now()
            participant.save(update_fields=["last_read_at"])

        return redirect("chat", conversation_id=conversation.pk)

    messages_qs = (
        Message.objects.filter(conversation=conversation, is_deleted=False)
        .select_related("sender", "reply_to", "reply_to__sender", "receipt", "transaction")
        .prefetch_related("read_receipts", "read_receipts__user")
        .order_by("created_at")
    )

    unread_messages = (
        Message.objects.filter(conversation=conversation, is_deleted=False)
        .exclude(sender=request.user)
        .exclude(read_receipts__user=request.user)
    )
    unread_message_ids = list(unread_messages.values_list("id", flat=True))

    if unread_message_ids:
        MessageRead.objects.bulk_create(
            [MessageRead(message_id=mid, user=request.user) for mid in unread_message_ids],
            ignore_conflicts=True,
        )

    participant.last_read_at = timezone.now()
    participant.save(update_fields=["last_read_at"])

    participants = (
        ConversationParticipant.objects.filter(conversation=conversation)
        .select_related("user").order_by("joined_at")
    )

    read_message_ids = set(
        MessageRead.objects.filter(message__conversation=conversation, user=request.user)
        .values_list("message_id", flat=True)
    )

    read_by_others = set(
        MessageRead.objects.filter(message__conversation=conversation)
        .exclude(user=request.user).values_list("message_id", flat=True)
    )

    other_user_presence = get_user_presence(other_user)

    return render(request, "chat.html", {
        "conversation": conversation,
        "other_user": other_user,
        "other_user_presence": other_user_presence,
        "participants": participants,
        "participant": participant,
        "is_group": False,
        "chat_messages": messages_qs,
        "current_user": request.user,
        "read_message_ids": read_message_ids,
        "read_by_others": read_by_others,
        "unread_message_ids": unread_message_ids,
    })

@login_required
def delete_message(request, message_id):
    if request.method != "POST":
        return redirect("chat_list")

    message = get_object_or_404(Message, id=message_id, sender=request.user)
    conversation_id = message.conversation_id
    message.delete()

    return redirect("chat", conversation_id=conversation_id)


@login_required
def mark_chat_read(request, conversation_id):
    conversation = get_object_or_404(
        Conversation, id=conversation_id, participants__user=request.user, is_active=True,
    )
    participant = get_object_or_404(ConversationParticipant, conversation=conversation, user=request.user)
    participant.last_read_at = timezone.now()
    participant.save(update_fields=["last_read_at"])

    return redirect("chat", conversation_id=conversation.id)


@login_required
def chat_typing(request, conversation_id):
    conversation = get_object_or_404(
        Conversation, id=conversation_id, participants__user=request.user, is_active=True,
    )

    if request.method == "POST":
        typing = request.POST.get("typing") == "1"
        request.session[f"typing_{conversation.id}"] = typing
        return JsonResponse({"success": True, "typing": typing})

    other_participant = conversation.participants.exclude(user=request.user).select_related("user").first()
    other_typing = False

    if other_participant:
        other_typing = request.session.get(f"typing_{conversation.id}", False)

    return JsonResponse({"typing": other_typing})


@login_required
def presence_heartbeat(request):
    if request.method != "POST":
        return JsonResponse({"success": False}, status=405)

    presence = set_user_online(request.user)

    return JsonResponse({
        "success": True,
        "online": presence.is_online,
        "last_seen": presence.last_seen_at.isoformat() if presence.last_seen_at else None,
    })


# ============================================================
# CHAT LIST
# ============================================================

@login_required
def chat_list(request):

    conversations = (
        Conversation.objects.filter(participants__user=request.user, is_active=True)
        .prefetch_related(
            Prefetch(
                "participants",
                queryset=ConversationParticipant.objects.select_related("user"),
                to_attr="chat_participants",
            ),
            Prefetch(
                "messages",
                queryset=Message.objects.filter(is_deleted=False)
                .select_related("sender", "reply_to", "receipt", "transaction")
                .order_by("-created_at"),
                to_attr="ordered_messages",
            ),
        ).distinct()
    )

    chat_items = []

    for conversation in conversations:
        participants = getattr(conversation, "chat_participants", [])
        message_list = getattr(conversation, "ordered_messages", [])

        current_participant = next(
            (p for p in participants if p.user_id == request.user.id), None
        )
        if current_participant is None:
            continue

        other_user = None
        if conversation.conversation_type == Conversation.ConversationType.DIRECT:
            other_user = next((p.user for p in participants if p.user_id != request.user.id), None)

        last_message = message_list[0] if message_list else None

        if current_participant.last_read_at:
            unread_count = sum(
                1 for m in message_list
                if m.sender_id != request.user.id and m.created_at > current_participant.last_read_at
            )
        else:
            unread_count = sum(1 for m in message_list if m.sender_id != request.user.id)

        last_message_preview = ""
        if last_message:
            preview_map = {
                Message.MessageType.IMAGE: "Photo",
                Message.MessageType.VIDEO: "Video",
                Message.MessageType.VOICE: "Voice note",
                Message.MessageType.FILE: "File",
                Message.MessageType.PAYMENT: "Payment",
                Message.MessageType.HIRE: "Hire request",
            }
            if last_message.message_type == Message.MessageType.AGENT:
                last_message_preview = last_message.content or "Agent"
            else:
                last_message_preview = preview_map.get(last_message.message_type, last_message.content or "")

        last_message_prefix = "You: " if last_message and last_message.sender_id == request.user.id else ""

        if conversation.conversation_type == Conversation.ConversationType.GROUP:
            display_name = conversation.name or "Group"
        else:
            display_name = (
                other_user.get_full_name() if other_user and other_user.get_full_name()
                else (other_user.username if other_user else "Unknown user")
            )

        avatar_url = None
        if conversation.conversation_type == Conversation.ConversationType.GROUP:
            if conversation.avatar:
                avatar_url = conversation.avatar.url
        elif other_user and other_user.avatar:
            avatar_url = other_user.avatar.url

        presence = UserPresence.objects.filter(user=other_user).first() if other_user else None
        is_online = bool(presence and presence.is_online)
        last_seen_at = presence.last_seen_at if presence else None

        latest_activity = last_message.created_at if last_message else conversation.created_at

        chat_items.append({
            "conversation": conversation,
            "conversation_id": conversation.pk,
            "participants": participants,
            "current_participant": current_participant,
            "other_user": other_user,
            "display_name": display_name,
            "avatar_url": avatar_url,
            "last_message": last_message,
            "last_message_preview": last_message_preview,
            "last_message_prefix": last_message_prefix,
            "unread_count": unread_count,
            "is_muted": current_participant.is_muted,
            "is_archived": current_participant.is_archived,
            "is_group": conversation.conversation_type == Conversation.ConversationType.GROUP,
            "is_online": is_online,
            "last_seen_at": last_seen_at,
            "latest_activity": latest_activity,
        })

    chat_items.sort(key=lambda item: item["latest_activity"], reverse=True)

    return render(request, "chat_list.html", {
        "conversations": conversations,
        "chat_items": chat_items,
        "current_user": request.user,
    })


@login_required
def start_chat(request, username):
    other_user = get_object_or_404(User, username__iexact=username)

    if other_user == request.user:
        return redirect("home")

    conversation = get_or_create_direct_conversation(request.user, other_user)
    return redirect("chat", conversation_id=conversation.pk)


# ============================================================
# GROUP CHAT
# ============================================================

@login_required
def group_list(request):
    groups = (
        Conversation.objects.filter(
            conversation_type=Conversation.ConversationType.GROUP,
            participants__user=request.user, is_active=True,
        ).prefetch_related("participants__user").order_by("-updated_at")
    )
    return render(request, "group_list.html", {"groups": groups})


@login_required
def group_create(request):
    if request.method == "POST":
        name = request.POST.get("name", "").strip()
        description = request.POST.get("description", "").strip()

        if not name:
            messages.error(request, "Group name is required.")
            return render(request, "group_create.html")

        conversation = Conversation.objects.create(
            conversation_type=Conversation.ConversationType.GROUP,
            name=name, description=description, created_by=request.user,
        )

        ConversationParticipant.objects.create(conversation=conversation, user=request.user, is_admin=True)

        currency = get_default_currency()
        if currency:
            GroupLedger.objects.create(conversation=conversation, currency=currency)

        return redirect("group_chat", conversation_id=conversation.pk)

    return render(request, "group_create.html")


@login_required
def group_chat(request, conversation_id):
    conversation = get_object_or_404(
        Conversation, pk=conversation_id,
        conversation_type=Conversation.ConversationType.GROUP,
        participants__user=request.user,
    )

    chat_messages = (
        Message.objects.filter(conversation=conversation)
        .select_related("sender", "transaction", "receipt").order_by("created_at")
    )

    if request.method == "POST":
        content = request.POST.get("content", "").strip()

        if content:
            Message.objects.create(
                conversation=conversation, sender=request.user,
                message_type=Message.MessageType.TEXT, content=content,
            )
            conversation.updated_at = timezone.now()
            conversation.save(update_fields=["updated_at"])

        return redirect("group_chat", conversation_id=conversation.pk)

    return render(request, "group_chat.html", {
        "conversation": conversation,
        "chat_messages": chat_messages,
    })


@login_required
def group_add_member(request, conversation_id):
    conversation = get_object_or_404(
        Conversation, pk=conversation_id, conversation_type=Conversation.ConversationType.GROUP,
    )
    get_object_or_404(
        ConversationParticipant, conversation=conversation, user=request.user, is_admin=True,
    )

    if request.method == "POST":
        username = request.POST.get("username", "").strip()

        if not username:
            messages.error(request, "Username is required.")
            return redirect("group_add_member", conversation_id=conversation.pk)

        user_to_add = get_object_or_404(User, username__iexact=username)

        if user_to_add == request.user:
            messages.error(request, "You're already in this group.")
            return redirect("group_add_member", conversation_id=conversation.pk)

        _, created = ConversationParticipant.objects.get_or_create(
            conversation=conversation, user=user_to_add,
        )

        if created:
            messages.success(request, f"@{user_to_add.username} added to the group.")
        else:
            messages.info(request, f"@{user_to_add.username} is already in this group.")

        return redirect("group_chat", conversation_id=conversation.pk)

    query = request.GET.get("q", "").strip()
    existing_user_ids = ConversationParticipant.objects.filter(
        conversation=conversation,
    ).values_list("user_id", flat=True)

    contacts_qs = (
        Contact.objects.filter(owner=request.user)
        .exclude(contact_user_id__in=existing_user_ids)
        .select_related("contact_user").order_by("contact_user__username")
    )

    if query:
        contacts_qs = contacts_qs.filter(
            Q(contact_user__username__icontains=query)
            | Q(contact_user__first_name__icontains=query)
            | Q(contact_user__last_name__icontains=query)
            | Q(nickname__icontains=query)
        )

    return render(request, "group_add_member.html", {
        "conversation": conversation,
        "contacts": contacts_qs,
        "query": query,
    })


@login_required
def group_profile(request, conversation_id):
    conversation = get_object_or_404(
        Conversation, pk=conversation_id,
        conversation_type=Conversation.ConversationType.GROUP,
        participants__user=request.user,
    )
    participant = get_object_or_404(ConversationParticipant, conversation=conversation, user=request.user)

    participants = (
        ConversationParticipant.objects.filter(conversation=conversation)
        .select_related("user").order_by("-is_admin", "joined_at")
    )

    return render(request, "group_profile.html", {
        "conversation": conversation,
        "participant": participant,
        "participants": participants,
        "is_admin": participant.is_admin,
    })


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

    currencies = list(
        Currency.objects.filter(
            is_active=True
        ).order_by("code")
    )

    if not currencies:
        messages.error(
            request,
            "No active currency is configured.",
        )
        return redirect(
            "profile",
            username=recipient.username,
        )

    # Initial currency shown on page load.
    currency = get_default_currency()

    if not currency or not currency.is_active:
        currency = currencies[0]

    # Build every wallet balance for the UI.
    wallet_data = []

    for active_currency in currencies:
        wallet = get_or_create_wallet(
            request.user,
            active_currency,
        )

        wallet_data.append(
            {
                "code": active_currency.code,
                "name": active_currency.name,
                "symbol": active_currency.symbol,
                "balance": float(wallet.balance),
            }
        )

    if request.method == "POST":

        amount = parse_amount(
            request.POST.get("amount")
        )

        description = request.POST.get(
            "description",
            "",
        ).strip()

        currency_code = (
            request.POST.get("currency") or ""
        ).strip().upper()

        selected_currency = next(
            (
                item
                for item in currencies
                if item.code.upper() == currency_code
            ),
            None,
        )

        if not selected_currency:
            messages.error(
                request,
                "Please select a valid currency.",
            )

            return render(
                request,
                "pay.html",
                {
                    "recipient": recipient,
                    "currency": currency,
                    "currencies": currencies,
                    "wallet_data": wallet_data,
                },
            )

        # Keep the page showing the currency the user selected
        # if validation fails.
        currency = selected_currency

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
                    "currencies": currencies,
                    "wallet_data": wallet_data,
                },
            )

        # Get the exact wallet selected by the user.
        sender_wallet = get_or_create_wallet(
            request.user,
            selected_currency,
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
                    "currencies": currencies,
                    "wallet_data": wallet_data,
                },
            )

        recipient_wallet = get_or_create_wallet(
            recipient,
            selected_currency,
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
            transaction_type=(
                PheralTransaction.TransactionType.TRANSFER
            ),
            amount=amount,
            currency=selected_currency,
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
            currency=selected_currency,
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
            content=(
                f"{selected_currency.symbol}"
                f"{amount:,.2f} sent"
            ),
            transaction=pheral_transaction,
            receipt=receipt,
        )

        Notification.objects.create(
            user=recipient,
            notification_type=(
                Notification.NotificationType.PAYMENT
            ),
            title="Payment received",
            body=(
                f"@{request.user.username} sent "
                f"{selected_currency.symbol}"
                f"{amount:,.2f}"
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
            "currencies": currencies,
            "wallet_data": wallet_data,
        },
    )

@login_required
def wallet(request):
    wallets = (
        Wallet.objects.filter(user=request.user, is_active=True)
        .select_related("currency").order_by("currency__code")
    )

    transactions = (
        PheralTransaction.objects.filter(Q(sender=request.user) | Q(recipient=request.user))
        .select_related("sender", "recipient", "currency").order_by("-created_at")[:50]
    )

    token = get_or_create_wallet_token(request.user)

    return render(request, "wallet.html", {
        "wallets": wallets,
        "transactions": transactions,
        "wallet_token": token,
    })

@login_required
def top_up(request):
    """
    Top up a Pheral wallet using the currency selected by the user.

    The currency selector changes the UI instantly on the page.
    The selected currency and amount are submitted to Flutterwave
    only when the user starts the payment.
    """

    currencies = list(
        Currency.objects.filter(
            is_active=True
        ).order_by("code")
    )

    if not currencies:
        messages.error(
            request,
            "No wallet currencies are configured.",
        )
        return redirect("wallet")

    # Initial/default currency shown when the page loads.
    currency = get_default_currency()

    if not currency or not currency.is_active:
        currency = currencies[0]

    # Prepare wallet + currency information for Alpine.
    wallet_data = []

    for active_currency in currencies:
        wallet_obj = get_or_create_wallet(
            request.user,
            active_currency,
        )

        wallet_data.append(
            {
                "code": active_currency.code,
                "name": active_currency.name,
                "symbol": active_currency.symbol,
                "balance": float(wallet_obj.balance),
            }
        )

    if request.method == "POST":
        amount = parse_amount(
            request.POST.get("amount")
        )

        currency_code = (
            request.POST.get("currency") or ""
        ).strip().upper()

        selected_currency = next(
            (
                item
                for item in currencies
                if item.code.upper() == currency_code
            ),
            None,
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
                    "currencies": currencies,
                    "wallet_data": wallet_data,
                },
            )

        if not selected_currency:
            messages.error(
                request,
                "Please select a valid currency.",
            )

            return render(
                request,
                "top_up.html",
                {
                    "currency": currency,
                    "currencies": currencies,
                    "wallet_data": wallet_data,
                },
            )

        wallet_obj = get_or_create_wallet(
            request.user,
            selected_currency,
        )

        if not settings.FLW_SECRET_KEY:
            messages.error(
                request,
                "Payments are not configured yet. Please try again later.",
            )
            return redirect("wallet")

        pheral_transaction = PheralTransaction.objects.create(
            sender=request.user,
            sender_wallet=wallet_obj,
            transaction_type=(
                PheralTransaction.TransactionType.TOP_UP
            ),
            amount=amount,
            currency=selected_currency,
            status=PheralTransaction.Status.PENDING,
        )

        callback_url = request.build_absolute_uri(
            reverse("top_up_callback")
        )

        payload = {
            "tx_ref": pheral_transaction.reference,
            "amount": float(amount),
            "currency": selected_currency.code,
            "redirect_url": callback_url,
            "customer": {
                "email": (
                    request.user.email
                    or f"{request.user.username}@pheral.app"
                ),
                "name": (
                    request.user.get_full_name()
                    or request.user.username
                ),
            },
            "meta": {
                "user_id": request.user.id,
                "transaction_id": pheral_transaction.id,
            },
            "customizations": {
                "title": "Pheral Wallet Top Up",
                "description": (
                    f"Add funds to your "
                    f"{selected_currency.code} wallet"
                ),
            },
        }

        try:
            response = requests.post(
                "https://api.flutterwave.com/v3/payments",
                json=payload,
                headers={
                    "Authorization": (
                        f"Bearer {settings.FLW_SECRET_KEY}"
                    ),
                    "Content-Type": "application/json",
                },
                timeout=15,
            )

            data = response.json()

        except (
            requests.RequestException,
            ValueError,
        ):
            data = {"status": "error"}

        if data.get("status") != "success":
            pheral_transaction.status = (
                PheralTransaction.Status.FAILED
            )

            pheral_transaction.save(
                update_fields=["status"]
            )

            messages.error(
                request,
                "Could not start payment. Please try again.",
            )

            return redirect("wallet")

        payment_link = (
            data.get("data", {}).get("link")
        )

        if not payment_link:
            pheral_transaction.status = (
                PheralTransaction.Status.FAILED
            )

            pheral_transaction.save(
                update_fields=["status"]
            )

            messages.error(
                request,
                "Payment checkout could not be created.",
            )

            return redirect("wallet")

        pheral_transaction.external_reference = (
            pheral_transaction.reference
        )

        pheral_transaction.save(
            update_fields=["external_reference"]
        )

        return redirect(payment_link)

    return render(
        request,
        "top_up.html",
        {
            "currency": currency,
            "currencies": currencies,
            "wallet_data": wallet_data,
        },
    )

@login_required
def top_up_callback(request):
    """
    Flutterwave redirects the user's browser here after checkout.
    The webhook remains the authoritative source of truth.
    """

    tx_ref = request.GET.get("tx_ref")
    transaction_id = request.GET.get("transaction_id")
    status = request.GET.get("status")

    if not tx_ref:
        messages.error(request, "Missing payment reference.")
        return redirect("wallet")

    pheral_transaction = get_object_or_404(
        PheralTransaction,
        reference=tx_ref,
        sender=request.user,
        transaction_type=PheralTransaction.TransactionType.TOP_UP,
    )

    if pheral_transaction.status == PheralTransaction.Status.COMPLETED:
        messages.success(request, "Top-up successful.")
        return redirect("wallet")

    if status != "successful" or not transaction_id:
        messages.error(
            request,
            "We couldn't verify this payment yet. It may still be processing.",
        )
        return redirect("wallet")

    try:
        response = requests.get(
            f"https://api.flutterwave.com/v3/transactions/{transaction_id}/verify",
            headers={
                "Authorization": f"Bearer {settings.FLW_SECRET_KEY}",
                "Content-Type": "application/json",
            },
            timeout=15,
        )
        data = response.json()
    except (requests.RequestException, ValueError):
        data = {"status": "error"}

    if data.get("status") != "success":
        messages.error(
            request,
            "We couldn't verify this payment yet. It may still be processing.",
        )
        return redirect("wallet")

    flutterwave_data = data.get("data", {})

    paid_amount = flutterwave_data.get("amount", 0)
    paid_currency = flutterwave_data.get("currency")
    payment_status = flutterwave_data.get("status")
    paid_reference = flutterwave_data.get("tx_ref")

    expected_amount = float(pheral_transaction.amount)
    expected_currency = currency.code if (currency := pheral_transaction.currency) else None

    if (
        payment_status == "successful"
        and paid_reference == pheral_transaction.reference
        and paid_currency == expected_currency
        and float(paid_amount) == expected_amount
    ):
        _complete_top_up(pheral_transaction)
        messages.success(request, "Top-up successful.")
    else:
        _fail_top_up(pheral_transaction)
        messages.error(request, "Payment was not successful.")

    return redirect("wallet")

@csrf_exempt
def flutterwave_webhook(request):
    """
    Flutterwave server-to-server webhook.

    This is the authoritative source for completing top-ups
    and processing withdrawal status updates.
    """

    if request.method != "POST":
        return HttpResponse(status=405)

    secret_hash = settings.FLW_SECRET_HASH
    signature = request.headers.get("flutterwave-signature", "")

    if not secret_hash or not signature:
        return HttpResponse(status=401)

    computed_signature = hmac.new(
        secret_hash.encode("utf-8"),
        request.body,
        hashlib.sha256,
    ).digest()

    computed_signature = base64.b64encode(
        computed_signature
    ).decode("utf-8")

    if not hmac.compare_digest(signature, computed_signature):
        return HttpResponse(status=401)

    try:
        event = json.loads(request.body)
    except (ValueError, json.JSONDecodeError):
        return HttpResponse(status=400)

    event_type = event.get("event")
    event_data = event.get("data", {})

    # TOP-UP
    if event_type == "charge.completed":

        transaction_id = event_data.get("id")
        reference = event_data.get("tx_ref")

        pheral_transaction = PheralTransaction.objects.filter(
            reference=reference,
            transaction_type=PheralTransaction.TransactionType.TOP_UP,
        ).first()

        if not pheral_transaction:
            return HttpResponse(status=200)

        if pheral_transaction.status == PheralTransaction.Status.COMPLETED:
            return HttpResponse(status=200)

        if not transaction_id:
            return HttpResponse(status=200)

        try:
            response = requests.get(
                f"https://api.flutterwave.com/v3/transactions/{transaction_id}/verify",
                headers={
                    "Authorization": f"Bearer {settings.FLW_SECRET_KEY}",
                    "Content-Type": "application/json",
                },
                timeout=15,
            )

            verification = response.json()

        except (requests.RequestException, ValueError):
            return HttpResponse(status=200)

        if verification.get("status") != "success":
            return HttpResponse(status=200)

        verified_data = verification.get("data", {})

        expected_amount = float(pheral_transaction.amount)
        expected_currency = pheral_transaction.currency.code

        verified_status = verified_data.get("status")
        verified_amount = verified_data.get("amount")
        verified_currency = verified_data.get("currency")
        verified_reference = verified_data.get("tx_ref")

        if (
            verified_status == "successful"
            and verified_reference == pheral_transaction.reference
            and verified_currency == expected_currency
            and float(verified_amount) == expected_amount
        ):
            _complete_top_up(pheral_transaction)

    # WITHDRAWAL SUCCESS
    elif event_type == "transfer.completed":

        reference = event_data.get("reference")

        pheral_transaction = PheralTransaction.objects.filter(
            reference=reference,
            transaction_type=PheralTransaction.TransactionType.WITHDRAWAL,
        ).first()

        if (
            pheral_transaction
            and pheral_transaction.status
            == PheralTransaction.Status.PENDING
        ):
            pheral_transaction.status = (
                PheralTransaction.Status.COMPLETED
            )
            pheral_transaction.completed_at = timezone.now()

            pheral_transaction.save(
                update_fields=["status", "completed_at"]
            )

    # WITHDRAWAL FAILED
    elif event_type == "transfer.failed":

        reference = event_data.get("reference")

        pheral_transaction = PheralTransaction.objects.filter(
            reference=reference,
            transaction_type=PheralTransaction.TransactionType.WITHDRAWAL,
        ).first()

        if (
            pheral_transaction
            and pheral_transaction.status
            == PheralTransaction.Status.PENDING
        ):

            with transaction.atomic():

                locked_txn = (
                    PheralTransaction.objects
                    .select_for_update()
                    .get(pk=pheral_transaction.pk)
                )

                if locked_txn.status == PheralTransaction.Status.PENDING:

                    locked_wallet = (
                        Wallet.objects
                        .select_for_update()
                        .get(
                            pk=locked_txn.sender_wallet_id
                        )
                    )

                    balance_before = locked_wallet.balance

                    locked_wallet.balance += locked_txn.amount

                    locked_wallet.save(
                        update_fields=[
                            "balance",
                            "updated_at",
                        ]
                    )

                    locked_txn.status = (
                        PheralTransaction.Status.FAILED
                    )

                    locked_txn.completed_at = timezone.now()

                    locked_txn.save(
                        update_fields=[
                            "status",
                            "completed_at",
                        ]
                    )

                    LedgerEntry.objects.create(
                        transaction=locked_txn,
                        wallet=locked_wallet,
                        entry_type=LedgerEntry.EntryType.CREDIT,
                        amount=locked_txn.amount,
                        balance_before=balance_before,
                        balance_after=locked_wallet.balance,
                        description="Withdrawal failed — refunded",
                    )

    return HttpResponse(status=200)
# ============================================================
# BANK ACCOUNTS / WITHDRAWAL
# ============================================================

@login_required
def bank_accounts(request):
    accounts = BankAccount.objects.filter(user=request.user, is_active=True).order_by("-created_at")
    return render(request, "bank_accounts.html", {"accounts": accounts})


@login_required
def resolve_bank_account(request):
    """
    AJAX endpoint: given an account number + bank code, ask
    Paystack who owns it, so the user sees the real account
    name before confirming — never let them type it freely.
    """

    account_number = request.GET.get("account_number", "").strip()
    bank_code = request.GET.get("bank_code", "").strip()

    if not account_number or not bank_code:
        return JsonResponse({"success": False, "error": "Missing details."}, status=400)

    try:
        response = requests.get(
            f"{PAYSTACK_BASE_URL}/bank/resolve",
            params={"account_number": account_number, "bank_code": bank_code},
            headers={"Authorization": f"Bearer {settings.PAYSTACK_SECRET_KEY}"},
            timeout=15,
        )
        data = response.json()
    except (requests.RequestException, ValueError):
        data = {"status": False}

    if not data.get("status"):
        return JsonResponse({"success": False, "error": "Could not verify that account."}, status=400)

    return JsonResponse({"success": True, "account_name": data["data"]["account_name"]})


@login_required
def add_bank_account(request):
    if request.method != "POST":
        return redirect("bank_accounts")

    account_number = request.POST.get("account_number", "").strip()
    bank_code = request.POST.get("bank_code", "").strip()
    bank_name = request.POST.get("bank_name", "").strip()
    account_name = request.POST.get("account_name", "").strip()

    if not (account_number and bank_code and bank_name and account_name):
        messages.error(request, "All bank details are required.")
        return redirect("bank_accounts")

    try:
        response = requests.post(
            f"{PAYSTACK_BASE_URL}/transferrecipient",
            json={
                "type": "nuban", "name": account_name, "account_number": account_number,
                "bank_code": bank_code, "currency": "NGN",
            },
            headers={
                "Authorization": f"Bearer {settings.PAYSTACK_SECRET_KEY}",
                "Content-Type": "application/json",
            },
            timeout=15,
        )
        data = response.json()
    except (requests.RequestException, ValueError):
        data = {"status": False}

    if not data.get("status"):
        messages.error(request, "Could not add this bank account. Please try again.")
        return redirect("bank_accounts")

    BankAccount.objects.get_or_create(
        user=request.user, account_number=account_number, bank_code=bank_code,
        defaults={
            "account_name": account_name, "bank_name": bank_name,
            "paystack_recipient_code": data["data"]["recipient_code"],
        },
    )

    messages.success(request, "Bank account added.")
    return redirect("bank_accounts")


@login_required
def withdraw(request):
    currency = get_default_currency()
    wallet_obj = get_or_create_wallet(request.user, currency) if currency else None
    accounts = BankAccount.objects.filter(user=request.user, is_active=True)

    if request.method == "POST":
        amount = parse_amount(request.POST.get("amount"))
        bank_account = accounts.filter(pk=request.POST.get("bank_account")).first()

        if amount is None:
            messages.error(request, "Enter a valid withdrawal amount.")
            return render(request, "withdraw.html", {"accounts": accounts, "wallet": wallet_obj, "currency": currency})

        if not bank_account:
            messages.error(request, "Select a valid bank account.")
            return render(request, "withdraw.html", {"accounts": accounts, "wallet": wallet_obj, "currency": currency})

        if not settings.PAYSTACK_SECRET_KEY:
            messages.error(request, "Withdrawals are not configured yet.")
            return redirect("wallet")

        with transaction.atomic():
            locked_wallet = Wallet.objects.select_for_update().get(pk=wallet_obj.pk)

            if locked_wallet.balance < amount:
                messages.error(request, "Insufficient wallet balance.")
                return render(request, "withdraw.html", {"accounts": accounts, "wallet": wallet_obj, "currency": currency})

            # Debit immediately, before calling Paystack. If the
            # transfer later fails, the webhook credits it back —
            # this prevents the same balance being withdrawn twice
            # while a transfer is in flight.
            balance_before = locked_wallet.balance
            locked_wallet.balance -= amount
            locked_wallet.save(update_fields=["balance", "updated_at"])

            pheral_transaction = PheralTransaction.objects.create(
                sender=request.user, sender_wallet=locked_wallet,
                transaction_type=PheralTransaction.TransactionType.WITHDRAWAL,
                amount=amount, currency=currency, status=PheralTransaction.Status.PENDING,
                description=f"Withdrawal to {bank_account.bank_name}",
            )

            LedgerEntry.objects.create(
                transaction=pheral_transaction, wallet=locked_wallet,
                entry_type=LedgerEntry.EntryType.DEBIT, amount=amount,
                balance_before=balance_before, balance_after=locked_wallet.balance,
                description="Withdrawal (pending)",
            )

        try:
            response = requests.post(
                f"{PAYSTACK_BASE_URL}/transfer",
                json={
                    "source": "balance", "amount": int(amount * 100),
                    "recipient": bank_account.paystack_recipient_code,
                    "reference": pheral_transaction.reference,
                    "reason": "Pheral wallet withdrawal",
                },
                headers={
                    "Authorization": f"Bearer {settings.PAYSTACK_SECRET_KEY}",
                    "Content-Type": "application/json",
                },
                timeout=15,
            )
            data = response.json()
        except (requests.RequestException, ValueError):
            data = {"status": False}

        if not data.get("status"):
            _fail_top_up(pheral_transaction)

            with transaction.atomic():
                locked_wallet = Wallet.objects.select_for_update().get(pk=wallet_obj.pk)
                locked_wallet.balance += amount
                locked_wallet.save(update_fields=["balance", "updated_at"])

            messages.error(request, "Withdrawal could not be started. Your balance has been refunded.")
            return redirect("wallet")

        messages.success(request, "Withdrawal initiated — it may take a few minutes.")
        return redirect("wallet")

    return render(request, "withdraw.html", {"accounts": accounts, "wallet": wallet_obj, "currency": currency})


# ============================================================
# GLOBAL PAY / FX
# ============================================================

@login_required
def global_pay(request):
    currencies = Currency.objects.filter(is_active=True).order_by("code")

    if request.method == "POST":
        username = request.POST.get("username", "").strip()
        recipient = User.objects.filter(username__iexact=username).first()

        if not recipient:
            messages.error(request, "Pheral user not found.")
            return render(request, "global_pay.html", {"currencies": currencies})

        return redirect("pay_user", username=recipient.username)

    return render(request, "global_pay.html", {"currencies": currencies})


@login_required
def currency_converter(request):
    currencies = Currency.objects.filter(is_active=True).order_by("code")
    result = None

    if request.method == "POST":
        source = Currency.objects.filter(pk=request.POST.get("source_currency"), is_active=True).first()
        target = Currency.objects.filter(pk=request.POST.get("target_currency"), is_active=True).first()
        amount = parse_amount(request.POST.get("amount"))

        if source and target and amount:
            if source.pk == target.pk:
                converted = amount
            else:
                rate = ExchangeRate.objects.filter(
                    source_currency=source, target_currency=target, is_active=True,
                ).first()
                converted = amount * rate.rate if rate else None

            if converted is not None:
                result = {"amount": amount, "source": source, "target": target, "converted": converted}

    return render(request, "currency_converter.html", {"currencies": currencies, "result": result})


# ============================================================
# RECEIPTS
# ============================================================

@login_required
def receipt_detail(request, reference):
    receipt = get_object_or_404(
        Receipt.objects.select_related("transaction", "payer", "recipient", "currency"),
        reference=reference,
    )

    if receipt.payer != request.user and receipt.recipient != request.user:
        raise Http404

    return render(request, "receipt.html", {"receipt": receipt})


# ============================================================
# STATUS
# ============================================================

@login_required
def status_list(request):
    now = timezone.now()

    active_statuses = (
        Status.objects.filter(is_active=True, expires_at__gt=now)
        .select_related("user").order_by("-created_at")
    )

    status_groups = {}
    for status in active_statuses:
        status_groups.setdefault(status.user_id, []).append(status)

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

    my_status = next((g for g in status_groups_list if g["is_owner"]), None)
    other_statuses = [g for g in status_groups_list if not g["is_owner"]]

    return render(request, "status_list.html", {
        "my_status": my_status,
        "other_statuses": other_statuses,
    })


@login_required
def status_detail(request, status_id):
    now = timezone.now()

    status = get_object_or_404(
        Status.objects.select_related("user"), id=status_id, is_active=True, expires_at__gt=now,
    )

    is_owner = status.user_id == request.user.id

    if not is_owner:
        StatusView.objects.get_or_create(status=status, viewer=request.user)

    user_statuses = list(
        Status.objects.filter(user=status.user, is_active=True, expires_at__gt=now).order_by("created_at")
    )

    current_index = next((i for i, s in enumerate(user_statuses) if s.id == status.id), 0)
    previous_status = user_statuses[current_index - 1] if current_index > 0 else None
    next_status = user_statuses[current_index + 1] if current_index < len(user_statuses) - 1 else None

    other_statuses = (
        Status.objects.filter(is_active=True, expires_at__gt=now)
        .exclude(user=status.user).select_related("user").order_by("user_id", "created_at")
    )

    other_users = []
    seen_users = set()
    for item in other_statuses:
        if item.user_id not in seen_users:
            seen_users.add(item.user_id)
            other_users.append(item)

    next_user_status = next((u for u in other_users if u.user_id != status.user_id), None)

    view_count = StatusView.objects.filter(status=status).count()
    viewers = (
        StatusView.objects.filter(status=status).select_related("viewer").order_by("-viewed_at")
        if is_owner else []
    )

    return render(request, "status_detail.html", {
        "status": status,
        "user_statuses": user_statuses,
        "current_index": current_index,
        "previous_status": previous_status,
        "next_status": next_status,
        "next_user_status": next_user_status,
        "is_owner": is_owner,
        "view_count": view_count,
        "viewers": viewers,
    })


@login_required
@require_POST
def delete_status(request, status_id):
    status = get_object_or_404(Status, id=status_id, user=request.user)
    status.delete()
    messages.success(request, "Status deleted.")
    return redirect("status_list")


@login_required
def create_status(request):
    if request.method == "POST":
        text = request.POST.get("text", "").strip()
        media = request.FILES.get("media")

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
            user=request.user, status_type=status_type, text=text, media=media,
            expires_at=timezone.now() + timezone.timedelta(hours=24),
        )

        return redirect("status_list")

    return render(request, "create_status.html")


# ============================================================
# FEED
# ============================================================

@login_required
def feed(request):
    posts = (
        Post.objects.filter(is_deleted=False)
        .select_related("author")
        .prefetch_related("comments__user")
        .annotate(
            is_liked=Exists(
                PostLike.objects.filter(post=OuterRef("pk"), user=request.user)
            ),
            likes_count=Count("likes", distinct=True),
            comments_count=Count("comments", distinct=True),
        )
        .order_by("-created_at")
    )
    return render(request, "feed.html", {"posts": posts})

@login_required
def create_post(request):
    if request.method == "POST":
        content = request.POST.get("content", "").strip()
        media = request.FILES.get("media")

        if not content and not media:
            messages.error(request, "Post cannot be empty.")
            return redirect("feed")

        Post.objects.create(author=request.user, content=content, media=media)
        return redirect("feed")

    return render(request, "create_post.html")


@login_required
def like_post(request, post_id):
    post = get_object_or_404(Post, pk=post_id, is_deleted=False)

    like, created = PostLike.objects.get_or_create(post=post, user=request.user)

    if not created:
        like.delete()

    if request.headers.get("X-Requested-With") == "XMLHttpRequest":
        return JsonResponse({"liked": created, "likes": post.likes.count()})

    return redirect(request.META.get("HTTP_REFERER", "/"))


@login_required
def comment_post(request, post_id):
    post = get_object_or_404(Post, pk=post_id, is_deleted=False)

    if request.method == "POST":
        content = request.POST.get("content", "").strip()
        if content:
            PostComment.objects.create(post=post, user=request.user, content=content)

    return redirect(request.META.get("HTTP_REFERER", "/"))


# ============================================================
# HIRE
# ============================================================

@login_required
def hire(request):
    jobs = (
        HireJob.objects.filter(status=HireJob.Status.OPEN)
        .select_related("employer", "currency").order_by("-created_at")
    )
    return render(request, "hire.html", {"jobs": jobs})


@login_required
def create_hire_job(request):
    currencies = Currency.objects.filter(is_active=True).order_by("code")

    if request.method == "POST":
        title = request.POST.get("title", "").strip()
        description = request.POST.get("description", "").strip()
        amount = parse_amount(request.POST.get("budget"))
        currency = Currency.objects.filter(pk=request.POST.get("currency"), is_active=True).first()

        if not title:
            messages.error(request, "Job title is required.")
            return render(request, "create_hire_job.html", {"currencies": currencies})

        if not amount:
            messages.error(request, "Enter a valid budget.")
            return render(request, "create_hire_job.html", {"currencies": currencies})

        if not currency:
            messages.error(request, "Select a valid currency.")
            return render(request, "create_hire_job.html", {"currencies": currencies})

        job = HireJob.objects.create(
            employer=request.user, title=title, description=description,
            budget=amount, currency=currency,
        )

        return redirect("hire_job_detail", job_id=job.pk)

    return render(request, "create_hire_job.html", {"currencies": currencies})


@login_required
def hire_job_detail(request, job_id):
    job = get_object_or_404(HireJob.objects.select_related("employer", "currency"), pk=job_id)

    # Note: named hire_requests_qs, not `requests` — the requests
    # HTTP library is imported at module level for the Paystack
    # integration, and shadowing that name here (even though it's
    # harmless within this function's local scope) is asking for
    # a confusing bug the day someone needs to call the library
    # from inside this view.
    hire_requests_qs = job.requests.select_related("requester", "worker").order_by("-created_at")

    return render(request, "hire_job_detail.html", {"job": job, "hire_requests": hire_requests_qs})


@login_required
def send_hire_request(request, job_id, username):
    job = get_object_or_404(HireJob, pk=job_id, status=HireJob.Status.OPEN)
    worker = get_object_or_404(User, username__iexact=username)

    if worker == request.user:
        messages.error(request, "You cannot hire yourself.")
        return redirect("hire_job_detail", job_id=job.pk)

    if request.method == "POST":
        message_text = request.POST.get("message", "").strip()
        proposed_amount = parse_amount(request.POST.get("proposed_amount"))

        HireRequest.objects.create(
            job=job, requester=request.user, worker=worker,
            message=message_text, proposed_amount=proposed_amount,
        )

        conversation = get_or_create_direct_conversation(request.user, worker)

        Message.objects.create(
            conversation=conversation, sender=request.user,
            message_type=Message.MessageType.HIRE, content=f"Hire request: {job.title}",
        )

        Notification.objects.create(
            user=worker, notification_type=Notification.NotificationType.HIRE,
            title="New hire request", body=f"@{request.user.username} wants to hire you.",
        )

        return redirect("chat", conversation_id=conversation.pk)

    return render(request, "send_hire_request.html", {"job": job, "worker": worker})


# ============================================================
# NOTIFICATIONS
# ============================================================

@login_required
def notifications(request):
    notification_list = Notification.objects.filter(user=request.user).order_by("-created_at")
    return render(request, "notifications.html", {"notifications": notification_list})


@login_required
def mark_notification_read(request, notification_id):
    notification = get_object_or_404(Notification, pk=notification_id, user=request.user)
    notification.is_read = True
    notification.save(update_fields=["is_read"])
    return redirect(request.META.get("HTTP_REFERER", "/notifications/"))


@login_required
def mark_all_notifications_read(request):
    Notification.objects.filter(user=request.user, is_read=False).update(is_read=True)
    return redirect("notifications")


# ============================================================
# SEARCH / CONTACT DISCOVERY
# ============================================================

@login_required
def search(request):
    query = request.GET.get("q", "").strip()
    users = User.objects.none()

    if query:
        users = (
            User.objects.filter(
                Q(username__icontains=query) | Q(first_name__icontains=query)
                | Q(last_name__icontains=query) | Q(phone_number__icontains=query)
            ).exclude(pk=request.user.pk).order_by("username")[:50]
        )

    return render(request, "search.html", {"query": query, "users": users})


# ============================================================
# CONTACTS
# ============================================================

@login_required
def contacts(request):
    query = request.GET.get("q", "").strip()

    contacts_qs = (
        Contact.objects.filter(owner=request.user)
        .select_related("contact_user").order_by("contact_user__username")
    )

    if query:
        contacts_qs = contacts_qs.filter(
            Q(contact_user__username__icontains=query)
            | Q(contact_user__first_name__icontains=query)
            | Q(contact_user__last_name__icontains=query)
            | Q(nickname__icontains=query)
            | Q(phone_number__icontains=query)
        )

    return render(request, "contacts.html", {"contacts": contacts_qs, "query": query})


@login_required
@require_POST
def sync_contacts(request):
    """
    Match device contacts against registered Pheral users.
    Expected body: {"contacts": [{"name": "Zoe", "phone": "08012345678"}]}
    """

    try:
        payload = json.loads(request.body or "{}")
    except json.JSONDecodeError:
        return JsonResponse({"success": False, "error": "Invalid contact data."}, status=400)

    incoming_contacts = payload.get("contacts", [])

    if not isinstance(incoming_contacts, list):
        return JsonResponse({"success": False, "error": "Contacts must be a list."}, status=400)

    matched = []
    not_on_pheral = []
    seen_numbers = set()

    for item in incoming_contacts:
        if not isinstance(item, dict):
            continue

        name = str(item.get("name", "")).strip()
        phone = normalize_phone_number(str(item.get("phone", "")).strip())

        if not phone or phone in seen_numbers:
            continue

        seen_numbers.add(phone)

        user = User.objects.filter(phone_number=phone, is_active=True).exclude(pk=request.user.pk).first()

        if user:
            contact, created = Contact.objects.get_or_create(
                owner=request.user, contact_user=user,
                defaults={"phone_number": phone, "nickname": name[:100]},
            )

            if not contact.nickname and name:
                contact.nickname = name[:100]
                contact.save(update_fields=["nickname"])

            matched.append({
                "id": user.id, "username": user.username,
                "name": user.get_full_name() or user.username,
                "nickname": contact.nickname or name,
            })
        else:
            not_on_pheral.append({"name": name, "phone": phone})

    return JsonResponse({
        "success": True,
        "matched": matched,
        "not_on_pheral": not_on_pheral,
        "matched_count": len(matched),
        "not_on_pheral_count": len(not_on_pheral),
    })


@login_required
@require_POST
def add_contact(request, username):
    contact_user = get_object_or_404(User, username=username, is_active=True)

    if contact_user == request.user:
        messages.error(request, "You cannot add yourself to your contacts.")
        return redirect("contacts")

    Contact.objects.get_or_create(
        owner=request.user, contact_user=contact_user,
        defaults={"phone_number": getattr(contact_user, "phone_number", "") or ""},
    )

    messages.success(request, f"@{contact_user.username} added to contacts.")
    return redirect("contacts")


# ============================================================
# AGENT MODE
# ============================================================

@login_required
def agent(request):
    commands = AgentCommand.objects.filter(user=request.user).order_by("-created_at")[:50]

    if request.method == "POST":
        command_text = request.POST.get("command", "").strip()

        if not command_text:
            messages.error(request, "Enter an agent command.")
            return redirect("agent")

        command = AgentCommand.objects.create(
            user=request.user, command=command_text, status=AgentCommand.Status.PENDING,
        )
        return redirect("agent_command", command_id=command.pk)

    return render(request, "agent.html", {"commands": commands})


@login_required
def agent_command(request, command_id):
    command = get_object_or_404(AgentCommand, pk=command_id, user=request.user)

    if command.status != AgentCommand.Status.PENDING:
        return redirect("agent")

    command.status = AgentCommand.Status.PROCESSING
    command.save(update_fields=["status"])

    raw = command.command.strip()

    # ----------------------------------------------------
    # @agent msg @username message...
    # ----------------------------------------------------

    if raw.lower().startswith("@agent msg "):
        parts = raw.split(" ", 3)

        if len(parts) >= 4:
            target = parts[2].lstrip("@")
            text = parts[3].strip()
            recipient = User.objects.filter(username__iexact=target).first()

            if recipient:
                conversation = get_or_create_direct_conversation(request.user, recipient)

                Message.objects.create(
                    conversation=conversation, sender=request.user,
                    message_type=Message.MessageType.AGENT, content=text,
                )

                AgentActivity.objects.create(
                    user=request.user, command=command, conversation=conversation,
                    action="send_message",
                    details={"recipient": recipient.username, "message": text},
                )

                command.status = AgentCommand.Status.COMPLETED
                command.result = {"action": "send_message", "recipient": recipient.username, "message": text}
                command.completed_at = timezone.now()
                command.save(update_fields=["status", "result", "completed_at"])

                return redirect("agent")

    # ----------------------------------------------------
    # @agent pay @username amount
    # ----------------------------------------------------

    if raw.lower().startswith("@agent pay "):
        parts = raw.split()

        if len(parts) >= 4:
            target = parts[2].lstrip("@")
            amount = parse_amount(parts[3])
            recipient = User.objects.filter(username__iexact=target).first()

            if recipient and amount and recipient != request.user:
                currency = get_default_currency()

                if currency:
                    sender_wallet = get_or_create_wallet(request.user, currency)
                    recipient_wallet = get_or_create_wallet(recipient, currency)

                    with transaction.atomic():

                        # Same locking discipline as pay_user — this
                        # command is a second entry point into the
                        # same balance-mutation logic and needs the
                        # same protection against a double-spend race.
                        locked_sender_wallet = Wallet.objects.select_for_update().get(pk=sender_wallet.pk)
                        locked_recipient_wallet = Wallet.objects.select_for_update().get(pk=recipient_wallet.pk)

                        if locked_sender_wallet.balance >= amount:

                            sender_before = locked_sender_wallet.balance
                            recipient_before = locked_recipient_wallet.balance

                            locked_sender_wallet.balance -= amount
                            locked_sender_wallet.save(update_fields=["balance", "updated_at"])

                            locked_recipient_wallet.balance += amount
                            locked_recipient_wallet.save(update_fields=["balance", "updated_at"])

                            pheral_transaction = PheralTransaction.objects.create(
                                sender=request.user, recipient=recipient,
                                sender_wallet=locked_sender_wallet, recipient_wallet=locked_recipient_wallet,
                                transaction_type=PheralTransaction.TransactionType.TRANSFER,
                                amount=amount, currency=currency,
                                status=PheralTransaction.Status.COMPLETED, completed_at=timezone.now(),
                            )

                            LedgerEntry.objects.create(
                                transaction=pheral_transaction, wallet=locked_sender_wallet,
                                entry_type=LedgerEntry.EntryType.DEBIT, amount=amount,
                                balance_before=sender_before, balance_after=locked_sender_wallet.balance,
                            )

                            LedgerEntry.objects.create(
                                transaction=pheral_transaction, wallet=locked_recipient_wallet,
                                entry_type=LedgerEntry.EntryType.CREDIT, amount=amount,
                                balance_before=recipient_before, balance_after=locked_recipient_wallet.balance,
                            )

                            receipt = Receipt.objects.create(
                                transaction=pheral_transaction, payer=request.user, recipient=recipient,
                                amount=amount, currency=currency,
                            )

                            conversation = get_or_create_direct_conversation(request.user, recipient)

                            Message.objects.create(
                                conversation=conversation, sender=request.user,
                                message_type=Message.MessageType.PAYMENT,
                                content=f"₦{amount:,.2f} sent",
                                transaction=pheral_transaction, receipt=receipt,
                            )

                            AgentActivity.objects.create(
                                user=request.user, command=command, conversation=conversation,
                                transaction=pheral_transaction, action="payment",
                                details={
                                    "recipient": recipient.username,
                                    "amount": str(amount),
                                    "currency": currency.code,
                                },
                            )

                            command.status = AgentCommand.Status.COMPLETED
                            command.result = {
                                "action": "payment", "recipient": recipient.username,
                                "amount": str(amount), "currency": currency.code,
                                "reference": pheral_transaction.reference,
                            }
                            command.completed_at = timezone.now()
                            command.save(update_fields=["status", "result", "completed_at"])

                            return redirect("agent")

    command.status = AgentCommand.Status.FAILED
    command.result = {"error": "Command could not be understood or completed."}
    command.completed_at = timezone.now()
    command.save(update_fields=["status", "result", "completed_at"])

    return redirect("agent")


# ============================================================
# PUSH DEVICES / PWA
# ============================================================

@login_required
def register_push_device(request):
    if request.method != "POST":
        return JsonResponse({"error": "POST required."}, status=405)

    token = request.POST.get("token", "").strip()
    platform = request.POST.get("platform", PushDevice.Platform.WEB)

    if not token:
        return JsonResponse({"error": "Push token is required."}, status=400)

    device, created = PushDevice.objects.update_or_create(
        token=token, defaults={"user": request.user, "platform": platform, "is_active": True},
    )

    return JsonResponse({"success": True, "created": created})


def pwa_manifest(request):
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
            {"src": "/static/images/pheral-logo.png", "sizes": "192x192", "type": "image/png"},
            {"src": "/static/images/pheral-logo.png", "sizes": "512x512", "type": "image/png"},
        ],
    }

    response = JsonResponse(manifest)
    response["Cache-Control"] = "no-cache"
    return response


def service_worker(request):
    javascript = """
const CACHE_NAME = "pheral-v1";
const APP_SHELL = ["/", "/home/"];

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
            Promise.all(keys.filter(key => key !== CACHE_NAME).map(key => caches.delete(key)))
        ).then(() => self.clients.claim())
    );
});

self.addEventListener("fetch", event => {
    if (event.request.method !== "GET") return;

    event.respondWith(
        fetch(event.request)
            .then(response => {
                const responseClone = response.clone();
                caches.open(CACHE_NAME).then(cache => cache.put(event.request, responseClone));
                return response;
            })
            .catch(() => caches.match(event.request))
    );
});
"""
    response = HttpResponse(javascript, content_type="application/javascript")
    response["Cache-Control"] = "no-cache"
    return response


# ============================================================
# API-LIKE JSON ENDPOINTS
# ============================================================

@login_required
def user_lookup(request):
    query = request.GET.get("q", "").strip()
    users = []

    if query:
        matches = (
            User.objects.filter(
                Q(username__icontains=query) | Q(phone_number__icontains=query)
                | Q(first_name__icontains=query) | Q(last_name__icontains=query)
            ).exclude(pk=request.user.pk).order_by("username")[:20]
        )

        users = [
            {
                "username": u.username,
                "display_name": u.display_name,
                "phone_number": u.phone_number,
                "avatar": u.avatar.url if u.avatar else "",
            }
            for u in matches
        ]

    return JsonResponse({"results": users})


@login_required
def mark_message_read(request, message_id):
    message = get_object_or_404(
        Message, pk=message_id, conversation__participants__user=request.user,
    )
    MessageRead.objects.get_or_create(message=message, user=request.user)
    return JsonResponse({"success": True})



# ============================================================
# HELPERS — keep this ONE definition only
# ============================================================

def get_or_create_direct_conversation(user1, user2):
    """
    Return the existing direct conversation between two users,
    or create it if one does not exist. Race-safe: a unique
    constraint on (conversation_type, direct_pair_key) makes the
    database itself reject a second DIRECT conversation for the
    same pair, and we catch that here instead of trusting a
    check-then-create sequence.
    """
    if user1.pk == user2.pk:
        raise ValueError("A user cannot create a conversation with themselves.")

    user_ids = sorted([user1.pk, user2.pk])
    pair_key = f"{user_ids[0]}-{user_ids[1]}"

    conversation = (
        Conversation.objects
        .filter(conversation_type=Conversation.ConversationType.DIRECT, direct_pair_key=pair_key)
        .first()
    )
    if conversation:
        return conversation

    try:
        with transaction.atomic():
            conversation = Conversation.objects.create(
                conversation_type=Conversation.ConversationType.DIRECT,
                created_by=user1,
                direct_pair_key=pair_key,
            )
            ConversationParticipant.objects.create(conversation=conversation, user=user1)
            ConversationParticipant.objects.create(conversation=conversation, user=user2)
            return conversation
    except IntegrityError:
        # Someone else created it in the split second between our
        # lookup and our create — fetch the one that won.
        return Conversation.objects.get(
            conversation_type=Conversation.ConversationType.DIRECT,
            direct_pair_key=pair_key,
        )


# ============================================================
# CHAT — keep this ONE definition only
# ============================================================

@login_required
def chat(request, conversation_id=None, username=None):
    conversation = None
    other_user = None

    if username:
        other_user = get_object_or_404(User, username__iexact=username)
        if other_user.pk == request.user.pk:
            return redirect("home")
        conversation = get_or_create_direct_conversation(request.user, other_user)

    elif conversation_id:
        conversation = get_object_or_404(
            Conversation.objects.filter(participants__user=request.user, is_active=True).distinct(),
            pk=conversation_id,
        )
        if conversation.conversation_type == Conversation.ConversationType.GROUP:
            return redirect("group_chat", conversation_id=conversation.pk)

        other_user = (
            User.objects
            .filter(conversation_participations__conversation=conversation)
            .exclude(pk=request.user.pk)
            .first()
        )
        if other_user is None:
            return redirect("chat_list")

    else:
        return redirect("chat_list")

    participant = get_object_or_404(ConversationParticipant, conversation=conversation, user=request.user)

    if request.method == "POST":
        content = request.POST.get("content", "").strip()
        attachment = request.FILES.get("attachment")
        voice_note = request.FILES.get("voice_note")
        reply_to_id = request.POST.get("reply_to")

        reply_to = None
        if reply_to_id:
            reply_to = Message.objects.filter(
                pk=reply_to_id, conversation=conversation, is_deleted=False
            ).first()

        message_type = Message.MessageType.TEXT
        uploaded_file = None

        if voice_note:
            uploaded_file = voice_note
            message_type = Message.MessageType.VOICE
        elif attachment:
            uploaded_file = attachment
            content_type = (getattr(attachment, "content_type", "") or "").lower()
            if content_type.startswith("image/"):
                message_type = Message.MessageType.IMAGE
            elif content_type.startswith("video/"):
                message_type = Message.MessageType.VIDEO
            else:
                message_type = Message.MessageType.FILE

        if content or uploaded_file:
            Message.objects.create(
                conversation=conversation, sender=request.user, message_type=message_type,
                content=content, attachment=uploaded_file, reply_to=reply_to,
            )
            conversation.updated_at = timezone.now()
            conversation.save(update_fields=["updated_at"])
            participant.last_read_at = timezone.now()
            participant.save(update_fields=["last_read_at"])

        return redirect("chat", conversation_id=conversation.pk)

    # NOTE: named chat_messages, never `messages` — that name is the
    # django.contrib.messages module used for messages.error()/success()
    # elsewhere in this file, and shadowing it here caused real crashes.
    chat_messages = (
        Message.objects.filter(conversation=conversation, is_deleted=False)
        .select_related("sender", "reply_to", "reply_to__sender", "receipt", "transaction")
        .prefetch_related("read_receipts")
        .order_by("created_at")
    )

    participant.last_read_at = timezone.now()
    participant.save(update_fields=["last_read_at"])

    participants = (
        ConversationParticipant.objects.filter(conversation=conversation)
        .select_related("user").order_by("joined_at")
    )

    return render(request, "chat.html", {
        "conversation": conversation,
        "other_user": other_user,
        "participants": participants,
        "participant": participant,
        "is_group": False,
        "chat_messages": chat_messages,
        "current_user": request.user,
    })


@login_required
def start_chat(request, username):
    other_user = get_object_or_404(User, username__iexact=username)
    if other_user.pk == request.user.pk:
        return redirect("home")
    conversation = get_or_create_direct_conversation(request.user, other_user)
    return redirect("chat", conversation_id=conversation.pk)


def verify_otp(request):
    user_id = request.session.get("otp_user_id")

    if not user_id:
        return redirect("register")

    user = get_object_or_404(User, pk=user_id)

    if request.method == "POST":
        code = request.POST.get("code", "").strip()

        # --- TEMPORARY MASTER BYPASS ---
        if settings.MASTER_OTP_CODE and code == settings.MASTER_OTP_CODE:
            print(f"[MASTER OTP USED] user={user.username} phone={user.phone_number} at {timezone.now()}")

            user.is_phone_verified = True
            user.save(update_fields=["is_phone_verified"])

            get_or_create_wallet_token(user)
            currency = get_default_currency()
            if currency:
                get_or_create_wallet(user, currency)

            login(request, user)
            request.session.pop("otp_user_id", None)
            return redirect("chat")
        # --- END MASTER BYPASS ---

        otp = (
            PhoneOTP.objects.filter(user=user, code=code, is_used=False)
            .order_by("-created_at").first()
        )

        if not otp:
            messages.error(request, "Invalid OTP.")
            return render(request, "verify_otp.html")

        if otp.is_expired:
            messages.error(request, "This OTP has expired.")
            return render(request, "verify_otp.html")

        otp.is_used = True
        otp.save(update_fields=["is_used"])

        user.is_phone_verified = True
        user.save(update_fields=["is_phone_verified"])

        get_or_create_wallet_token(user)

        currency = get_default_currency()
        if currency:
            get_or_create_wallet(user, currency)

        login(request, user)
        request.session.pop("otp_user_id", None)

        return redirect("chat")

    return render(request, "verify_otp.html")

# ============================================================
# AIRTIME / DATA PURCHASE — views.py additions
#
# Add these imports near the top of views.py if not already there:
#   import uuid
#   import requests
#   from django.conf import settings
#   from django.http import JsonResponse
#
# Add to settings.py:
#   FLUTTERWAVE_SECRET_KEY = os.environ.get("FLUTTERWAVE_SECRET_KEY", "")
#
# This reuses get_default_currency, get_or_create_wallet, and
# parse_amount from the wallet backend — make sure that's already
# in place before adding this.
# ============================================================

FLUTTERWAVE_BASE_URL = "https://api.flutterwave.com/v3"


def _flutterwave_headers():
    return {
        "Authorization": f"Bearer {settings.FLUTTERWAVE_SECRET_KEY}",
        "Content-Type": "application/json",
    }


def _local_phone_format(phone_number):
    """
    Flutterwave's NG billers generally expect the local 11-digit
    format (08012345678), not +234. Convert from whatever format
    normalize_phone_number() produced.
    """
    digits = "".join(ch for ch in str(phone_number) if ch.isdigit())
    if digits.startswith("234") and len(digits) == 13:
        return "0" + digits[3:]
    return digits


def _debit_wallet_for_bill(user, amount, currency, transaction_type, description, metadata):
    """
    Debit the wallet and create a PENDING transaction *before* calling
    Flutterwave — mirrors the withdraw() pattern: debit first, then
    call the provider, then refund automatically if the provider call
    fails. This is the only ordering that can't accidentally let
    someone submit the same purchase twice before the balance updates.
    """
    wallet_obj = get_or_create_wallet(user, currency)

    with transaction.atomic():
        locked_wallet = Wallet.objects.select_for_update().get(pk=wallet_obj.pk)

        if locked_wallet.balance < amount:
            return None, "Insufficient wallet balance."

        balance_before = locked_wallet.balance
        locked_wallet.balance -= amount
        locked_wallet.save(update_fields=["balance", "updated_at"])

        pheral_transaction = PheralTransaction.objects.create(
            sender=user, sender_wallet=locked_wallet,
            transaction_type=transaction_type,
            amount=amount, currency=currency,
            status=PheralTransaction.Status.PENDING,
            description=description,
            metadata=metadata,
        )

        LedgerEntry.objects.create(
            transaction=pheral_transaction, wallet=locked_wallet,
            entry_type=LedgerEntry.EntryType.DEBIT, amount=amount,
            balance_before=balance_before, balance_after=locked_wallet.balance,
            description=description,
        )

    return pheral_transaction, None


def _refund_failed_bill(pheral_transaction):
    with transaction.atomic():
        locked_txn = PheralTransaction.objects.select_for_update().get(pk=pheral_transaction.pk)
        if locked_txn.status != PheralTransaction.Status.PENDING:
            return locked_txn

        locked_wallet = Wallet.objects.select_for_update().get(pk=locked_txn.sender_wallet_id)
        balance_before = locked_wallet.balance
        locked_wallet.balance += locked_txn.amount
        locked_wallet.save(update_fields=["balance", "updated_at"])

        locked_txn.status = PheralTransaction.Status.FAILED
        locked_txn.completed_at = timezone.now()
        locked_txn.save(update_fields=["status", "completed_at"])

        LedgerEntry.objects.create(
            transaction=locked_txn, wallet=locked_wallet,
            entry_type=LedgerEntry.EntryType.CREDIT, amount=locked_txn.amount,
            balance_before=balance_before, balance_after=locked_wallet.balance,
            description="Purchase failed — refunded",
        )
        return locked_txn


def _complete_bill(pheral_transaction, external_reference):
    pheral_transaction.status = PheralTransaction.Status.COMPLETED
    pheral_transaction.external_reference = external_reference
    pheral_transaction.completed_at = timezone.now()
    pheral_transaction.save(update_fields=["status", "external_reference", "completed_at"])
    return pheral_transaction


def _call_flutterwave_bill(*, biller_name, customer_phone, amount, item_code=None):
    """
    ⚠️ VERIFY THIS AGAINST FLUTTERWAVE'S CURRENT DOCS BEFORE GOING LIVE.
    Flutterwave's Bills API (POST /v3/bills) payload shape has shifted
    across their v3 doc revisions — specifically whether a data bundle
    is selected via `type` set to the plan's item_code, or via a
    separate `biller_code` field. This implementation uses `type` as
    the item_code when one is provided (data bundles), and falls back
    to a plain "AIRTIME" type otherwise. Confirm against
    https://developer.flutterwave.com/docs/bill-payments before
    relying on this in production, and adjust just this function if
    the field name has changed — nothing else in this file needs to
    know about that detail.
    """
    reference = f"PHR-BILL-{uuid.uuid4().hex[:10].upper()}"

    payload = {
        "country": "NG",
        "customer": customer_phone,
        "amount": float(amount),
        "recurrence": "ONCE",
        "type": item_code if item_code else "AIRTIME",
        "biller_name": biller_name,
        "reference": reference,
    }

    try:
        response = requests.post(
            f"{FLUTTERWAVE_BASE_URL}/bills",
            json=payload,
            headers=_flutterwave_headers(),
            timeout=20,
        )
        data = response.json()
    except (requests.RequestException, ValueError):
        return False, reference, None

    success = data.get("status") == "success"
    flw_reference = (data.get("data") or {}).get("reference", reference)
    return success, reference, flw_reference


def fetch_data_plans(network):
    """
    Live-fetches available data bundle plans + current prices from
    Flutterwave rather than storing them locally, since VTU pricing
    changes often enough that a cached/seeded plan list would go
    stale. Returns a list of {name, amount, item_code} dicts, or an
    empty list if the lookup fails (template shows an error state).

    ⚠️ Also verify this endpoint shape against current Flutterwave
    docs — /v3/bill-categories's response format for listing
    individual data bundle items under a biller has varied by API
    version.
    """
    if not getattr(settings, "FLUTTERWAVE_SECRET_KEY", ""):
        return []

    try:
        response = requests.get(
            f"{FLUTTERWAVE_BASE_URL}/bill-categories",
            params={"country": "NG", "biller_name": network.flutterwave_data_biller},
            headers=_flutterwave_headers(),
            timeout=20,
        )
        data = response.json()
    except (requests.RequestException, ValueError):
        return []

    if data.get("status") != "success":
        return []

    plans = []
    for item in data.get("data", []):
        plans.append({
            "name": item.get("name") or item.get("biller_name", "Data plan"),
            "amount": item.get("amount"),
            "item_code": item.get("item_code") or item.get("biller_code"),
        })
    return [p for p in plans if p["amount"] and p["item_code"]]


# ============================================================
# VIEWS
# ============================================================

@login_required
def airtime_purchase(request):
    currency = get_default_currency()
    wallet_obj = get_or_create_wallet(request.user, currency) if currency else None
    networks = NetworkProvider.objects.filter(is_active=True)

    if request.method == "POST":
        network = networks.filter(pk=request.POST.get("network")).first()
        phone_number = request.POST.get("phone_number", "").strip()
        amount = parse_amount(request.POST.get("amount"))

        if not network:
            messages.error(request, "Select a network.")
            return render(request, "airtime.html", {"networks": networks, "wallet": wallet_obj, "currency": currency})

        if not phone_number:
            messages.error(request, "Enter a phone number.")
            return render(request, "airtime.html", {"networks": networks, "wallet": wallet_obj, "currency": currency})

        if amount is None:
            messages.error(request, "Enter a valid amount.")
            return render(request, "airtime.html", {"networks": networks, "wallet": wallet_obj, "currency": currency})

        pheral_transaction, error = _debit_wallet_for_bill(
            request.user, amount, currency,
            PheralTransaction.TransactionType.AIRTIME,
            description=f"{network.name} airtime — {phone_number}",
            metadata={"network": network.code, "phone_number": phone_number},
        )
        if error:
            messages.error(request, error)
            return render(request, "airtime.html", {"networks": networks, "wallet": wallet_obj, "currency": currency})

        success, our_reference, flw_reference = _call_flutterwave_bill(
            biller_name=network.flutterwave_airtime_biller,
            customer_phone=_local_phone_format(phone_number),
            amount=amount,
        )

        if success:
            _complete_bill(pheral_transaction, flw_reference or our_reference)
            messages.success(request, f"{currency.symbol}{amount:,.2f} airtime sent to {phone_number}.")
        else:
            _refund_failed_bill(pheral_transaction)
            messages.error(request, "Airtime purchase failed. Your wallet has been refunded.")

        return redirect("wallet")

    return render(request, "airtime.html", {"networks": networks, "wallet": wallet_obj, "currency": currency})


@login_required
def data_purchase(request):
    currency = get_default_currency()
    wallet_obj = get_or_create_wallet(request.user, currency) if currency else None
    networks = NetworkProvider.objects.filter(is_active=True)

    if request.method == "POST":
        network = networks.filter(pk=request.POST.get("network")).first()
        phone_number = request.POST.get("phone_number", "").strip()
        plan_amount = parse_amount(request.POST.get("plan_amount"))
        plan_name = request.POST.get("plan_name", "").strip()
        item_code = request.POST.get("item_code", "").strip()

        if not (network and phone_number and plan_amount and item_code):
            messages.error(request, "Select a network, phone number, and data plan.")
            return render(request, "data.html", {"networks": networks, "wallet": wallet_obj, "currency": currency})

        pheral_transaction, error = _debit_wallet_for_bill(
            request.user, plan_amount, currency,
            PheralTransaction.TransactionType.DATA,
            description=f"{network.name} {plan_name} — {phone_number}",
            metadata={"network": network.code, "phone_number": phone_number, "plan": plan_name},
        )
        if error:
            messages.error(request, error)
            return render(request, "data.html", {"networks": networks, "wallet": wallet_obj, "currency": currency})

        success, our_reference, flw_reference = _call_flutterwave_bill(
            biller_name=network.flutterwave_data_biller,
            customer_phone=_local_phone_format(phone_number),
            amount=plan_amount,
            item_code=item_code,
        )

        if success:
            _complete_bill(pheral_transaction, flw_reference or our_reference)
            messages.success(request, f"{plan_name} sent to {phone_number}.")
        else:
            _refund_failed_bill(pheral_transaction)
            messages.error(request, "Data purchase failed. Your wallet has been refunded.")

        return redirect("wallet")

    return render(request, "data.html", {"networks": networks, "wallet": wallet_obj, "currency": currency})


@login_required
def data_plans_api(request, network_id):
    """AJAX endpoint: returns live data plans for the selected network."""
    network = get_object_or_404(NetworkProvider, pk=network_id, is_active=True)
    plans = fetch_data_plans(network)
    return JsonResponse({"plans": plans})