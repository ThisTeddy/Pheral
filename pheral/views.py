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


import uuid
from .models import (
    AgentActivity,
    AgentCommand,
    BankAccount,
    Contact,
    Conversation,
    ConversationParticipant,
    Currency,
    ExchangeRate,
    generate_reference,
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
    VirtualCard,
    Wallet,
    WalletToken,
    NetworkProvider,
)

from django.db.models import Count, Exists, OuterRef, Q

PAYSTACK_BASE_URL = "https://api.paystack.co"


# ============================================================
# HELPERS
# ============================================================
# ============================================================
# Paste into views.py.
#
# 1. pip install phonenumbers
# 2. Delete ALL old copies of: normalize_phone_number, register,
#    sync_contacts.
# 3. Add the imports below to the top of views.py (skip any you
#    already have).
# 4. Paste everything under the imports into views.py.
# ============================================================

# ---------- IMPORTS (top of views.py) ----------

import re
import json
import secrets
from datetime import timedelta

import phonenumbers
from phonenumbers import NumberParseException, PhoneNumberFormat

from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.contrib.auth.password_validation import validate_password
from django.core.exceptions import ValidationError
from django.db import IntegrityError, transaction
from django.http import JsonResponse
from django.shortcuts import redirect, render
from django.utils import timezone
from django.views.decorators.http import require_POST

# (Contact, PhoneOTP, User are already imported from .models in views.py)


# ---------- PHONE HELPERS ----------
# Countries matching the currencies Pheral supports. Used to build
# the region selector on registration so normalize_phone_number
# knows how to read a locally-formatted number (e.g. "0801..." is
# only unambiguous once you know which country it's from).
SUPPORTED_REGIONS = [
    ("NG", "Nigeria"),
    ("GH", "Ghana"),
    ("KE", "Kenya"),
    ("ZA", "South Africa"),
    ("UG", "Uganda"),
    ("TZ", "Tanzania"),
    ("RW", "Rwanda"),
    ("EG", "Egypt"),
    ("US", "United States"),
    ("GB", "United Kingdom"),
    ("CA", "Canada"),
    ("AU", "Australia"),
]


def region_dial_code(region):
    try:
        return "+" + str(phonenumbers.country_code_for_region(region))
    except Exception:
        return ""

DEFAULT_REGION = "NG"


def normalize_phone_number(phone, region=DEFAULT_REGION):
    """
    Return the number in E.164 (+2348012345678), or "" if it isn't
    a valid phone number. Numbers without a country code
    (08012345678) are read as belonging to `region`.
    """
    if not phone:
        return ""

    raw = str(phone).strip()

    # 00234... is a common way of writing +234...
    if raw.startswith("00"):
        raw = "+" + raw[2:]

    try:
        parsed = phonenumbers.parse(raw, region)
    except NumberParseException:
        return ""

    if not phonenumbers.is_valid_number(parsed):
        return ""

    return phonenumbers.format_number(parsed, PhoneNumberFormat.E164)


def region_for_user(user):
    """Country of a user's stored number, used to read their local-format contacts."""
    try:
        parsed = phonenumbers.parse(user.phone_number, None)
        return phonenumbers.region_code_for_number(parsed) or DEFAULT_REGION
    except NumberParseException:
        return DEFAULT_REGION


# ---------- OTP HELPER ----------

OTP_TTL = timedelta(minutes=10)


def issue_phone_otp(user, purpose=PhoneOTP.PURPOSE_VERIFICATION):
    """Invalidate old codes, create a fresh one, and return it."""
    PhoneOTP.objects.filter(user=user, purpose=purpose, is_used=False).update(is_used=True)

    code = f"{secrets.randbelow(1_000_000):06d}"  # secrets, not random: OTPs need a CSPRNG

    PhoneOTP.objects.create(
        user=user,
        phone_number=user.phone_number,
        code=code,
        purpose=purpose,
        expires_at=timezone.now() + OTP_TTL,
    )

    # TODO: replace with a real SMS provider
    print(f"\n{'=' * 50}\nPHERAL OTP ({purpose})\nPhone: {user.phone_number}\nOTP:   {code}\n{'=' * 50}\n")

    return code


# ---------- REGISTER ----------

USERNAME_RE = re.compile(r"^[A-Za-z0-9_.]{3,30}$")

def register(request):
    if request.user.is_authenticated:
        return redirect("chat")

    if request.method == "POST":
        username = request.POST.get("username", "").strip()
        region = request.POST.get("region", "NG").strip().upper()
        phone_raw = request.POST.get("phone_number", "").strip()
        password = request.POST.get("password", "")
        password_confirm = request.POST.get("password_confirm", "")
        first_name = request.POST.get("first_name", "").strip()
        last_name = request.POST.get("last_name", "").strip()

        context = {"regions": SUPPORTED_REGIONS, "selected_region": region, "phone_raw": phone_raw}

        if not username:
            messages.error(request, "Username is required.")
            return render(request, "register.html", context)

        if region not in dict(SUPPORTED_REGIONS):
            region = "NG"

        phone_number = normalize_phone_number(phone_raw, region)

        if not phone_raw:
            messages.error(request, "Phone number is required.")
            return render(request, "register.html", context)

        if not phone_number:
            messages.error(request, "Enter a valid phone number for the selected country.")
            return render(request, "register.html", context)

        if not first_name or not last_name:
            messages.error(request, "First name and last name are required.")
            return render(request, "register.html", context)

        if not password:
            messages.error(request, "Password is required.")
            return render(request, "register.html", context)

        if password != password_confirm:
            messages.error(request, "Passwords do not match.")
            return render(request, "register.html", context)

        terms_accepted = request.POST.get("terms_accepted") == "on"

        if not terms_accepted:
            messages.error(request, "You must accept the Terms of Service to continue.")
            return render(request, "register.html", context)
        if User.objects.filter(username__iexact=username).exists():
            messages.error(request, "That username is already taken.")
            return render(request, "register.html", context)

        if User.objects.filter(phone_number=phone_number).exists():
            messages.error(request, "That phone number is already registered.")
            return render(request, "register.html", context)

        user = User.objects.create_user(
            username=username, phone_number=phone_number, password=password,
            first_name=first_name, last_name=last_name,
        )
        user.is_phone_verified = False
        user.save(update_fields=["is_phone_verified"])

        code = f"{random.randint(0, 999999):06d}"
        PhoneOTP.objects.create(
            user=user, phone_number=phone_number, code=code,
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

    return render(request, "register.html", {"regions": SUPPORTED_REGIONS, "selected_region": "NG"})
# ---------- SYNC CONTACTS ----------

MAX_SYNC_CONTACTS = 2000


@login_required
@require_POST
def sync_contacts(request):
    """
    Match device contacts against registered Pheral users.
    Expected body: {"contacts": [{"name": "Zoe", "phone": "08012345678"}]}
    """
    try:
        payload = json.loads(request.body or "{}")
    except ValueError:
        return JsonResponse({"success": False, "error": "Invalid contact data."}, status=400)

    if not isinstance(payload, dict):
        return JsonResponse({"success": False, "error": "Invalid contact data."}, status=400)

    incoming = payload.get("contacts", [])

    if not isinstance(incoming, list):
        return JsonResponse({"success": False, "error": "Contacts must be a list."}, status=400)

    if len(incoming) > MAX_SYNC_CONTACTS:
        return JsonResponse(
            {"success": False, "error": f"Too many contacts (max {MAX_SYNC_CONTACTS})."},
            status=400,
        )

    # Local-format numbers in the address book are read as the user's own country.
    region = region_for_user(request.user)

    device = {}  # normalized phone -> name (first name wins for duplicates)
    invalid_count = 0

    for item in incoming:
        if not isinstance(item, dict):
            continue

        name = str(item.get("name", "")).strip()[:100]
        phone = normalize_phone_number(str(item.get("phone", "")), region)

        if not phone:
            invalid_count += 1
            continue

        device.setdefault(phone, name)

    users_by_phone = {
        u.phone_number: u
        for u in User.objects
        .filter(phone_number__in=device.keys(), is_active=True)
        .exclude(pk=request.user.pk)
    }

    existing = {
        c.contact_user_id: c
        for c in Contact.objects.filter(
            owner=request.user,
            contact_user_id__in=[u.pk for u in users_by_phone.values()],
        )
    }

    to_create, to_update, matched = [], [], []

    for phone, user in users_by_phone.items():
        name = device[phone]
        contact = existing.get(user.pk)

        if contact is None:
            contact = Contact(
                owner=request.user, contact_user=user,
                phone_number=phone, nickname=name,
            )
            to_create.append(contact)
        elif name and not contact.nickname:
            contact.nickname = name
            to_update.append(contact)

        matched.append({
            "id": user.id,
            "username": user.username,
            "name": user.get_full_name() or user.username,
            "nickname": contact.nickname or name,
        })

    with transaction.atomic():
        Contact.objects.bulk_create(to_create, ignore_conflicts=True)
        if to_update:
            Contact.objects.bulk_update(to_update, ["nickname"])

    not_on_pheral = [
        {"name": name, "phone": phone}
        for phone, name in device.items()
        if phone not in users_by_phone
    ]

    return JsonResponse({
        "success": True,
        "matched": matched,
        "not_on_pheral": not_on_pheral,
        "matched_count": len(matched),
        "not_on_pheral_count": len(not_on_pheral),
        "invalid_count": invalid_count,
    })

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

def _verify_flutterwave_transaction(pheral_transaction, transaction_id):
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
        return False

    if data.get("status") != "success":
        return False

    verified = data.get("data", {})
    expected_amount = float(pheral_transaction.amount)
    expected_currency = pheral_transaction.currency.code

    if (
        verified.get("status") == "successful"
        and verified.get("tx_ref") == pheral_transaction.reference
        and verified.get("currency") == expected_currency
        and float(verified.get("amount", 0)) == expected_amount
    ):
        _complete_top_up(pheral_transaction)
        return True

    return False
    
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

# ============================================================
# AUTHENTICATION
# ============================================================

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
            "is_pinned": current_participant.is_pinned,
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

from decimal import Decimal

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

        if not pheral_transaction:
            return HttpResponse(status=200)

        with transaction.atomic():
            locked_txn = (
                PheralTransaction.objects
                .select_for_update()
                .get(pk=pheral_transaction.pk)
            )

            # Idempotent: only a pending withdrawal can become completed.
            if locked_txn.status == PheralTransaction.Status.PENDING:
                locked_txn.status = (
                    PheralTransaction.Status.COMPLETED
                )
                locked_txn.completed_at = timezone.now()

                locked_txn.save(
                    update_fields=[
                        "status",
                        "completed_at",
                    ]
                )

    # WITHDRAWAL FAILED
    elif event_type == "transfer.failed":
        reference = event_data.get("reference")

        pheral_transaction = PheralTransaction.objects.filter(
            reference=reference,
            transaction_type=PheralTransaction.TransactionType.WITHDRAWAL,
        ).first()

        if not pheral_transaction:
            return HttpResponse(status=200)

        with transaction.atomic():
            locked_txn = (
                PheralTransaction.objects
                .select_for_update()
                .get(pk=pheral_transaction.pk)
            )

            # Important:
            # If another request already failed/refunded this transaction,
            # do nothing. This prevents double refunds.
            if locked_txn.status != PheralTransaction.Status.PENDING:
                return HttpResponse(status=200)

            locked_wallet = (
                Wallet.objects
                .select_for_update()
                .get(pk=locked_txn.sender_wallet_id)
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
    """
    Withdraw funds from the user's Pheral wallet to a verified bank account
    using Flutterwave Transfers.

    The wallet is debited before the transfer is submitted so the same
    balance cannot be withdrawn twice while a transfer is pending.

    If Flutterwave rejects the transfer immediately, the wallet is refunded
    immediately and the transaction is marked FAILED.

    If Flutterwave accepts/queues the transfer, the transaction remains
    PENDING until the Flutterwave webhook reports the final outcome.
    """
    currency = get_default_currency()
    wallet_obj = (
        get_or_create_wallet(request.user, currency)
        if currency
        else None
    )

    accounts = BankAccount.objects.filter(
        user=request.user,
        is_active=True,
    )

    if request.method == "POST":
        amount = parse_amount(request.POST.get("amount"))
        bank_account = accounts.filter(
            pk=request.POST.get("bank_account")
        ).first()

        if amount is None:
            messages.error(
                request,
                "Enter a valid withdrawal amount.",
            )
            return render(
                request,
                "withdraw.html",
                {
                    "accounts": accounts,
                    "wallet": wallet_obj,
                    "currency": currency,
                },
            )

        if not currency:
            messages.error(
                request,
                "No withdrawal currency is configured.",
            )
            return redirect("wallet")

        if not bank_account:
            messages.error(
                request,
                "Select a valid bank account.",
            )
            return render(
                request,
                "withdraw.html",
                {
                    "accounts": accounts,
                    "wallet": wallet_obj,
                    "currency": currency,
                },
            )

        if not settings.FLW_SECRET_KEY:
            messages.error(
                request,
                "Withdrawals are not configured yet.",
            )
            return redirect("wallet")

        # Flutterwave bank transfers require the destination currency.
        # Your current default wallet currency should therefore be NGN
        # for Nigerian bank withdrawals.
        if currency.code.upper() != "NGN":
            messages.error(
                request,
                "Nigerian bank withdrawals are currently available in NGN only.",
            )
            return redirect("wallet")

        # ---------------------------------------------------------
        # 1. Lock wallet + debit funds + create pending transaction
        # ---------------------------------------------------------
        with transaction.atomic():
            locked_wallet = (
                Wallet.objects
                .select_for_update()
                .get(pk=wallet_obj.pk)
            )

            if locked_wallet.balance < amount:
                messages.error(
                    request,
                    "Insufficient wallet balance.",
                )
                return render(
                    request,
                    "withdraw.html",
                    {
                        "accounts": accounts,
                        "wallet": locked_wallet,
                        "currency": currency,
                    },
                )

            balance_before = locked_wallet.balance

            locked_wallet.balance -= amount

            locked_wallet.save(
                update_fields=[
                    "balance",
                    "updated_at",
                ]
            )

            pheral_transaction = PheralTransaction.objects.create(
                sender=request.user,
                sender_wallet=locked_wallet,
                transaction_type=(
                    PheralTransaction.TransactionType.WITHDRAWAL
                ),
                amount=amount,
                currency=currency,
                status=PheralTransaction.Status.PENDING,
                description=(
                    f"Withdrawal to "
                    f"{bank_account.bank_name}"
                ),
            )

            LedgerEntry.objects.create(
                transaction=pheral_transaction,
                wallet=locked_wallet,
                entry_type=LedgerEntry.EntryType.DEBIT,
                amount=amount,
                balance_before=balance_before,
                balance_after=locked_wallet.balance,
                description="Withdrawal (pending)",
            )

        # ---------------------------------------------------------
        # 2. Submit transfer to Flutterwave
        # ---------------------------------------------------------
        try:
            response = requests.post(
                "https://api.flutterwave.com/v3/transfers",
                json={
                    "account_bank": bank_account.bank_code,
                    "account_number": bank_account.account_number,
                    "amount": float(amount),
                    "currency": currency.code.upper(),
                    "narration": "Pheral wallet withdrawal",
                    "reference": pheral_transaction.reference,
                },
                headers={
                    "Authorization": (
                        f"Bearer {settings.FLW_SECRET_KEY}"
                    ),
                    "Content-Type": "application/json",
                },
                timeout=20,
            )

            data = response.json()

        except (requests.RequestException, ValueError):
            data = {
                "status": "error",
            }

        # ---------------------------------------------------------
        # 3. Flutterwave rejected the transfer immediately
        # ---------------------------------------------------------
        if data.get("status") != "success":
            with transaction.atomic():
                locked_txn = (
                    PheralTransaction.objects
                    .select_for_update()
                    .get(pk=pheral_transaction.pk)
                )

                # Only refund if the transaction is still pending.
                # This protects against a race with a webhook.
                if (
                    locked_txn.status
                    == PheralTransaction.Status.PENDING
                ):
                    locked_wallet = (
                        Wallet.objects
                        .select_for_update()
                        .get(
                            pk=locked_txn.sender_wallet_id
                        )
                    )

                    balance_before_refund = locked_wallet.balance

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
                        balance_before=balance_before_refund,
                        balance_after=locked_wallet.balance,
                        description=(
                            "Withdrawal failed — refunded"
                        ),
                    )

            messages.error(
                request,
                "Withdrawal could not be started. "
                "Your balance has been refunded.",
            )
            return redirect("wallet")

        # ---------------------------------------------------------
        # 4. Transfer accepted/queued by Flutterwave
        # ---------------------------------------------------------
        transfer_data = data.get("data") or {}

        messages.success(
            request,
            "Withdrawal initiated — it may take a few minutes.",
        )

        return redirect("wallet")

    return render(
        request,
        "withdraw.html",
        {
            "accounts": accounts,
            "wallet": wallet_obj,
            "currency": currency,
        },
    )

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
        "start_url": "/chat_list/",
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
const APP_SHELL = ["/", "/chat_list/"];

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

def _call_flutterwave_bill(
    *,
    biller_code,
    customer_phone,
    amount,
    item_code,
):
    """
    Send an airtime/data bill payment to Flutterwave.
    """

    reference = f"PHR-BILL-{uuid.uuid4().hex[:10].upper()}"

    payload = {
        "country": "NG",
        "customer_id": customer_phone,
        "amount": float(amount),
        "reference": reference,
    }

    try:
        response = requests.post(
            f"{FLUTTERWAVE_BASE_URL}/billers/"
            f"{biller_code}/items/{item_code}/payment",
            json=payload,
            headers=_flutterwave_headers(),
            timeout=30,
        )

        data = response.json()

    except (requests.RequestException, ValueError):
        return False, reference, None

    success = data.get("status") == "success"

    response_data = data.get("data") or {}

    flw_reference = (
        response_data.get("reference")
        or response_data.get("tx_ref")
        or reference
    )

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
        biller_code=network.flutterwave_airtime_biller_code,
        customer_phone=_local_phone_format(phone_number),
        amount=amount,
        item_code="AT102",
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


@login_required
def group_chat(request, conversation_id):
    conversation = get_object_or_404(
        Conversation, pk=conversation_id,
        conversation_type=Conversation.ConversationType.GROUP,
        participants__user=request.user,
    )

    participant = get_object_or_404(ConversationParticipant, conversation=conversation, user=request.user)

    ledger, _ = GroupLedger.objects.get_or_create(
        conversation=conversation,
        defaults={"currency": get_default_currency()},
    )

    chat_messages = (
        Message.objects.filter(conversation=conversation)
        .select_related("sender", "transaction", "receipt").order_by("created_at")
    )

    ledger_entries = (
        GroupLedgerEntry.objects.filter(ledger=ledger)
        .select_related("user").order_by("-created_at")[:15]
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
        "ledger": ledger,
        "ledger_entries": ledger_entries,
        "participant": participant,
        "participant_count": conversation.participants.count(),
    })


@login_required
@require_POST
def group_contribute(request, conversation_id):
    """
    Moves money from the member's personal wallet (in the group's
    ledger currency) into the group's shared balance. Race-safe via
    select_for_update on both the wallet and the ledger — same
    discipline as pay_user.
    """

    conversation = get_object_or_404(
        Conversation, pk=conversation_id,
        conversation_type=Conversation.ConversationType.GROUP,
        participants__user=request.user,
    )
    ledger = get_object_or_404(GroupLedger, conversation=conversation)

    amount = parse_amount(request.POST.get("amount"))
    if amount is None:
        messages.error(request, "Enter a valid contribution amount.")
        return redirect("group_chat", conversation_id=conversation.pk)

    wallet = get_or_create_wallet(request.user, ledger.currency)

    with transaction.atomic():
        locked_wallet = Wallet.objects.select_for_update().get(pk=wallet.pk)
        locked_ledger = GroupLedger.objects.select_for_update().get(pk=ledger.pk)

        if locked_wallet.balance < amount:
            messages.error(request, "Insufficient wallet balance.")
            return redirect("group_chat", conversation_id=conversation.pk)

        balance_before = locked_wallet.balance
        locked_wallet.balance -= amount
        locked_wallet.save(update_fields=["balance", "updated_at"])

        locked_ledger.balance += amount
        locked_ledger.save(update_fields=["balance", "updated_at"])

        pheral_transaction = PheralTransaction.objects.create(
            sender=request.user, sender_wallet=locked_wallet,
            transaction_type=PheralTransaction.TransactionType.GROUP_TRANSFER,
            amount=amount, currency=locked_ledger.currency,
            status=PheralTransaction.Status.COMPLETED, completed_at=timezone.now(),
            description=f"Contribution to {conversation.name or 'group'}",
        )

        LedgerEntry.objects.create(
            transaction=pheral_transaction, wallet=locked_wallet,
            entry_type=LedgerEntry.EntryType.DEBIT, amount=amount,
            balance_before=balance_before, balance_after=locked_wallet.balance,
            description="Group contribution",
        )

        GroupLedgerEntry.objects.create(
            ledger=locked_ledger, user=request.user,
            entry_type=GroupLedgerEntry.EntryType.CONTRIBUTION, amount=amount,
            description=f"@{request.user.username} contributed",
        )

        Message.objects.create(
            conversation=conversation, sender=request.user,
            message_type=Message.MessageType.PAYMENT,
            content=f"{locked_ledger.currency.symbol}{amount:,.2f} contributed to the group",
            transaction=pheral_transaction,
        )

        conversation.updated_at = timezone.now()
        conversation.save(update_fields=["updated_at"])

    return redirect("group_chat", conversation_id=conversation.pk)


@login_required
@require_POST
def group_withdraw(request, conversation_id):
    """
    Admin-only: moves money out of the group's shared balance into
    the admin's own wallet, minus the group's configured
    withdrawal_fee (the fee stays in the ledger's history as its own
    entry rather than vanishing silently).
    """

    conversation = get_object_or_404(
        Conversation, pk=conversation_id,
        conversation_type=Conversation.ConversationType.GROUP,
        participants__user=request.user,
    )
    get_object_or_404(ConversationParticipant, conversation=conversation, user=request.user, is_admin=True)
    ledger = get_object_or_404(GroupLedger, conversation=conversation)

    amount = parse_amount(request.POST.get("amount"))
    if amount is None:
        messages.error(request, "Enter a valid withdrawal amount.")
        return redirect("group_chat", conversation_id=conversation.pk)

    wallet = get_or_create_wallet(request.user, ledger.currency)

    with transaction.atomic():
        locked_ledger = GroupLedger.objects.select_for_update().get(pk=ledger.pk)
        locked_wallet = Wallet.objects.select_for_update().get(pk=wallet.pk)

        if locked_ledger.balance < amount:
            messages.error(request, "Insufficient group balance.")
            return redirect("group_chat", conversation_id=conversation.pk)

        fee = min(locked_ledger.withdrawal_fee, amount)
        net_amount = amount - fee

        locked_ledger.balance -= amount
        locked_ledger.save(update_fields=["balance", "updated_at"])

        wallet_before = locked_wallet.balance
        locked_wallet.balance += net_amount
        locked_wallet.save(update_fields=["balance", "updated_at"])

        pheral_transaction = PheralTransaction.objects.create(
            recipient=request.user, recipient_wallet=locked_wallet,
            transaction_type=PheralTransaction.TransactionType.GROUP_TRANSFER,
            amount=net_amount, fee=fee, currency=locked_ledger.currency,
            status=PheralTransaction.Status.COMPLETED, completed_at=timezone.now(),
            description=f"Withdrawal from {conversation.name or 'group'}",
        )

        LedgerEntry.objects.create(
            transaction=pheral_transaction, wallet=locked_wallet,
            entry_type=LedgerEntry.EntryType.CREDIT, amount=net_amount,
            balance_before=wallet_before, balance_after=locked_wallet.balance,
            description="Group withdrawal",
        )

        GroupLedgerEntry.objects.create(
            ledger=locked_ledger, user=request.user,
            entry_type=GroupLedgerEntry.EntryType.WITHDRAWAL, amount=amount,
            description=f"@{request.user.username} withdrew",
        )

        if fee > 0:
            GroupLedgerEntry.objects.create(
                ledger=locked_ledger, user=request.user,
                entry_type=GroupLedgerEntry.EntryType.FEE, amount=fee,
                description="Withdrawal fee",
            )

        Message.objects.create(
            conversation=conversation, sender=request.user,
            message_type=Message.MessageType.PAYMENT,
            content=f"{locked_ledger.currency.symbol}{net_amount:,.2f} withdrawn from the group",
            transaction=pheral_transaction,
        )

        conversation.updated_at = timezone.now()
        conversation.save(update_fields=["updated_at"])

    return redirect("group_chat", conversation_id=conversation.pk)

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

    ledger = GroupLedger.objects.filter(conversation=conversation).first()

    return render(request, "group_profile.html", {
        "conversation": conversation,
        "participant": participant,
        "participants": participants,
        "is_admin": participant.is_admin,
        "ledger": ledger,
    })


@login_required
@require_POST
def group_toggle_admin(request, conversation_id, username):
    """
    Admin-only: promote or demote another member. An admin can't
    demote themselves this way if they're the only admin left —
    that would leave the group with no one able to manage it.
    """

    conversation = get_object_or_404(
        Conversation, pk=conversation_id, conversation_type=Conversation.ConversationType.GROUP,
    )
    get_object_or_404(ConversationParticipant, conversation=conversation, user=request.user, is_admin=True)

    target = get_object_or_404(
        ConversationParticipant, conversation=conversation, user__username__iexact=username,
    )

    if target.is_admin:
        remaining_admins = ConversationParticipant.objects.filter(
            conversation=conversation, is_admin=True,
        ).exclude(pk=target.pk).count()

        if remaining_admins == 0:
            messages.error(request, "A group needs at least one admin.")
            return redirect("group_profile", conversation_id=conversation.pk)

    target.is_admin = not target.is_admin
    target.save(update_fields=["is_admin"])

    messages.success(
        request,
        f"@{target.user.username} is {'now an admin' if target.is_admin else 'no longer an admin'}.",
    )
    return redirect("group_profile", conversation_id=conversation.pk)


@login_required
@require_POST
def group_remove_member(request, conversation_id, username):
    conversation = get_object_or_404(
        Conversation, pk=conversation_id, conversation_type=Conversation.ConversationType.GROUP,
    )
    get_object_or_404(ConversationParticipant, conversation=conversation, user=request.user, is_admin=True)

    target = get_object_or_404(
        ConversationParticipant, conversation=conversation, user__username__iexact=username,
    )

    if target.user_id == request.user.id:
        messages.error(request, "Use \"Leave group\" to remove yourself.")
        return redirect("group_profile", conversation_id=conversation.pk)

    target_username = target.user.username
    target.delete()

    messages.success(request, f"@{target_username} was removed from the group.")
    return redirect("group_profile", conversation_id=conversation.pk)


@login_required
@require_POST
def group_leave(request, conversation_id):
    conversation = get_object_or_404(
        Conversation, pk=conversation_id, conversation_type=Conversation.ConversationType.GROUP,
    )
    participant = get_object_or_404(ConversationParticipant, conversation=conversation, user=request.user)

    if participant.is_admin:
        remaining_admins = ConversationParticipant.objects.filter(
            conversation=conversation, is_admin=True,
        ).exclude(pk=participant.pk).count()

        if remaining_admins == 0:
            other_member = (
                ConversationParticipant.objects.filter(conversation=conversation)
                .exclude(pk=participant.pk).order_by("joined_at").first()
            )
            if other_member:
                other_member.is_admin = True
                other_member.save(update_fields=["is_admin"])

    participant.delete()
    messages.success(request, "You left the group.")
    return redirect("chat_list")






# ============================================================
# AIRTIME / DATA
# ============================================================

# Networks shown to the user. "prefix_hint" is just UI copy, not
# used for validation — Flutterwave detects the network from the
# phone number itself on their end.
NETWORKS = [
    {"code": "mtn", "label": "MTN"},
    {"code": "airtel", "label": "Airtel"},
    {"code": "glo", "label": "Glo"},
    {"code": "9mobile", "label": "9mobile"},
]

# NOTE: these biller_code / item_code values are placeholders and
# MUST be replaced with real values from your Flutterwave dashboard
# (Bills > Data) before this goes live — call GET /v3/bill-categories
# with `?country=NG` to get the current, correct codes and prices
# for each network's data bundles. Shipping with wrong codes here
# will cause every data purchase to fail at the Flutterwave step,
# after the user's wallet has already been debited — see buy note
# in purchase_bill() about why the debit only happens after the
# provider call succeeds, specifically to avoid that failure mode.
DATA_PLANS = {
    "mtn": [
        {"code": "mtn-100mb-1day", "label": "100MB — 1 day", "amount": Decimal("100.00")},
        {"code": "mtn-1gb-1day", "label": "1GB — 1 day", "amount": Decimal("350.00")},
        {"code": "mtn-1.5gb-30day", "label": "1.5GB — 30 days", "amount": Decimal("1000.00")},
        {"code": "mtn-3.5gb-30day", "label": "3.5GB — 30 days", "amount": Decimal("1500.00")},
        {"code": "mtn-10gb-30day", "label": "10GB — 30 days", "amount": Decimal("3000.00")},
    ],
    "airtel": [
        {"code": "airtel-500mb-1day", "label": "500MB — 1 day", "amount": Decimal("300.00")},
        {"code": "airtel-1.5gb-7day", "label": "1.5GB — 7 days", "amount": Decimal("500.00")},
        {"code": "airtel-4gb-30day", "label": "4GB — 30 days", "amount": Decimal("1500.00")},
        {"code": "airtel-10gb-30day", "label": "10GB — 30 days", "amount": Decimal("3000.00")},
    ],
    "glo": [
        {"code": "glo-1.35gb-1day", "label": "1.35GB — 1 day", "amount": Decimal("300.00")},
        {"code": "glo-2.9gb-30day", "label": "2.9GB — 30 days", "amount": Decimal("1000.00")},
        {"code": "glo-7.7gb-30day", "label": "7.7GB — 30 days", "amount": Decimal("2500.00")},
    ],
    "9mobile": [
        {"code": "9mobile-500mb-30day", "label": "500MB — 30 days", "amount": Decimal("500.00")},
        {"code": "9mobile-1.5gb-30day", "label": "1.5GB — 30 days", "amount": Decimal("1000.00")},
        {"code": "9mobile-4.5gb-30day", "label": "4.5GB — 30 days", "amount": Decimal("2500.00")},
    ],
}


@login_required
def airtime_data(request):
    currency = get_default_currency()
    wallet_obj = get_or_create_wallet(request.user, currency) if currency else None

    return render(request, "airtime_data.html", {
        "wallet": wallet_obj,
        "currency": currency,
        "networks": NETWORKS,
        "data_plans_json": json.dumps(DATA_PLANS, default=str),
        "default_phone": request.user.phone_number,
    })


@login_required
@require_POST
def purchase_bill(request):
    """
    Handles both airtime and data purchases. AJAX-called from
    airtime_data.html so the whole flow stays inline on the page —
    same pattern as init_top_up/verify_top_up.

    Order of operations matters here: the wallet is only debited
    AFTER Flutterwave confirms the bill purchase succeeded, not
    before. Airtime/data purchases (unlike top-ups) settle
    synchronously in one API call — there's no separate webhook step
    to fall back on if we debited first and the provider then
    failed, so debit-after is the only safe order for this flow.
    """

    bill_type = request.POST.get("bill_type", "").strip()
    network_code = request.POST.get("network", "").strip()
    phone_number = request.POST.get("phone_number", "").strip()

    if bill_type not in ("airtime", "data"):
        return JsonResponse({"success": False, "error": "Invalid request."}, status=400)

    network = next((n for n in NETWORKS if n["code"] == network_code), None)
    if not network:
        return JsonResponse({"success": False, "error": "Select a valid network."}, status=400)

    if not phone_number or len(re.sub(r"\D", "", phone_number)) < 10:
        return JsonResponse({"success": False, "error": "Enter a valid phone number."}, status=400)

    currency = get_default_currency()
    if not currency:
        return JsonResponse({"success": False, "error": "No wallet currency is configured."}, status=400)

    if bill_type == "airtime":
        amount = parse_amount(request.POST.get("amount"))
        if amount is None:
            return JsonResponse({"success": False, "error": "Enter a valid amount."}, status=400)
        biller_type = f"{network_code.upper()}_AIRTIME"
        description = f"{network['label']} airtime — {phone_number}"

    else:
        plan_code = request.POST.get("plan_code", "").strip()
        plans = DATA_PLANS.get(network_code, [])
        plan = next((p for p in plans if p["code"] == plan_code), None)
        if not plan:
            return JsonResponse({"success": False, "error": "Select a valid data plan."}, status=400)
        amount = plan["amount"]
        biller_type = plan_code
        description = f"{network['label']} data — {plan['label']} — {phone_number}"

    if not settings.FLW_SECRET_KEY:
        return JsonResponse({"success": False, "error": "Bill payments are not configured yet."}, status=400)

    wallet_obj = get_or_create_wallet(request.user, currency)

    with transaction.atomic():
        locked_wallet = Wallet.objects.select_for_update().get(pk=wallet_obj.pk)

        if locked_wallet.balance < amount:
            return JsonResponse({"success": False, "error": "Insufficient wallet balance."})

        # Hold the row locked through the external API call so no
        # concurrent request can double-spend this balance while
        # we're waiting on Flutterwave's response.
        try:
            response = requests.post(
                "https://api.flutterwave.com/v3/bills",
                json={
                    "country": "NG",
                    "customer": phone_number,
                    "amount": float(amount),
                    "recurrence": "ONCE",
                    "type": biller_type,
                    "reference": generate_reference(prefix="BILL"),
                },
                headers={
                    "Authorization": f"Bearer {settings.FLW_SECRET_KEY}",
                    "Content-Type": "application/json",
                },
                timeout=20,
            )
            data = response.json()
        except (requests.RequestException, ValueError):
            return JsonResponse({"success": False, "error": "Could not reach the payment network. Please try again."}, status=502)

        if data.get("status") != "success":
            return JsonResponse({"success": False, "error": data.get("message", "Purchase failed. Please try again.")})

        balance_before = locked_wallet.balance
        locked_wallet.balance -= amount
        locked_wallet.save(update_fields=["balance", "updated_at"])

        pheral_transaction = PheralTransaction.objects.create(
            sender=request.user, sender_wallet=locked_wallet,
            transaction_type=(
                PheralTransaction.TransactionType.AIRTIME if bill_type == "airtime"
                else PheralTransaction.TransactionType.DATA
            ),
            amount=amount, currency=currency, status=PheralTransaction.Status.COMPLETED,
            completed_at=timezone.now(), description=description,
            external_reference=data.get("data", {}).get("reference", ""),
        )

        LedgerEntry.objects.create(
            transaction=pheral_transaction, wallet=locked_wallet,
            entry_type=LedgerEntry.EntryType.DEBIT, amount=amount,
            balance_before=balance_before, balance_after=locked_wallet.balance,
            description=description,
        )

    return JsonResponse({
        "success": True,
        "balance": float(locked_wallet.balance),
        "description": description,
        "reference": pheral_transaction.reference,
    })
    return HttpResponse(status=200)

@login_required
def top_up(request):
    """
    Renders the top-up page only. No Flutterwave call happens here —
    payment is started via AJAX (init_top_up) once the user picks a
    currency and amount, so the checkout opens as an inline modal on
    this same page instead of redirecting the browser away.
    """
    currencies = list(Currency.objects.filter(is_active=True).order_by("code"))

    if not currencies:
        messages.error(request, "No wallet currencies are configured.")
        return redirect("wallet")

    currency = get_default_currency()
    if not currency or not currency.is_active:
        currency = currencies[0]

    wallet_data = []
    for active_currency in currencies:
        wallet_obj = get_or_create_wallet(request.user, active_currency)
        wallet_data.append({
            "code": active_currency.code,
            "name": active_currency.name,
            "symbol": active_currency.symbol,
            "balance": float(wallet_obj.balance),
        })

    return render(request, "top_up.html", {
        "currency": currency,
        "currencies": currencies,
        "wallet_data": wallet_data,
        "default_phone": request.user.phone_number,
    })


@login_required
@require_POST
def init_top_up(request):
    """
    AJAX endpoint called right before the Flutterwave Inline modal
    opens. Creates the PENDING transaction and returns only what the
    browser needs to launch the modal — FLW_PUBLIC_KEY, never
    FLW_SECRET_KEY. top_up_callback and the webhook remain the
    source of truth for completing the top-up.
    """
    currencies = list(Currency.objects.filter(is_active=True).order_by("code"))

    amount = parse_amount(request.POST.get("amount"))
    currency_code = (request.POST.get("currency") or "").strip().upper()
    selected_currency = next((c for c in currencies if c.code.upper() == currency_code), None)

    if amount is None:
        return JsonResponse({"success": False, "error": "Enter a valid top-up amount."}, status=400)

    if not selected_currency:
        return JsonResponse({"success": False, "error": "Please select a valid currency."}, status=400)

    if not settings.FLW_PUBLIC_KEY:
        return JsonResponse({"success": False, "error": "Payments are not configured yet."}, status=400)

    wallet_obj = get_or_create_wallet(request.user, selected_currency)

    pheral_transaction = PheralTransaction.objects.create(
        sender=request.user, sender_wallet=wallet_obj,
        transaction_type=PheralTransaction.TransactionType.TOP_UP,
        amount=amount, currency=selected_currency, status=PheralTransaction.Status.PENDING,
    )

    return JsonResponse({
        "success": True,
        "public_key": settings.FLW_PUBLIC_KEY,
        "tx_ref": pheral_transaction.reference,
        "amount": float(amount),
        "currency": selected_currency.code,
        "customer_email": request.user.email or f"{request.user.username}@pheral.app",
        "customer_name": request.user.get_full_name() or request.user.username,
        "redirect_url": request.build_absolute_uri(reverse("top_up_callback")),
    })

@login_required
@require_POST
def verify_top_up(request):
    """
    Called the instant Flutterwave's inline modal closes with a
    result — verifies immediately and returns JSON so the page can
    update the balance in place, no redirect. The webhook still
    fires independently as the true source of authority if this call
    is ever missed.
    """
    tx_ref = request.POST.get("tx_ref", "").strip()
    transaction_id = request.POST.get("transaction_id", "").strip()
    status = request.POST.get("status", "").strip()

    if not tx_ref:
        return JsonResponse({"success": False, "error": "Missing payment reference."}, status=400)

    pheral_transaction = PheralTransaction.objects.filter(
        reference=tx_ref, sender=request.user,
        transaction_type=PheralTransaction.TransactionType.TOP_UP,
    ).first()

    if not pheral_transaction:
        return JsonResponse({"success": False, "error": "Transaction not found."}, status=404)

    if pheral_transaction.status == PheralTransaction.Status.COMPLETED:
        wallet_obj = pheral_transaction.sender_wallet
        return JsonResponse({
            "success": True, "already_completed": True,
            "balance": float(wallet_obj.balance), "currency": wallet_obj.currency.code,
        })

    if status != "successful" or not transaction_id:
        _fail_top_up(pheral_transaction)
        return JsonResponse({"success": False, "error": "Payment was not successful."})

    verified = _verify_flutterwave_transaction(pheral_transaction, transaction_id)

    if not verified:
        return JsonResponse({
            "success": False,
            "error": "We couldn't verify this payment yet. It may still be processing.",
            "pending": True,
        })

    pheral_transaction.refresh_from_db()
    wallet_obj = pheral_transaction.sender_wallet

    return JsonResponse({
        "success": True,
        "balance": float(wallet_obj.balance),
        "currency": wallet_obj.currency.code,
        "reference": pheral_transaction.reference,
    })











# ============================================================
# Airtime / data purchases: one flow instead of two.
#
# WHAT TO DO IN views.py
# 1. DELETE these (they are the second, older flow):
#      NETWORKS, DATA_PLANS, airtime_data(), purchase_bill()
#    and, in urls.py, the routes that point at airtime_data and
#    purchase_bill. You can also delete airtime_data.html.
# 2. DELETE the old versions of these and paste in the ones below:
#      FLUTTERWAVE_BASE_URL, _flutterwave_headers, _call_flutterwave_bill,
#      fetch_data_plans, airtime_purchase, data_purchase
#    (KEEP _local_phone_format, _debit_wallet_for_bill,
#     _refund_failed_bill, _complete_bill and data_plans_api as they are.)
# 3. Add the imports directly below.
#
# WHAT TO DO IN models.py  (NetworkProvider)
#    Add this field next to the other flutterwave_* fields, then run
#    `python manage.py makemigrations && python manage.py migrate`:
#
#        flutterwave_airtime_item_code = models.CharField(
#            max_length=50, blank=True, default="",
#        )
#
#    Then fill in flutterwave_airtime_biller_code and
#    flutterwave_airtime_item_code for each network in the admin
#    (get the real values from GET /v3/bill-categories?country=NG).
#
# WHAT TO DO IN settings.py
#    The bill code used FLUTTERWAVE_SECRET_KEY while the rest of your
#    Flutterwave code uses FLW_SECRET_KEY. Everything below uses
#    FLW_SECRET_KEY, so you can remove FLUTTERWAVE_SECRET_KEY.
#    Optional, only if you route bill calls through a static-IP proxy:
#        FLW_PROXY_URL = os.environ.get("FLW_PROXY_URL", "")
# ============================================================

# ---------- IMPORTS ----------
import logging

from django.core.cache import cache

logger = logging.getLogger(__name__)


# ---------- FLUTTERWAVE HELPERS ----------

FLUTTERWAVE_BASE_URL = "https://api.flutterwave.com/v3"

BILL_OK = "ok"                # Flutterwave accepted the payment
BILL_FAILED = "failed"        # Flutterwave answered and said no: safe to refund
BILL_UNKNOWN = "unknown"      # no usable answer: the payment may have gone through


def _flutterwave_headers():
    return {
        "Authorization": f"Bearer {settings.FLW_SECRET_KEY}",
        "Content-Type": "application/json",
    }


def flw_proxies():
    """Static-IP proxy for calls that must come from a whitelisted IP. None = go direct."""
    url = getattr(settings, "FLW_PROXY_URL", "")
    return {"http": url, "https": url} if url else None


def _call_flutterwave_bill(*, biller_code, item_code, customer_phone, amount, reference):
    """
    Send an airtime/data payment to Flutterwave.

    Returns (outcome, flw_reference).

    BILL_UNKNOWN matters: after a timeout or a 5xx we cannot tell whether
    Flutterwave paid the bill. Refunding then could hand the customer free
    airtime, so the caller leaves the transaction PENDING instead.

    `reference` is our own PheralTransaction.reference, so the payment can
    be looked up on Flutterwave's side later.
    """
    try:
        response = requests.post(
            f"{FLUTTERWAVE_BASE_URL}/billers/{biller_code}/items/{item_code}/payment",
            json={
                "country": "NG",
                "customer_id": customer_phone,
                "amount": float(amount),
                "reference": reference,
            },
            headers=_flutterwave_headers(),
            proxies=flw_proxies(),
            timeout=30,
        )
    except requests.RequestException:
        logger.exception("Flutterwave bill call failed to complete (ref %s)", reference)
        return BILL_UNKNOWN, None

    if response.status_code >= 500:
        logger.error("Flutterwave bill call returned %s (ref %s)", response.status_code, reference)
        return BILL_UNKNOWN, None

    try:
        data = response.json()
    except ValueError:
        logger.error("Flutterwave bill call returned non-JSON (ref %s)", reference)
        return BILL_UNKNOWN, None

    if data.get("status") == "success":
        response_data = data.get("data") or {}
        return BILL_OK, response_data.get("flw_ref") or response_data.get("reference") or reference

    # An IP that isn't whitelisted shows up here, so keep the real message.
    logger.warning("Flutterwave rejected bill (ref %s): %s", reference, data.get("message"))
    return BILL_FAILED, None


def fetch_data_plans(network):
    """
    Live-fetches data bundle plans and current prices from Flutterwave
    (VTU prices change often), cached for 5 minutes. Returns a list of
    {name, amount, item_code}, or [] if the lookup fails.

    Verify the response shape of this endpoint against the current
    Flutterwave docs before going live.
    """
    if not settings.FLW_SECRET_KEY or not network.flutterwave_data_biller:
        return []

    cache_key = f"flw_data_plans:{network.pk}"
    cached = cache.get(cache_key)
    if cached is not None:
        return cached

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
    plans = [p for p in plans if p["amount"] and p["item_code"]]

    if plans:  # never cache a failed lookup
        cache.set(cache_key, plans, 300)

    return plans


# ---------- SHARED BILL HELPERS ----------

def _bill_currency():
    """Bills are Naira-only: no silent fallback to some other currency."""
    return Currency.objects.filter(code__iexact="NGN", is_active=True).first()


def _ng_bill_phone(raw):
    """Local 11-digit form (08012345678) of a Nigerian number, or None."""
    normalized = normalize_phone_number(raw, "NG")
    if not normalized.startswith("+234"):
        return None
    return _local_phone_format(normalized)


def _finish_bill(request, pheral_transaction, outcome, flw_reference, success_message):
    if outcome == BILL_OK:
        _complete_bill(pheral_transaction, flw_reference)
        messages.success(request, success_message)

    elif outcome == BILL_FAILED:
        _refund_failed_bill(pheral_transaction)
        messages.error(request, "Purchase failed. Your wallet has been refunded.")

    else:
        # Left PENDING on purpose; see _call_flutterwave_bill.
        messages.warning(
            request,
            "We're still confirming this purchase. Your balance is on hold until it "
            "settles, so check your transactions before trying again.",
        )

    return redirect("wallet")


# ---------- VIEWS ----------

@login_required
def airtime_purchase(request):
    currency = _bill_currency()
    wallet_obj = get_or_create_wallet(request.user, currency) if currency else None
    networks = NetworkProvider.objects.filter(is_active=True)
    context = {"networks": networks, "wallet": wallet_obj, "currency": currency}

    if request.method != "POST":
        return render(request, "airtime.html", context)

    def fail(message):
        messages.error(request, message)
        return render(request, "airtime.html", context)

    if not currency:
        return fail("Airtime isn't available right now.")

    if not settings.FLW_SECRET_KEY:
        return fail("Bill payments are not configured yet.")

    network = networks.filter(pk=request.POST.get("network")).first()
    if not network:
        return fail("Select a network.")

    if not (network.flutterwave_airtime_biller_code and network.flutterwave_airtime_item_code):
        return fail("Airtime isn't set up for this network yet.")

    phone = _ng_bill_phone(request.POST.get("phone_number", ""))
    if not phone:
        return fail("Airtime is only available for Nigerian phone numbers.")

    amount = parse_amount(request.POST.get("amount"))
    if amount is None:
        return fail("Enter a valid amount.")

    # Debit first, then call Flutterwave (same pattern as withdraw()).
    pheral_transaction, error = _debit_wallet_for_bill(
        request.user, amount, currency,
        PheralTransaction.TransactionType.AIRTIME,
        description=f"{network.name} airtime — {phone}",
        metadata={"network": network.code, "phone_number": phone},
    )
    if error:
        return fail(error)

    outcome, flw_reference = _call_flutterwave_bill(
        biller_code=network.flutterwave_airtime_biller_code,
        item_code=network.flutterwave_airtime_item_code,
        customer_phone=phone,
        amount=amount,
        reference=pheral_transaction.reference,
    )

    return _finish_bill(
        request, pheral_transaction, outcome, flw_reference,
        f"{currency.symbol}{amount:,.2f} airtime sent to {phone}.",
    )


@login_required
def data_purchase(request):
    currency = _bill_currency()
    wallet_obj = get_or_create_wallet(request.user, currency) if currency else None
    networks = NetworkProvider.objects.filter(is_active=True)
    context = {"networks": networks, "wallet": wallet_obj, "currency": currency}

    if request.method != "POST":
        return render(request, "data.html", context)

    def fail(message):
        messages.error(request, message)
        return render(request, "data.html", context)

    if not currency:
        return fail("Data bundles aren't available right now.")

    if not settings.FLW_SECRET_KEY:
        return fail("Bill payments are not configured yet.")

    network = networks.filter(pk=request.POST.get("network")).first()
    if not network:
        return fail("Select a network.")

    if not network.flutterwave_data_biller_code:
        return fail("Data bundles aren't set up for this network yet.")

    phone = _ng_bill_phone(request.POST.get("phone_number", ""))
    if not phone:
        return fail("Data bundles are only available for Nigerian phone numbers.")

    # SECURITY: the price and name come from Flutterwave, never from the form.
    # The browser only tells us WHICH plan (item_code); if it also chose the
    # amount, anyone could buy a 10GB plan for 1 naira.
    item_code = request.POST.get("item_code", "").strip()
    plan = next((p for p in fetch_data_plans(network) if p["item_code"] == item_code), None)
    if not plan:
        return fail("That data plan is no longer available. Please pick another.")

    amount = parse_amount(plan["amount"])
    if amount is None:
        return fail("That data plan is unavailable right now.")

    pheral_transaction, error = _debit_wallet_for_bill(
        request.user, amount, currency,
        PheralTransaction.TransactionType.DATA,
        description=f"{network.name} {plan['name']} — {phone}",
        metadata={"network": network.code, "phone_number": phone, "plan": plan["name"]},
    )
    if error:
        return fail(error)

    outcome, flw_reference = _call_flutterwave_bill(
        biller_code=network.flutterwave_data_biller_code,
        item_code=item_code,
        customer_phone=phone,
        amount=amount,
        reference=pheral_transaction.reference,
    )

    return _finish_bill(
        request, pheral_transaction, outcome, flw_reference,
        f"{plan['name']} sent to {phone}.",
    )

@login_required
def _unused(): pass  # placeholder marker, ignore

def check_username(request):
    username = request.GET.get("username", "").strip()

    if len(username) < 3:
        return JsonResponse({"available": False, "reason": "too_short"})

    if not re.match(r"^[a-zA-Z0-9_]+$", username):
        return JsonResponse({"available": False, "reason": "invalid_chars"})

    exists = User.objects.filter(username__iexact=username).exists()
    return JsonResponse({"available": not exists, "reason": "taken" if exists else None})

def lookup_account(request):
    """
    Used by the 2-step login flow to show a name/avatar preview
    before the password field appears. Deliberately returns the
    SAME shape whether or not the phone exists — real user data
    only when found, an anonymous placeholder otherwise — so this
    endpoint can't be used to enumerate registered phone numbers.
    """
    phone_raw = request.GET.get("phone", "").strip()
    region = request.GET.get("region", "NG").strip().upper()

    phone_number = normalize_phone_number(phone_raw, region)
    if not phone_number:
        return JsonResponse({"found": False})

    user = User.objects.filter(phone_number=phone_number).first()

    if user:
        return JsonResponse({
            "found": True,
            "first_name": user.first_name,
            "avatar": user.avatar.url if user.avatar else "",
        })

    return JsonResponse({"found": False})

def login_view(request):
    if request.user.is_authenticated:
        return redirect("chat")

    if request.method != "POST":
        return render(request, "login.html", {"regions": SUPPORTED_REGIONS, "selected_region": "NG"})

    region = request.POST.get("region", "NG").strip().upper()
    phone_raw = request.POST.get("phone_number", "").strip()
    password = request.POST.get("password", "")
    remember_me = request.POST.get("remember_me") == "on"

    context = {"regions": SUPPORTED_REGIONS, "selected_region": region, "phone_raw": phone_raw}

    if region not in dict(SUPPORTED_REGIONS):
        region = "NG"

    phone_number = normalize_phone_number(phone_raw, region)

    if not phone_raw:
        messages.error(request, "Phone number is required.")
        return render(request, "login.html", context)

    if not phone_number:
        messages.error(request, "Enter a valid phone number.")
        return render(request, "login.html", context)

    if not password:
        messages.error(request, "Password is required.")
        return render(request, "login.html", context)

    try:
        user_obj = User.objects.get(phone_number=phone_number)
    except User.DoesNotExist:
        messages.error(request, "Invalid phone number or password.")
        return render(request, "login.html", context)

    user = authenticate(request, phone_number=phone_number, password=password)

    if user is None:
        messages.error(request, "Invalid phone number or password.")
        return render(request, "login.html", context)

    if not user.is_active:
        messages.error(request, "This account is inactive.")
        return render(request, "login.html", context)

    login(request, user)
    user.last_seen = timezone.now()
    user.save(update_fields=["last_seen"])

    if remember_me:
        request.session.set_expiry(60 * 60 * 24 * 30)  # 30 days
    else:
        request.session.set_expiry(0)  # expires when the browser closes

    return redirect("chat")


@login_required
@require_POST
def toggle_chat_flag(request, conversation_id, flag):
    """
    Single endpoint for pin/mute/archive toggles — all three are
    booleans on ConversationParticipant, all three follow the same
    "flip it, return the new state" pattern.
    """
    if flag not in ("pin", "mute", "archive"):
        return JsonResponse({"success": False}, status=400)

    participant = get_object_or_404(
        ConversationParticipant, conversation_id=conversation_id, user=request.user,
    )
    field = {"pin": "is_pinned", "mute": "is_muted", "archive": "is_archived"}[flag]

    setattr(participant, field, not getattr(participant, field))
    participant.save(update_fields=[field])

    return JsonResponse({"success": True, "value": getattr(participant, field)})


@login_required
@require_POST
def bulk_chat_action(request):
    """
    Applies one action to several conversations at once — the
    multi-select toolbar in chat_list.html. "delete" here means
    leaving/hiding the conversation for this user only (setting
    is_archived), never deleting it for the other participant.
    """
    action = request.POST.get("action", "")
    ids = request.POST.getlist("conversation_ids[]")

    if not ids or action not in ("read", "archive", "unarchive", "mute", "unmute"):
        return JsonResponse({"success": False}, status=400)

    participants = ConversationParticipant.objects.filter(
        conversation_id__in=ids, user=request.user,
    )

    if action == "read":
        participants.update(last_read_at=timezone.now())
    elif action == "archive":
        participants.update(is_archived=True)
    elif action == "unarchive":
        participants.update(is_archived=False)
    elif action == "mute":
        participants.update(is_muted=True)
    elif action == "unmute":
        participants.update(is_muted=False)

    return JsonResponse({"success": True, "count": participants.count()})

@login_required
def cards_and_accounts(request):
    account = VirtualAccount.objects.filter(user=request.user).first()
    cards = VirtualCard.objects.filter(user=request.user).order_by("-created_at")
    currency = get_default_currency()
    wallet_obj = get_or_create_wallet(request.user, currency) if currency else None

    usd_currency = Currency.objects.filter(code="USD", is_active=True).first()
    usd_wallet = get_or_create_wallet(request.user, usd_currency) if usd_currency else None

    return render(request, "cards_and_accounts.html", {
        "wallet": wallet_obj,
        "currency": currency,
        "account": account,
        "cards": cards,
        "usd_currency": usd_currency,
        "usd_wallet": usd_wallet,
    })


@login_required
@require_POST
def create_virtual_card(request):
    """
    Issues a new USD virtual card via Flutterwave, funded at
    creation from the user's USD wallet balance. Debit happens only
    after Flutterwave confirms card creation succeeded — same
    debit-after-success discipline as purchase_bill, since card
    issuance is a single synchronous call with no separate webhook
    step to fall back on.
    """

    amount = parse_amount(request.POST.get("initial_funding"))
    usd_currency = Currency.objects.filter(code="USD", is_active=True).first()

    if not usd_currency:
        return JsonResponse({"success": False, "error": "USD is not available on your account yet."}, status=400)

    if amount is None or amount < Decimal("2.00"):
        return JsonResponse({"success": False, "error": "Minimum funding to create a card is $2.00."}, status=400)

    if not settings.FLW_SECRET_KEY:
        return JsonResponse({"success": False, "error": "Not configured yet."}, status=400)

    wallet_obj = get_or_create_wallet(request.user, usd_currency)

    with transaction.atomic():
        locked_wallet = Wallet.objects.select_for_update().get(pk=wallet_obj.pk)

        if locked_wallet.balance < amount:
            return JsonResponse({"success": False, "error": "Insufficient USD balance."})

        try:
            response = requests.post(
                "https://api.flutterwave.com/v3/virtual-cards",
                json={
                    "currency": "USD",
                    "amount": float(amount),
                    "billing_name": request.user.get_full_name() or request.user.username,
                },
                headers={
                    "Authorization": f"Bearer {settings.FLW_SECRET_KEY}",
                    "Content-Type": "application/json",
                },
                timeout=20,
            )
            data = response.json()
        except (requests.RequestException, ValueError):
            return JsonResponse({"success": False, "error": "Could not reach the card issuer. Please try again."}, status=502)

        if data.get("status") != "success":
            return JsonResponse({"success": False, "error": data.get("message", "Could not create card.")})

        card_data = data.get("data", {})

        balance_before = locked_wallet.balance
        locked_wallet.balance -= amount
        locked_wallet.save(update_fields=["balance", "updated_at"])

        card = VirtualCard.objects.create(
            user=request.user, currency=usd_currency,
            flw_card_id=str(card_data.get("id", "")),
            masked_pan=card_data.get("masked_pan", ""),
            expiry_month=str(card_data.get("expiration", "")).split("/")[0] if card_data.get("expiration") else "",
            expiry_year=str(card_data.get("expiration", "")).split("/")[-1] if card_data.get("expiration") else "",
            card_name=card_data.get("name_on_card", ""),
            balance=amount,
        )

        pheral_transaction = PheralTransaction.objects.create(
            sender=request.user, sender_wallet=locked_wallet,
            transaction_type=PheralTransaction.TransactionType.WITHDRAWAL,
            amount=amount, currency=usd_currency, status=PheralTransaction.Status.COMPLETED,
            completed_at=timezone.now(), description="Virtual card funding",
        )

        LedgerEntry.objects.create(
            transaction=pheral_transaction, wallet=locked_wallet,
            entry_type=LedgerEntry.EntryType.DEBIT, amount=amount,
            balance_before=balance_before, balance_after=locked_wallet.balance,
            description="Virtual card creation",
        )

    return JsonResponse({
        "success": True,
        "card": {
            "id": card.id, "masked_pan": card.masked_pan,
            "expiry": f"{card.expiry_month}/{card.expiry_year[-2:]}" if card.expiry_month else "",
            "balance": float(card.balance), "status": card.status,
        },
    })


@login_required
@require_POST
def toggle_card_status(request, card_id):
    card = get_object_or_404(VirtualCard, pk=card_id, user=request.user)

    if card.status == VirtualCard.Status.TERMINATED:
        return JsonResponse({"success": False, "error": "This card has been terminated."}, status=400)

    new_status = VirtualCard.Status.FROZEN if card.status == VirtualCard.Status.ACTIVE else VirtualCard.Status.ACTIVE
    flw_action = "block" if new_status == VirtualCard.Status.FROZEN else "unblock"

    try:
        response = requests.put(
            f"https://api.flutterwave.com/v3/virtual-cards/{card.flw_card_id}/status/{flw_action}",
            headers={"Authorization": f"Bearer {settings.FLW_SECRET_KEY}"},
            timeout=15,
        )
        data = response.json()
    except (requests.RequestException, ValueError):
        return JsonResponse({"success": False, "error": "Could not reach the card issuer."}, status=502)

    if data.get("status") != "success":
        return JsonResponse({"success": False, "error": data.get("message", "Could not update card status.")})

    card.status = new_status
    card.save(update_fields=["status"])

    return JsonResponse({"success": True, "status": card.status})


@login_required
@require_POST
def fund_virtual_card(request, card_id):
    card = get_object_or_404(VirtualCard, pk=card_id, user=request.user, status=VirtualCard.Status.ACTIVE)
    amount = parse_amount(request.POST.get("amount"))

    if amount is None:
        return JsonResponse({"success": False, "error": "Enter a valid amount."}, status=400)

    wallet_obj = get_or_create_wallet(request.user, card.currency)

    with transaction.atomic():
        locked_wallet = Wallet.objects.select_for_update().get(pk=wallet_obj.pk)

        if locked_wallet.balance < amount:
            return JsonResponse({"success": False, "error": "Insufficient balance."})

        try:
            response = requests.post(
                f"https://api.flutterwave.com/v3/virtual-cards/{card.flw_card_id}/fund",
                json={"amount": float(amount), "debit_currency": card.currency.code},
                headers={"Authorization": f"Bearer {settings.FLW_SECRET_KEY}"},
                timeout=20,
            )
            data = response.json()
        except (requests.RequestException, ValueError):
            return JsonResponse({"success": False, "error": "Could not reach the card issuer."}, status=502)

        if data.get("status") != "success":
            return JsonResponse({"success": False, "error": data.get("message", "Funding failed.")})

        balance_before = locked_wallet.balance
        locked_wallet.balance -= amount
        locked_wallet.save(update_fields=["balance", "updated_at"])

        card.balance += amount
        card.save(update_fields=["balance"])

        pheral_transaction = PheralTransaction.objects.create(
            sender=request.user, sender_wallet=locked_wallet,
            transaction_type=PheralTransaction.TransactionType.WITHDRAWAL,
            amount=amount, currency=card.currency, status=PheralTransaction.Status.COMPLETED,
            completed_at=timezone.now(), description=f"Card funding — {card.masked_pan}",
        )

        LedgerEntry.objects.create(
            transaction=pheral_transaction, wallet=locked_wallet,
            entry_type=LedgerEntry.EntryType.DEBIT, amount=amount,
            balance_before=balance_before, balance_after=locked_wallet.balance,
            description="Card funding",
        )

    return JsonResponse({"success": True, "wallet_balance": float(locked_wallet.balance), "card_balance": float(card.balance)})


@login_required
@require_POST
def reveal_card_details(request, card_id):
    """
    One-time reveal of full PAN/CVV, fetched fresh from Flutterwave
    each time — never cached or stored. Returned once, straight to
    the browser, never logged.
    """
    card = get_object_or_404(VirtualCard, pk=card_id, user=request.user)

    try:
        response = requests.get(
            f"https://api.flutterwave.com/v3/virtual-cards/{card.flw_card_id}",
            headers={"Authorization": f"Bearer {settings.FLW_SECRET_KEY}"},
            timeout=15,
        )
        data = response.json()
    except (requests.RequestException, ValueError):
        return JsonResponse({"success": False, "error": "Could not reach the card issuer."}, status=502)

    if data.get("status") != "success":
        return JsonResponse({"success": False, "error": "Could not retrieve card details."})

    card_data = data.get("data", {})
    return JsonResponse({
        "success": True,
        "card_pan": card_data.get("card_pan", ""),
        "cvv": card_data.get("cvv", ""),
        "expiration": card_data.get("expiration", ""),
    })

@login_required
@require_POST
def create_virtual_account(request):
    """
    Creates a permanent virtual account for the user via Flutterwave.
    Nigerian permanent accounts require BVN — that's a CBN/regulatory
    requirement, not a Flutterwave restriction, so this cannot be
    bypassed. Requires a `bvn` field on User for NG users; add one
    before this can work for them if it doesn't already exist.
    """

    bvn = request.POST.get("bvn", "").strip()
    currency = get_default_currency()

    if not currency:
        return JsonResponse({"success": False, "error": "No wallet currency is configured."}, status=400)

    if VirtualAccount.objects.filter(user=request.user, is_active=True).exists():
        return JsonResponse({"success": False, "error": "You already have a virtual account."}, status=400)

    if currency.code == "NGN" and not bvn:
        return JsonResponse({"success": False, "error": "BVN is required to create a Nigerian bank account."}, status=400)

    if not settings.FLW_SECRET_KEY:
        return JsonResponse({"success": False, "error": "Not configured yet."}, status=400)

    wallet_obj = get_or_create_wallet(request.user, currency)
    tx_ref = generate_reference(prefix="VA")

    payload = {
        "email": request.user.email or f"{request.user.username}@pheral.app",
        "is_permanent": True,
        "bvn": bvn,
        "tx_ref": tx_ref,
        "phonenumber": request.user.phone_number,
        "firstname": request.user.first_name,
        "lastname": request.user.last_name,
        "narration": f"Pheral - {request.user.username}",
    }

    try:
        response = requests.post(
            "https://api.flutterwave.com/v3/virtual-account-numbers",
            json=payload,
            headers={
                "Authorization": f"Bearer {settings.FLW_SECRET_KEY}",
                "Content-Type": "application/json",
            },
            timeout=20,
        )
        data = response.json()
    except (requests.RequestException, ValueError):
        return JsonResponse({"success": False, "error": "Could not reach the payment network."}, status=502)

    if data.get("status") != "success":
        return JsonResponse({"success": False, "error": data.get("message", "Could not create account.")})

    va_data = data.get("data", {})

    account = VirtualAccount.objects.create(
        user=request.user, wallet=wallet_obj,
        account_number=va_data.get("account_number", ""),
        bank_name=va_data.get("bank_name", ""),
        account_name=va_data.get("account_name") or f"{request.user.first_name} {request.user.last_name}".strip(),
        flw_reference=va_data.get("flw_ref", tx_ref),
        order_ref=va_data.get("order_ref", ""),
    )

    return JsonResponse({
        "success": True,
        "account_number": account.account_number,
        "bank_name": account.bank_name,
        "account_name": account.account_name,
    })

@login_required
@require_POST
def add_bank_account(request):
    """
    Add and verify a Nigerian bank account for withdrawals.

    Flutterwave resolves the account number + bank code first.
    Only successfully resolved accounts are saved to BankAccount.
    """

    account_number = (request.POST.get("account_number") or "").strip()
    bank_code = (request.POST.get("bank_code") or "").strip()
    bank_name = (request.POST.get("bank_name") or "").strip()

    if not account_number or not bank_code:
        messages.error(
            request,
            "Enter a valid account number and select a bank.",
        )
        return redirect("withdraw")

    if len(account_number) != 10 or not account_number.isdigit():
        messages.error(
            request,
            "Enter a valid 10-digit Nigerian bank account number.",
        )
        return redirect("withdraw")

    if not settings.FLW_SECRET_KEY:
        messages.error(
            request,
            "Bank verification is not configured yet.",
        )
        return redirect("withdraw")

    try:
        response = requests.post(
            "https://api.flutterwave.com/v3/accounts/resolve",
            json={
                "account_number": account_number,
                "account_bank": bank_code,
            },
            headers={
                "Authorization": (
                    f"Bearer {settings.FLW_SECRET_KEY}"
                ),
                "Content-Type": "application/json",
            },
            timeout=15,
        )

        data = response.json()

    except (requests.RequestException, ValueError):
        messages.error(
            request,
            "We couldn't verify that bank account. Please try again.",
        )
        return redirect("withdraw")

    if data.get("status") != "success":
        messages.error(
            request,
            "We couldn't verify that bank account. Check the details and try again.",
        )
        return redirect("withdraw")

    resolved_data = data.get("data") or {}

    resolved_account_number = (
        resolved_data.get("account_number")
        or account_number
    )

    resolved_account_name = (
        resolved_data.get("account_name")
        or ""
    ).strip()

    if not resolved_account_name:
        messages.error(
            request,
            "The bank account could not be resolved.",
        )
        return redirect("withdraw")

    bank_account, created = BankAccount.objects.get_or_create(
        user=request.user,
        account_number=resolved_account_number,
        bank_code=bank_code,
        defaults={
            "account_name": resolved_account_name,
            "bank_name": bank_name or bank_code,
            "is_active": True,
        },
    )

    if not created:
        bank_account.account_name = resolved_account_name
        bank_account.bank_name = bank_name or bank_account.bank_name
        bank_account.is_active = True
        bank_account.save(
            update_fields=[
                "account_name",
                "bank_name",
                "is_active",
            ]
        )

    messages.success(
        request,
        f"Bank account verified: {resolved_account_name}.",
    )

    return redirect("withdraw")

@login_required
@require_POST
def add_bank_account(request):
    """
    Verify and save a Nigerian bank account for Pheral withdrawals.

    The account name is resolved through Flutterwave and is never
    trusted from user input.
    """

    account_number = (request.POST.get("account_number") or "").strip()
    bank_code = (request.POST.get("bank_code") or "").strip()
    bank_name = (request.POST.get("bank_name") or "").strip()

    if not account_number or not bank_code:
        messages.error(
            request,
            "Enter an account number and select a bank.",
        )
        return redirect("withdraw")

    if len(account_number) != 10 or not account_number.isdigit():
        messages.error(
            request,
            "Enter a valid 10-digit Nigerian bank account number.",
        )
        return redirect("withdraw")

    if not settings.FLW_SECRET_KEY:
        messages.error(
            request,
            "Bank verification is not configured yet.",
        )
        return redirect("withdraw")

    try:
        response = requests.post(
            "https://api.flutterwave.com/v3/accounts/resolve",
            json={
                "account_number": account_number,
                "account_bank": bank_code,
            },
            headers={
                "Authorization": f"Bearer {settings.FLW_SECRET_KEY}",
                "Content-Type": "application/json",
            },
            timeout=15,
        )

        data = response.json()

    except (requests.RequestException, ValueError):
        messages.error(
            request,
            "We couldn't verify the bank account. Please try again.",
        )
        return redirect("withdraw")

    if data.get("status") != "success":
        messages.error(
            request,
            "We couldn't verify that bank account. Check the details and try again.",
        )
        return redirect("withdraw")

    resolved_data = data.get("data") or {}

    resolved_account_number = (
        resolved_data.get("account_number")
        or account_number
    )

    resolved_account_name = (
        resolved_data.get("account_name")
        or ""
    ).strip()

    if not resolved_account_name:
        messages.error(
            request,
            "The bank account could not be resolved.",
        )
        return redirect("withdraw")

    bank_account, created = BankAccount.objects.get_or_create(
        user=request.user,
        account_number=resolved_account_number,
        bank_code=bank_code,
        defaults={
            "account_name": resolved_account_name,
            "bank_name": bank_name or bank_code,
            "flutterwave_recipient_id": "",
            "is_active": True,
        },
    )

    if not created:
        bank_account.account_name = resolved_account_name
        bank_account.bank_name = bank_name or bank_account.bank_name
        bank_account.is_active = True

        bank_account.save(
            update_fields=[
                "account_name",
                "bank_name",
                "is_active",
            ]
        )

    messages.success(
        request,
        f"Bank account verified: {resolved_account_name}.",
    )

    return redirect("withdraw")

@login_required
@require_POST
def sync_withdrawal_status(request, reference):
    """
    Sync a Pheral withdrawal with its current Flutterwave status.
    """

    pheral_transaction = (
        PheralTransaction.objects
        .filter(
            user=request.user,
            reference=reference,
            transaction_type=(
                PheralTransaction.TransactionType.WITHDRAWAL
            ),
        )
        .first()
    )

    if not pheral_transaction:
        return JsonResponse(
            {
                "status": "error",
                "message": "Withdrawal not found.",
            },
            status=404,
        )

    if not settings.FLW_SECRET_KEY:
        return JsonResponse(
            {
                "status": "error",
                "message": "Flutterwave is not configured.",
            },
            status=500,
        )

    try:
        response = requests.get(
            "https://api.flutterwave.com/v3/transfers",
            params={
                "reference": reference,
            },
            headers={
                "Authorization": (
                    f"Bearer {settings.FLW_SECRET_KEY}"
                ),
                "Content-Type": "application/json",
            },
            timeout=15,
        )

        data = response.json()

    except (requests.RequestException, ValueError):
        return JsonResponse(
            {
                "status": "error",
                "message": "Could not contact Flutterwave.",
            },
            status=502,
        )

    transfer_data = data.get("data") or {}

    flutterwave_status = (
        transfer_data.get("status") or ""
    ).upper()

    # ---------------------------------------------------------
    # SUCCESSFUL
    # ---------------------------------------------------------
    if flutterwave_status == "SUCCESSFUL":

        with transaction.atomic():

            locked_txn = (
                PheralTransaction.objects
                .select_for_update()
                .get(pk=pheral_transaction.pk)
            )

            if locked_txn.status == (
                PheralTransaction.Status.PENDING
            ):
                locked_txn.status = (
                    PheralTransaction.Status.COMPLETED
                )
                locked_txn.completed_at = timezone.now()
                locked_txn.external_reference = str(
                    transfer_data.get("id") or ""
                )

                locked_txn.save(
                    update_fields=[
                        "status",
                        "completed_at",
                        "external_reference",
                    ]
                )

        return JsonResponse(
            {
                "status": "success",
                "transaction_status": "completed",
            }
        )

    # ---------------------------------------------------------
    # FAILED
    # ---------------------------------------------------------
    if flutterwave_status == "FAILED":

        with transaction.atomic():

            locked_txn = (
                PheralTransaction.objects
                .select_for_update()
                .get(pk=pheral_transaction.pk)
            )

            if locked_txn.status == (
                PheralTransaction.Status.PENDING
            ):

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
                locked_txn.external_reference = str(
                    transfer_data.get("id") or ""
                )

                locked_txn.save(
                    update_fields=[
                        "status",
                        "completed_at",
                        "external_reference",
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

        return JsonResponse(
            {
                "status": "success",
                "transaction_status": "failed",
            }
        )

    # ---------------------------------------------------------
    # STILL PROCESSING
    # ---------------------------------------------------------
    return JsonResponse(
        {
            "status": "success",
            "transaction_status": "pending",
            "flutterwave_status": flutterwave_status,
        }
    )

@login_required
def withdraw(request):
    """
    Withdraw funds from the user's Pheral wallet to a verified bank account
    using Flutterwave Transfers.
    """

    currency = get_default_currency()

    wallet_obj = (
        get_or_create_wallet(request.user, currency)
        if currency
        else None
    )

    accounts = BankAccount.objects.filter(
        user=request.user,
        is_active=True,
    ).order_by("-created_at")

    # ---------------------------------------------------------
    # Fetch Nigerian banks for the Add Bank Account UI
    # ---------------------------------------------------------
    banks = []

    if settings.FLW_SECRET_KEY:
        try:
            response = requests.get(
                "https://api.flutterwave.com/v3/banks/NG",
                headers={
                    "Authorization": f"Bearer {settings.FLW_SECRET_KEY}",
                    "Content-Type": "application/json",
                },
                timeout=15,
            )

            bank_data = response.json()

            if bank_data.get("status") == "success":
                banks = bank_data.get("data") or []

        except (requests.RequestException, ValueError):
            banks = []

    context = {
        "accounts": accounts,
        "wallet": wallet_obj,
        "currency": currency,
        "banks": banks,
    }

    # ---------------------------------------------------------
    # Withdrawal
    # ---------------------------------------------------------
    if request.method == "POST":

        amount = parse_amount(request.POST.get("amount"))

        bank_account = accounts.filter(
            pk=request.POST.get("bank_account")
        ).first()

        if amount is None:
            messages.error(
                request,
                "Enter a valid withdrawal amount.",
            )
            return render(
                request,
                "withdraw.html",
                context,
            )

        if not currency:
            messages.error(
                request,
                "No withdrawal currency is configured.",
            )
            return redirect("wallet")

        if not bank_account:
            messages.error(
                request,
                "Select a valid bank account.",
            )
            return render(
                request,
                "withdraw.html",
                context,
            )

        if not settings.FLW_SECRET_KEY:
            messages.error(
                request,
                "Withdrawals are not configured yet.",
            )
            return redirect("wallet")

        if currency.code.upper() != "NGN":
            messages.error(
                request,
                "Nigerian bank withdrawals are currently available in NGN only.",
            )
            return redirect("wallet")

        # -----------------------------------------------------
        # Lock wallet + debit funds + create pending transaction
        # -----------------------------------------------------
        with transaction.atomic():

            locked_wallet = (
                Wallet.objects
                .select_for_update()
                .get(pk=wallet_obj.pk)
            )

            if locked_wallet.balance < amount:
                messages.error(
                    request,
                    "Insufficient wallet balance.",
                )

                context["wallet"] = locked_wallet

                return render(
                    request,
                    "withdraw.html",
                    context,
                )

            balance_before = locked_wallet.balance

            locked_wallet.balance -= amount

            locked_wallet.save(
                update_fields=[
                    "balance",
                    "updated_at",
                ]
            )

            pheral_transaction = PheralTransaction.objects.create(
                sender=request.user,
                sender_wallet=locked_wallet,
                transaction_type=(
                    PheralTransaction.TransactionType.WITHDRAWAL
                ),
                amount=amount,
                currency=currency,
                status=PheralTransaction.Status.PENDING,
                description=(
                    f"Withdrawal to "
                    f"{bank_account.bank_name}"
                ),
            )
            # SANDBOX TESTING ONLY
            pheral_transaction.reference = (
            f"{pheral_transaction.reference}_PMCKDU_1"
            )
            pheral_transaction.save(update_fields=["reference"])

            LedgerEntry.objects.create(
                transaction=pheral_transaction,
                wallet=locked_wallet,
                entry_type=LedgerEntry.EntryType.DEBIT,
                amount=amount,
                balance_before=balance_before,
                balance_after=locked_wallet.balance,
                description="Withdrawal (pending)",
            )

        # -----------------------------------------------------
        # Submit transfer to Flutterwave
        # -----------------------------------------------------
        try:

            response = requests.post(
                "https://api.flutterwave.com/v3/transfers",
                json={
                    "account_bank": bank_account.bank_code,
                    "account_number": bank_account.account_number,
                    "amount": float(amount),
                    "currency": currency.code.upper(),
                    "narration": "Pheral wallet withdrawal",
                    "reference": pheral_transaction.reference,
                },
                headers={
                    "Authorization": (
                        f"Bearer {settings.FLW_SECRET_KEY}"
                    ),
                    "Content-Type": "application/json",
                },
                timeout=20,
            )

            data = response.json()
            print("FLUTTERWAVE WITHDRAWAL RESPONSE:", response.status_code, data)

        except (requests.RequestException, ValueError):

            data = {
                "status": "error",
            }

        # -----------------------------------------------------
        # Flutterwave rejected the transfer
        # -----------------------------------------------------
        if data.get("status") != "success":

            with transaction.atomic():

                locked_txn = (
                    PheralTransaction.objects
                    .select_for_update()
                    .get(pk=pheral_transaction.pk)
                )

                if (
                    locked_txn.status
                    == PheralTransaction.Status.PENDING
                ):

                    locked_wallet = (
                        Wallet.objects
                        .select_for_update()
                        .get(
                            pk=locked_txn.sender_wallet_id
                        )
                    )

                    balance_before_refund = locked_wallet.balance

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
                        balance_before=balance_before_refund,
                        balance_after=locked_wallet.balance,
                        description=(
                            "Withdrawal failed — refunded"
                        ),
                    )

            messages.error(
                request,
                "Withdrawal could not be started. "
                "Your balance has been refunded.",
            )

            return redirect("wallet")

        # -----------------------------------------------------
        # Transfer accepted by Flutterwave
        # -----------------------------------------------------
        messages.success(
            request,
            "Withdrawal initiated — it may take a few minutes.",
        )

        return redirect("wallet")

    return render(
        request,
        "withdraw.html",
        context,
    )
