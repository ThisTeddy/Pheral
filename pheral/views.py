"""
Pheral views.

Consolidated: every view / helper is defined exactly once.
Money movement rules used throughout:
  * every balance change happens inside transaction.atomic() with select_for_update()
  * every balance change writes a LedgerEntry
  * external providers (Flutterwave) are only called AFTER our own debit is safely
    recorded as PENDING, and a refund only happens when the provider gave a
    definite "no". A timeout / 5xx leaves the transaction PENDING for the webhook
    or the sync endpoint to settle.
"""
import base64
import hashlib
import hmac
import json
import logging
import re
import secrets
import uuid
from datetime import timedelta
from decimal import Decimal, InvalidOperation

import phonenumbers
import requests
from phonenumbers import NumberParseException, PhoneNumberFormat

from django.conf import settings
from django.contrib import messages
from django.contrib.auth import authenticate, login, logout
from django.contrib.auth.decorators import login_required
from django.contrib.auth.password_validation import validate_password
from django.core.cache import cache
from django.core.paginator import Paginator
from django.core.exceptions import ValidationError
from django.db import IntegrityError, transaction
from django.db.models import Count, Exists, OuterRef, Prefetch, Q
from django.http import Http404, HttpResponse, HttpResponseForbidden, JsonResponse
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
    NetworkProvider,
    Notification,
    PheralTransaction,
    PhoneOTP,
    Post,
    PostComment,
    PostLike,
    PushDevice,
    Receipt,
    RevenueRecord,
    Status,
    StatusView,
    User,
    UserPresence,
    VirtualAccount,
    VirtualCard,
    Wallet,
    WalletToken,
    generate_reference,
)

logger = logging.getLogger(__name__)

FLW_API = "https://api.flutterwave.com/v3"


# ============================================================
# PHONE HELPERS
# ============================================================

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

DEFAULT_REGION = "NG"


def region_dial_code(region):
    try:
        return "+" + str(phonenumbers.country_code_for_region(region))
    except Exception:
        return ""


def normalize_phone_number(phone, region=DEFAULT_REGION):
    """E.164 (+2348012345678) or "" if the number isn't valid."""
    if not phone:
        return ""

    raw = str(phone).strip()
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
    try:
        parsed = phonenumbers.parse(user.phone_number, None)
        return phonenumbers.region_code_for_number(parsed) or DEFAULT_REGION
    except NumberParseException:
        return DEFAULT_REGION


# ============================================================
# MONEY / WALLET HELPERS
# ============================================================

MAX_AMOUNT = Decimal("1000000000000")


def parse_amount(value):
    """Positive, finite Decimal rounded to 2dp, or None."""
    try:
        amount = Decimal(str(value).strip())
    except (InvalidOperation, TypeError, ValueError):
        return None

    if not amount.is_finite():
        return None

    amount = amount.quantize(Decimal("0.01"))

    if amount <= Decimal("0") or amount > MAX_AMOUNT:
        return None

    return amount


def get_or_create_wallet(user, currency):
    wallet_obj, _ = Wallet.objects.get_or_create(
        user=user, currency=currency, defaults={"balance": Decimal("0.00")}
    )
    return wallet_obj


def get_default_currency():
    """NGN for the Nigerian MVP, otherwise the first active currency."""
    currency = Currency.objects.filter(code__iexact="NGN", is_active=True).first()
    return currency or Currency.objects.filter(is_active=True).first()


def get_or_create_wallet_token(user):
    token, _ = WalletToken.objects.get_or_create(user=user, defaults={"is_active": True})
    if not token.is_active:
        token.is_active = True
        token.save(update_fields=["is_active"])
    return token


def wallet_snapshot(user, currencies):
    """Balances for every active currency, for the pay / top-up screens."""
    return [
        {
            "code": c.code,
            "name": c.name,
            "symbol": c.symbol,
            "balance": float(get_or_create_wallet(user, c).balance),
        }
        for c in currencies
    ]


def convert_amount(amount, source_currency, target_currency):
    """
    Convert between currencies (direct rate, inverse rate, or via NGN),
    rounded to the target currency's decimal places. None if no path exists.
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


class InsufficientBalance(Exception):
    pass


def send_wallet_payment(sender, recipient, currency, amount, description="", transaction_type=None):
    """
    The ONE place a wallet-to-wallet payment happens (pay page, transfer page,
    agent command, hire payments). Locks both wallets in pk order (no
    deadlocks), moves the money, writes ledger entries, receipt, chat message
    and notification.

    transaction_type defaults to TRANSFER; pass PheralTransaction.TransactionType.HIRE_PAYMENT
    (etc.) so the ledger and wallet history record what the money was actually for.

    Raises InsufficientBalance. Returns (transaction, conversation).
    """
    transaction_type = transaction_type or PheralTransaction.TransactionType.TRANSFER
    sender_wallet = get_or_create_wallet(sender, currency)
    recipient_wallet = get_or_create_wallet(recipient, currency)

    with transaction.atomic():
        locked = {
            w.pk: w
            for w in Wallet.objects.select_for_update()
            .filter(pk__in=[sender_wallet.pk, recipient_wallet.pk])
            .order_by("pk")
        }
        locked_sender = locked[sender_wallet.pk]
        locked_recipient = locked[recipient_wallet.pk]

        if locked_sender.balance < amount:
            raise InsufficientBalance()

        sender_before = locked_sender.balance
        recipient_before = locked_recipient.balance

        locked_sender.balance -= amount
        locked_sender.save(update_fields=["balance", "updated_at"])
        locked_recipient.balance += amount
        locked_recipient.save(update_fields=["balance", "updated_at"])

        txn = PheralTransaction.objects.create(
            sender=sender, recipient=recipient,
            sender_wallet=locked_sender, recipient_wallet=locked_recipient,
            transaction_type=transaction_type,
            amount=amount, currency=currency, fee=Decimal("0.00"),
            status=PheralTransaction.Status.COMPLETED,
            description=description, completed_at=timezone.now(),
        )

        LedgerEntry.objects.create(
            transaction=txn, wallet=locked_sender,
            entry_type=LedgerEntry.EntryType.DEBIT, amount=amount,
            balance_before=sender_before, balance_after=locked_sender.balance,
            description=description,
        )
        LedgerEntry.objects.create(
            transaction=txn, wallet=locked_recipient,
            entry_type=LedgerEntry.EntryType.CREDIT, amount=amount,
            balance_before=recipient_before, balance_after=locked_recipient.balance,
            description=description,
        )

        receipt = Receipt.objects.create(
            transaction=txn, payer=sender, recipient=recipient,
            amount=amount, currency=currency, description=description,
        )

        conversation = get_or_create_direct_conversation(sender, recipient)

        Message.objects.create(
            conversation=conversation, sender=sender,
            message_type=Message.MessageType.PAYMENT,
            content=f"{currency.symbol}{amount:,.2f} sent",
            transaction=txn, receipt=receipt,
        )

        Notification.objects.create(
            user=recipient,
            notification_type=Notification.NotificationType.PAYMENT,
            title="Payment received",
            body=f"@{sender.username} sent {currency.symbol}{amount:,.2f}",
            link=reverse("receipt_detail", args=[receipt.reference]),
        )

    return txn, conversation


FX_QUOTE_TTL_SECONDS = int(getattr(settings, "FX_QUOTE_TTL_SECONDS", 45))


def get_fx_margin_rate():
    """Fraction of a conversion Pheral keeps as margin (settings.FX_MARGIN_PERCENT, default 1.5%)."""
    percent = Decimal(str(getattr(settings, "FX_MARGIN_PERCENT", "1.5")))
    return percent / Decimal("100")


def build_fx_quote(user, source_currency, target_currency, amount):
    """
    Price a conversion between two of the user's own wallets. `amount` is what
    leaves the source wallet, unchanged. The margin is taken out of the
    mid-market converted amount, so what's quoted as "you get" is already net
    of Pheral's fee. Raises ValueError("no_rate") or ValueError("too_small").
    Returns (receive_amount, margin_amount).
    """
    mid_converted = convert_amount(amount, source_currency, target_currency)
    if mid_converted is None:
        raise ValueError("no_rate")

    quantize_to = Decimal("1").scaleb(-target_currency.decimal_places)
    margin_amount = (mid_converted * get_fx_margin_rate()).quantize(quantize_to)
    receive_amount = (mid_converted - margin_amount).quantize(quantize_to)

    if receive_amount <= 0:
        raise ValueError("too_small")

    return receive_amount, margin_amount


def execute_fx_conversion(user, source_currency, target_currency, amount, receive_amount, margin_amount):
    """
    Move `amount` out of the user's source wallet and `receive_amount` into
    their target wallet. The amounts are exactly what a quote already fixed
    (see build_fx_quote / fx_quote / fx_convert) — this never recalculates the
    rate, so what the user confirmed is exactly what moves. Records the
    margin as Pheral's revenue.

    Raises InsufficientBalance if the source wallet can't cover `amount`.
    Returns the PheralTransaction.
    """
    source_wallet = get_or_create_wallet(user, source_currency)
    target_wallet = get_or_create_wallet(user, target_currency)

    with transaction.atomic():
        locked = {
            w.pk: w
            for w in Wallet.objects.select_for_update()
            .filter(pk__in=[source_wallet.pk, target_wallet.pk])
            .order_by("pk")
        }
        locked_source = locked[source_wallet.pk]
        locked_target = locked[target_wallet.pk]

        if locked_source.balance < amount:
            raise InsufficientBalance()

        source_before = locked_source.balance
        target_before = locked_target.balance

        locked_source.balance -= amount
        locked_source.save(update_fields=["balance", "updated_at"])
        locked_target.balance += receive_amount
        locked_target.save(update_fields=["balance", "updated_at"])

        txn = PheralTransaction.objects.create(
            sender=user, recipient=user,
            sender_wallet=locked_source, recipient_wallet=locked_target,
            transaction_type=PheralTransaction.TransactionType.FX,
            amount=amount, currency=source_currency,
            # NOTE: no `fee=margin_amount` here — margin_amount is denominated in
            # target_currency, but this transaction's `currency` FK is source_currency.
            # Storing it as `fee` would print the wrong currency symbol anywhere fee
            # is rendered (e.g. wallet.html's "+ {{ txn.currency.symbol }}{{ txn.fee }}").
            # The margin is recorded correctly below, on RevenueRecord, which has its
            # own currency field, and is kept here in metadata for reference.
            status=PheralTransaction.Status.COMPLETED, completed_at=timezone.now(),
            description=f"Converted to {target_currency.code}",
            metadata={
                "target_currency": target_currency.code,
                "converted_amount": str(receive_amount),
                "margin_amount": str(margin_amount),
                "margin_currency": target_currency.code,
            },
        )

        LedgerEntry.objects.create(
            transaction=txn, wallet=locked_source,
            entry_type=LedgerEntry.EntryType.DEBIT, amount=amount,
            balance_before=source_before, balance_after=locked_source.balance,
            description=f"Converted to {target_currency.code}",
        )
        LedgerEntry.objects.create(
            transaction=txn, wallet=locked_target,
            entry_type=LedgerEntry.EntryType.CREDIT, amount=receive_amount,
            balance_before=target_before, balance_after=locked_target.balance,
            description=f"Converted from {source_currency.code}",
        )

        if margin_amount > 0:
            RevenueRecord.objects.create(
                user=user, revenue_type=RevenueRecord.RevenueType.FX_MARGIN,
                amount=margin_amount, currency=target_currency, transaction=txn,
                description=f"FX margin {source_currency.code}->{target_currency.code}",
            )

    return txn


# ============================================================
# CHAT HELPERS
# ============================================================

def get_or_create_direct_conversation(user1, user2):
    """
    Existing direct conversation for the pair, or a new one. Race-safe: a unique
    constraint on (conversation_type, direct_pair_key) rejects a second one.
    """
    if user1.pk == user2.pk:
        raise ValueError("A user cannot create a conversation with themselves.")

    low, high = sorted([user1.pk, user2.pk])
    pair_key = f"{low}-{high}"

    conversation = Conversation.objects.filter(
        conversation_type=Conversation.ConversationType.DIRECT, direct_pair_key=pair_key,
    ).first()
    if conversation:
        return conversation

    try:
        with transaction.atomic():
            conversation = Conversation.objects.create(
                conversation_type=Conversation.ConversationType.DIRECT,
                created_by=user1, direct_pair_key=pair_key,
            )
            ConversationParticipant.objects.create(conversation=conversation, user=user1)
            ConversationParticipant.objects.create(conversation=conversation, user=user2)
            return conversation
    except IntegrityError:
        return Conversation.objects.get(
            conversation_type=Conversation.ConversationType.DIRECT, direct_pair_key=pair_key,
        )


def create_system_message(conversation, content):
    return Message.objects.create(
        conversation=conversation, sender=None,
        message_type=Message.MessageType.SYSTEM, content=content,
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


# ============================================================
# OTP HELPERS
# ============================================================

OTP_TTL = timedelta(minutes=10)
MAX_OTP_ATTEMPTS = 5
OTP_RESEND_COOLDOWN_SECONDS = 60


def issue_phone_otp(user, purpose=PhoneOTP.PURPOSE_VERIFICATION):
    """Invalidate old codes, create a fresh one, return it."""
    PhoneOTP.objects.filter(user=user, purpose=purpose, is_used=False).update(is_used=True)

    code = f"{secrets.randbelow(1_000_000):06d}"

    PhoneOTP.objects.create(
        user=user, phone_number=user.phone_number, code=code, purpose=purpose,
        expires_at=timezone.now() + OTP_TTL,
    )

    # TODO: replace with a real SMS provider. Printing is development-only.
    print(f"\n{'=' * 50}\nPHERAL OTP ({purpose})\nPhone: {user.phone_number}\nOTP:   {code}\n{'=' * 50}\n")

    return code


def check_phone_otp(user, code, purpose):
    """Returns (ok, error_message). Counts failed attempts on the active code."""
    otp = (
        PhoneOTP.objects.filter(user=user, purpose=purpose, is_used=False)
        .order_by("-created_at").first()
    )

    if not otp:
        return False, "No active code. Please request a new one."

    if otp.is_expired:
        return False, "This OTP has expired."

    if otp.attempts >= MAX_OTP_ATTEMPTS:
        return False, "Too many wrong attempts. Please request a new code."

    if not hmac.compare_digest(otp.code, code):
        otp.attempts += 1
        otp.save(update_fields=["attempts"])
        return False, "Invalid OTP."

    otp.is_used = True
    otp.save(update_fields=["is_used"])
    return True, None


def otp_resend_allowed(user, purpose):
    key = f"otp_resend:{purpose}:{user.pk}"
    if cache.get(key):
        return False
    cache.set(key, True, OTP_RESEND_COOLDOWN_SECONDS)
    return True


# ============================================================
# LANDING
# ============================================================

def landing(request):
    return render(request, "landing.html")


# ============================================================
# AUTHENTICATION
# ============================================================

USERNAME_RE = re.compile(r"^[A-Za-z0-9_.]{3,30}$")


def register(request):
    if request.user.is_authenticated:
        return redirect("chat")

    if request.method != "POST":
        return render(request, "register.html", {"regions": SUPPORTED_REGIONS, "selected_region": DEFAULT_REGION})

    username = request.POST.get("username", "").strip()
    region = request.POST.get("region", DEFAULT_REGION).strip().upper()
    phone_raw = request.POST.get("phone_number", "").strip()
    password = request.POST.get("password", "")
    password_confirm = request.POST.get("password_confirm", "")
    first_name = request.POST.get("first_name", "").strip()
    last_name = request.POST.get("last_name", "").strip()
    terms_accepted = request.POST.get("terms_accepted") == "on"

    if region not in dict(SUPPORTED_REGIONS):
        region = DEFAULT_REGION

    context = {"regions": SUPPORTED_REGIONS, "selected_region": region, "phone_raw": phone_raw}

    def fail(message):
        messages.error(request, message)
        return render(request, "register.html", context)

    if not username:
        return fail("Username is required.")
    if not USERNAME_RE.fullmatch(username):
        return fail("Username must be 3-30 characters: letters, numbers, underscores or dots.")
    if not phone_raw:
        return fail("Phone number is required.")

    phone_number = normalize_phone_number(phone_raw, region)
    if not phone_number:
        return fail("Enter a valid phone number for the selected country.")

    if not first_name or not last_name:
        return fail("First name and last name are required.")
    if not password:
        return fail("Password is required.")
    if password != password_confirm:
        return fail("Passwords do not match.")

    try:
        validate_password(password)
    except ValidationError as exc:
        return fail(" ".join(exc.messages))

    if not terms_accepted:
        return fail("You must accept the Terms of Service to continue.")
    if User.objects.filter(username__iexact=username).exists():
        return fail("That username is already taken.")
    if User.objects.filter(phone_number=phone_number).exists():
        return fail("That phone number is already registered.")

    try:
        with transaction.atomic():
            user = User.objects.create_user(
                username=username, phone_number=phone_number, password=password,
                first_name=first_name, last_name=last_name, is_phone_verified=False,
            )
    except IntegrityError:
        return fail("That username or phone number is already registered.")

    issue_phone_otp(user)
    request.session["otp_user_id"] = user.pk
    return redirect("verify_otp")


def _finish_signup(request, user):
    user.is_phone_verified = True
    user.save(update_fields=["is_phone_verified"])

    get_or_create_wallet_token(user)
    currency = get_default_currency()
    if currency:
        get_or_create_wallet(user, currency)

    login(request, user)
    request.session.pop("otp_user_id", None)
    return redirect("chat")


def verify_otp(request):
    user_id = request.session.get("otp_user_id")
    if not user_id:
        return redirect("register")

    user = get_object_or_404(User, pk=user_id)

    if request.method != "POST":
        return render(request, "verify_otp.html")

    code = request.POST.get("code", "").strip()

    # Development shortcut ONLY. Never active when DEBUG is False.
    master = getattr(settings, "MASTER_OTP_CODE", "")
    if settings.DEBUG and master and hmac.compare_digest(code, str(master)):
        logger.warning("MASTER OTP used for user=%s", user.username)
        return _finish_signup(request, user)

    ok, error = check_phone_otp(user, code, PhoneOTP.PURPOSE_VERIFICATION)
    if not ok:
        messages.error(request, error)
        return render(request, "verify_otp.html")

    return _finish_signup(request, user)


def resend_otp(request):
    user_id = request.session.get("otp_user_id")
    if not user_id:
        return redirect("register")

    user = get_object_or_404(User, pk=user_id)

    if user.is_phone_verified:
        return redirect("login_view")

    if request.method != "POST":
        return redirect("verify_otp")

    if not otp_resend_allowed(user, PhoneOTP.PURPOSE_VERIFICATION):
        messages.error(request, "Please wait a minute before requesting another code.")
        return redirect("verify_otp")

    issue_phone_otp(user)
    messages.success(request, "A new verification code has been sent.")
    return redirect("verify_otp")


def forgot_password(request):
    if request.user.is_authenticated:
        return redirect("chat_list")

    if request.method != "POST":
        return render(request, "forgot_password.html")

    phone_raw = request.POST.get("phone_number", "").strip()
    region = request.POST.get("region", DEFAULT_REGION).strip().upper()
    if region not in dict(SUPPORTED_REGIONS):
        region = DEFAULT_REGION

    if not phone_raw:
        messages.error(request, "Phone number is required.")
        return render(request, "forgot_password.html")

    phone_number = normalize_phone_number(phone_raw, region)
    user = User.objects.filter(phone_number=phone_number).first() if phone_number else None

    if not user:
        messages.error(request, "No account was found with that phone number.")
        return render(request, "forgot_password.html")

    if not user.is_active:
        messages.error(request, "This account is inactive.")
        return render(request, "forgot_password.html")

    issue_phone_otp(user, PhoneOTP.PURPOSE_PASSWORD_RESET)
    request.session["password_reset_user_id"] = user.pk
    request.session.pop("password_reset_verified", None)
    return redirect("verify_password_reset_otp")


def verify_password_reset_otp(request):
    user_id = request.session.get("password_reset_user_id")
    if not user_id:
        return redirect("forgot_password")

    user = get_object_or_404(User, pk=user_id)

    if request.method != "POST":
        return render(request, "verify_password_reset_otp.html")

    code = request.POST.get("code", "").strip()
    ok, error = check_phone_otp(user, code, PhoneOTP.PURPOSE_PASSWORD_RESET)

    if not ok:
        messages.error(request, error)
        return render(request, "verify_password_reset_otp.html")

    request.session["password_reset_verified"] = True
    return redirect("reset_password")


def reset_password(request):
    user_id = request.session.get("password_reset_user_id")
    if not user_id or not request.session.get("password_reset_verified"):
        return redirect("forgot_password")

    user = get_object_or_404(User, pk=user_id)

    if request.method != "POST":
        return render(request, "reset_password.html")

    password = request.POST.get("password", "")
    password_confirm = request.POST.get("password_confirm", "")

    if not password:
        messages.error(request, "Password is required.")
        return render(request, "reset_password.html")

    if password != password_confirm:
        messages.error(request, "Passwords do not match.")
        return render(request, "reset_password.html")

    try:
        validate_password(password, user=user)
    except ValidationError as exc:
        messages.error(request, " ".join(exc.messages))
        return render(request, "reset_password.html")

    user.set_password(password)
    user.save(update_fields=["password"])

    request.session.pop("password_reset_user_id", None)
    request.session.pop("password_reset_verified", None)

    messages.success(request, "Your password has been reset. You can now log in.")
    return redirect("login_view")


def resend_password_reset_otp(request):
    user_id = request.session.get("password_reset_user_id")
    if not user_id:
        return redirect("forgot_password")

    user = get_object_or_404(User, pk=user_id)

    if request.method != "POST":
        return redirect("verify_password_reset_otp")

    if not otp_resend_allowed(user, PhoneOTP.PURPOSE_PASSWORD_RESET):
        messages.error(request, "Please wait a minute before requesting another code.")
        return redirect("verify_password_reset_otp")

    issue_phone_otp(user, PhoneOTP.PURPOSE_PASSWORD_RESET)
    messages.success(request, "A new verification code has been sent.")
    return redirect("verify_password_reset_otp")


def check_username(request):
    username = request.GET.get("username", "").strip()

    if len(username) < 3:
        return JsonResponse({"available": False, "reason": "too_short"})

    if not USERNAME_RE.fullmatch(username):
        return JsonResponse({"available": False, "reason": "invalid_chars"})

    exists = User.objects.filter(username__iexact=username).exists()
    return JsonResponse({"available": not exists, "reason": "taken" if exists else None})


def lookup_account(request):
    """
    Preview (first name + avatar) for the 2-step login. Same response shape
    whether or not the phone exists, so it can't be used to enumerate numbers.
    """
    phone_raw = request.GET.get("phone", "").strip()
    region = request.GET.get("region", DEFAULT_REGION).strip().upper()
    if region not in dict(SUPPORTED_REGIONS):
        region = DEFAULT_REGION

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
        return render(request, "login.html", {"regions": SUPPORTED_REGIONS, "selected_region": DEFAULT_REGION})

    region = request.POST.get("region", DEFAULT_REGION).strip().upper()
    if region not in dict(SUPPORTED_REGIONS):
        region = DEFAULT_REGION

    phone_raw = request.POST.get("phone_number", "").strip()
    password = request.POST.get("password", "")
    remember_me = request.POST.get("remember_me") == "on"

    context = {"regions": SUPPORTED_REGIONS, "selected_region": region, "phone_raw": phone_raw}

    def fail(message):
        messages.error(request, message)
        return render(request, "login.html", context)

    if not phone_raw:
        return fail("Phone number is required.")

    phone_number = normalize_phone_number(phone_raw, region)
    if not phone_number:
        return fail("Enter a valid phone number.")

    if not password:
        return fail("Password is required.")

    existing = User.objects.filter(phone_number=phone_number).first()
    if existing and not existing.is_active:
        return fail("This account is inactive.")

    user = authenticate(request, phone_number=phone_number, password=password)
    if user is None:
        return fail("Invalid phone number or password.")

    if not user.is_phone_verified:
        issue_phone_otp(user)
        request.session["otp_user_id"] = user.pk
        messages.info(request, "Please verify your phone number to continue.")
        return redirect("verify_otp")

    login(request, user)
    user.last_seen = timezone.now()
    user.save(update_fields=["last_seen"])

    request.session.set_expiry(60 * 60 * 24 * 30 if remember_me else 0)
    return redirect("chat")


def logout_view(request):
    if request.user.is_authenticated:
        request.user.last_seen = timezone.now()
        request.user.save(update_fields=["last_seen"])
    logout(request)
    return redirect("landing")


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

    return render(request, "profile.html", {"profile_user": profile_user, "posts": posts, "jobs": jobs})


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
# CHAT (direct conversations; groups use group_chat)
# ============================================================

@login_required
def chat(request, conversation_id=None, username=None):
    if username:
        other_user = get_object_or_404(User, username__iexact=username)
        if other_user.pk == request.user.pk:
            return redirect("chat_list")
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
                pk=reply_to_id, conversation=conversation, is_deleted=False,
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

    # Named chat_messages, never `messages`: that name is django.contrib.messages.
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
        return redirect("chat_list")
    conversation = get_or_create_direct_conversation(request.user, other_user)
    return redirect("chat", conversation_id=conversation.pk)


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
    """Typing state lives in the cache (a session is per-user, so it can never reach the other side)."""
    conversation = get_object_or_404(
        Conversation, id=conversation_id, participants__user=request.user, is_active=True,
    )

    if request.method == "POST":
        typing = request.POST.get("typing") == "1"
        key = f"typing:{conversation.pk}:{request.user.pk}"
        if typing:
            cache.set(key, True, 6)
        else:
            cache.delete(key)
        return JsonResponse({"success": True, "typing": typing})

    other_ids = conversation.participants.exclude(user=request.user).values_list("user_id", flat=True)
    other_typing = any(cache.get(f"typing:{conversation.pk}:{uid}") for uid in other_ids)
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


@login_required
def chat_list(request):
    conversations = (
        Conversation.objects.filter(participants__user=request.user, is_active=True)
        .prefetch_related(
            Prefetch(
                "participants",
                queryset=ConversationParticipant.objects.select_related("user", "user__presence"),
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

    preview_map = {
        Message.MessageType.IMAGE: "Photo",
        Message.MessageType.VIDEO: "Video",
        Message.MessageType.VOICE: "Voice note",
        Message.MessageType.FILE: "File",
        Message.MessageType.PAYMENT: "Payment",
        Message.MessageType.HIRE: "Hire request",
    }

    chat_items = []

    for conversation in conversations:
        participants = getattr(conversation, "chat_participants", [])
        message_list = getattr(conversation, "ordered_messages", [])

        current_participant = next((p for p in participants if p.user_id == request.user.id), None)
        if current_participant is None:
            continue

        is_group = conversation.conversation_type == Conversation.ConversationType.GROUP

        other_user = None
        if not is_group:
            other_user = next((p.user for p in participants if p.user_id != request.user.id), None)

        last_message = message_list[0] if message_list else None

        last_read = current_participant.last_read_at
        unread_count = sum(
            1 for m in message_list
            if m.sender_id != request.user.id and (last_read is None or m.created_at > last_read)
        )

        last_message_preview = ""
        if last_message:
            if last_message.message_type == Message.MessageType.AGENT:
                last_message_preview = last_message.content or "Agent"
            else:
                last_message_preview = preview_map.get(last_message.message_type, last_message.content or "")

        last_message_prefix = "You: " if last_message and last_message.sender_id == request.user.id else ""

        if is_group:
            display_name = conversation.name or "Group"
        elif other_user:
            display_name = other_user.get_full_name() or other_user.username
        else:
            display_name = "Unknown user"

        avatar_url = None
        if is_group:
            if conversation.avatar:
                avatar_url = conversation.avatar.url
        elif other_user and other_user.avatar:
            avatar_url = other_user.avatar.url

        presence = getattr(other_user, "presence", None) if other_user else None

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
            "is_group": is_group,
            "is_online": bool(presence and presence.is_online),
            "last_seen_at": presence.last_seen_at if presence else None,
            "latest_activity": last_message.created_at if last_message else conversation.created_at,
        })

    chat_items.sort(key=lambda item: item["latest_activity"], reverse=True)

    return render(request, "chat_list.html", {
        "conversations": conversations,
        "chat_items": chat_items,
        "current_user": request.user,
    })


@login_required
@require_POST
def toggle_chat_flag(request, conversation_id, flag):
    """Pin / mute / archive: flip the boolean and return the new value."""
    fields = {"pin": "is_pinned", "mute": "is_muted", "archive": "is_archived"}
    if flag not in fields:
        return JsonResponse({"success": False}, status=400)

    participant = get_object_or_404(
        ConversationParticipant, conversation_id=conversation_id, user=request.user,
    )
    field = fields[flag]
    setattr(participant, field, not getattr(participant, field))
    participant.save(update_fields=[field])

    return JsonResponse({"success": True, "value": getattr(participant, field)})


@login_required
@require_POST
def bulk_chat_action(request):
    """Multi-select toolbar in chat_list.html. Only ever affects THIS user's participant rows."""
    action = request.POST.get("action", "")
    ids = request.POST.getlist("conversation_ids[]")

    if not ids or action not in ("read", "archive", "unarchive", "mute", "unmute"):
        return JsonResponse({"success": False}, status=400)

    participants = ConversationParticipant.objects.filter(conversation_id__in=ids, user=request.user)
    count = participants.count()

    updates = {
        "read": {"last_read_at": timezone.now()},
        "archive": {"is_archived": True},
        "unarchive": {"is_archived": False},
        "mute": {"is_muted": True},
        "unmute": {"is_muted": False},
    }
    participants.update(**updates[action])

    return JsonResponse({"success": True, "count": count})


# ============================================================
# GROUPS
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
    if request.method != "POST":
        return render(request, "group_create.html")

    name = request.POST.get("name", "").strip()
    description = request.POST.get("description", "").strip()

    if not name:
        messages.error(request, "Group name is required.")
        return render(request, "group_create.html")

    with transaction.atomic():
        conversation = Conversation.objects.create(
            conversation_type=Conversation.ConversationType.GROUP,
            name=name, description=description, created_by=request.user,
        )
        ConversationParticipant.objects.create(conversation=conversation, user=request.user, is_admin=True)

        currency = get_default_currency()
        if currency:
            GroupLedger.objects.create(conversation=conversation, currency=currency)

    return redirect("group_chat", conversation_id=conversation.pk)


@login_required
def group_chat(request, conversation_id):
    conversation = get_object_or_404(
        Conversation, pk=conversation_id,
        conversation_type=Conversation.ConversationType.GROUP,
        participants__user=request.user,
    )
    participant = get_object_or_404(ConversationParticipant, conversation=conversation, user=request.user)

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

    ledger = GroupLedger.objects.filter(conversation=conversation).first()
    if ledger is None:
        currency = get_default_currency()
        if currency:
            ledger = GroupLedger.objects.create(conversation=conversation, currency=currency)

    chat_messages = (
        Message.objects.filter(conversation=conversation, is_deleted=False)
        .select_related("sender", "transaction", "receipt").order_by("created_at")
    )

    ledger_entries = (
        GroupLedgerEntry.objects.filter(ledger=ledger).select_related("user").order_by("-created_at")[:15]
        if ledger else []
    )

    return render(request, "group_chat.html", {
        "conversation": conversation,
        "chat_messages": chat_messages,
        "ledger": ledger,
        "ledger_entries": ledger_entries,
        "participant": participant,
        "participant_count": conversation.participants.count(),
    })


@login_required
def group_add_member(request, conversation_id):
    conversation = get_object_or_404(
        Conversation, pk=conversation_id, conversation_type=Conversation.ConversationType.GROUP,
    )
    get_object_or_404(ConversationParticipant, conversation=conversation, user=request.user, is_admin=True)

    if request.method == "POST":
        username = request.POST.get("username", "").strip()

        if not username:
            messages.error(request, "Username is required.")
            return redirect("group_add_member", conversation_id=conversation.pk)

        user_to_add = get_object_or_404(User, username__iexact=username, is_active=True)

        if user_to_add == request.user:
            messages.error(request, "You're already in this group.")
            return redirect("group_add_member", conversation_id=conversation.pk)

        _, created = ConversationParticipant.objects.get_or_create(conversation=conversation, user=user_to_add)

        if created:
            messages.success(request, f"@{user_to_add.username} added to the group.")
        else:
            messages.info(request, f"@{user_to_add.username} is already in this group.")

        return redirect("group_chat", conversation_id=conversation.pk)

    query = request.GET.get("q", "").strip()
    existing_user_ids = ConversationParticipant.objects.filter(conversation=conversation).values_list("user_id", flat=True)

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
        "conversation": conversation, "contacts": contacts_qs, "query": query,
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
        "ledger": GroupLedger.objects.filter(conversation=conversation).first(),
    })


@login_required
@require_POST
def group_contribute(request, conversation_id):
    """Personal wallet -> group's shared balance (both rows locked)."""
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

    wallet_obj = get_or_create_wallet(request.user, ledger.currency)

    with transaction.atomic():
        locked_wallet = Wallet.objects.select_for_update().get(pk=wallet_obj.pk)
        locked_ledger = GroupLedger.objects.select_for_update().get(pk=ledger.pk)

        if locked_wallet.balance < amount:
            messages.error(request, "Insufficient wallet balance.")
            return redirect("group_chat", conversation_id=conversation.pk)

        balance_before = locked_wallet.balance
        locked_wallet.balance -= amount
        locked_wallet.save(update_fields=["balance", "updated_at"])

        locked_ledger.balance += amount
        locked_ledger.save(update_fields=["balance", "updated_at"])

        txn = PheralTransaction.objects.create(
            sender=request.user, sender_wallet=locked_wallet,
            transaction_type=PheralTransaction.TransactionType.GROUP_TRANSFER,
            amount=amount, currency=locked_ledger.currency,
            status=PheralTransaction.Status.COMPLETED, completed_at=timezone.now(),
            description=f"Contribution to {conversation.name or 'group'}",
        )

        LedgerEntry.objects.create(
            transaction=txn, wallet=locked_wallet,
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
            transaction=txn,
        )

        conversation.updated_at = timezone.now()
        conversation.save(update_fields=["updated_at"])

    return redirect("group_chat", conversation_id=conversation.pk)


@login_required
@require_POST
def group_withdraw(request, conversation_id):
    """Admin only: group balance -> admin's wallet, minus the group's withdrawal fee."""
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

    wallet_obj = get_or_create_wallet(request.user, ledger.currency)

    with transaction.atomic():
        locked_ledger = GroupLedger.objects.select_for_update().get(pk=ledger.pk)
        locked_wallet = Wallet.objects.select_for_update().get(pk=wallet_obj.pk)

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

        txn = PheralTransaction.objects.create(
            recipient=request.user, recipient_wallet=locked_wallet,
            transaction_type=PheralTransaction.TransactionType.GROUP_TRANSFER,
            amount=max(net_amount, Decimal("0.01")), fee=fee, currency=locked_ledger.currency,
            status=PheralTransaction.Status.COMPLETED, completed_at=timezone.now(),
            description=f"Withdrawal from {conversation.name or 'group'}",
        )

        if net_amount > 0:
            LedgerEntry.objects.create(
                transaction=txn, wallet=locked_wallet,
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
            transaction=txn,
        )

        conversation.updated_at = timezone.now()
        conversation.save(update_fields=["updated_at"])

    return redirect("group_chat", conversation_id=conversation.pk)


@login_required
@require_POST
def group_toggle_admin(request, conversation_id, username):
    """Promote/demote a member. A group can never be left without an admin."""
    conversation = get_object_or_404(
        Conversation, pk=conversation_id, conversation_type=Conversation.ConversationType.GROUP,
    )
    get_object_or_404(ConversationParticipant, conversation=conversation, user=request.user, is_admin=True)

    target = get_object_or_404(
        ConversationParticipant, conversation=conversation, user__username__iexact=username,
    )

    if target.is_admin:
        remaining_admins = (
            ConversationParticipant.objects.filter(conversation=conversation, is_admin=True)
            .exclude(pk=target.pk).count()
        )
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
        messages.error(request, 'Use "Leave group" to remove yourself.')
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
        remaining_admins = (
            ConversationParticipant.objects.filter(conversation=conversation, is_admin=True)
            .exclude(pk=participant.pk).count()
        )
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
# CONTACTS
# ============================================================

MAX_SYNC_CONTACTS = 2000


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
        defaults={"phone_number": contact_user.phone_number or ""},
    )

    messages.success(request, f"@{contact_user.username} added to contacts.")
    return redirect("contacts")


@login_required
@require_POST
def sync_contacts(request):
    """
    Match device contacts against registered users.
    Body: {"contacts": [{"name": "Zoe", "phone": "08012345678"}]}
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
        return JsonResponse({"success": False, "error": f"Too many contacts (max {MAX_SYNC_CONTACTS})."}, status=400)

    region = region_for_user(request.user)

    device = {}  # normalized phone -> name (first wins)
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
        for u in User.objects.filter(phone_number__in=device.keys(), is_active=True).exclude(pk=request.user.pk)
    }

    existing = {
        c.contact_user_id: c
        for c in Contact.objects.filter(
            owner=request.user, contact_user_id__in=[u.pk for u in users_by_phone.values()],
        )
    }

    to_create, to_update, matched = [], [], []

    for phone, user in users_by_phone.items():
        name = device[phone]
        contact = existing.get(user.pk)

        if contact is None:
            contact = Contact(owner=request.user, contact_user=user, phone_number=phone, nickname=name)
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

    not_on_pheral = [{"name": n, "phone": p} for p, n in device.items() if p not in users_by_phone]

    return JsonResponse({
        "success": True,
        "matched": matched,
        "not_on_pheral": not_on_pheral,
        "matched_count": len(matched),
        "not_on_pheral_count": len(not_on_pheral),
        "invalid_count": invalid_count,
    })


@login_required
def search(request):
    query = request.GET.get("q", "").strip()
    users = User.objects.none()

    if query:
        users = (
            User.objects.filter(is_active=True)
            .filter(
                Q(username__icontains=query) | Q(first_name__icontains=query)
                | Q(last_name__icontains=query) | Q(phone_number__icontains=query)
            ).exclude(pk=request.user.pk).order_by("username")[:50]
        )

    return render(request, "search.html", {"query": query, "users": users})


@login_required
def user_lookup(request):
    query = request.GET.get("q", "").strip()
    results = []

    if query:
        matches = (
            User.objects.filter(is_active=True)
            .filter(
                Q(username__icontains=query) | Q(phone_number__icontains=query)
                | Q(first_name__icontains=query) | Q(last_name__icontains=query)
            ).exclude(pk=request.user.pk).order_by("username")[:20]
        )
        results = [
            {
                "username": u.username,
                "display_name": u.display_name,
                "phone_number": u.phone_number,
                "avatar": u.avatar.url if u.avatar else "",
            }
            for u in matches
        ]

    return JsonResponse({"results": results})


@login_required
def mark_message_read(request, message_id):
    message = get_object_or_404(Message, pk=message_id, conversation__participants__user=request.user)
    MessageRead.objects.get_or_create(message=message, user=request.user)
    return JsonResponse({"success": True})


# ============================================================
# PAY A PHERAL USER
# ============================================================

@login_required
def pay_user(request, username):
    recipient = get_object_or_404(User, username__iexact=username, is_active=True)

    if recipient == request.user:
        messages.error(request, "You cannot pay yourself.")
        return redirect("profile_user", username=recipient.username)

    currencies = list(Currency.objects.filter(is_active=True).order_by("code"))
    if not currencies:
        messages.error(request, "No active currency is configured.")
        return redirect("profile_user", username=recipient.username)

    default = get_default_currency()
    currency = default if default and default.is_active else currencies[0]

    def page(selected=None):
        return render(request, "pay.html", {
            "recipient": recipient,
            "currency": selected or currency,
            "currencies": currencies,
            "wallet_data": wallet_snapshot(request.user, currencies),
        })

    if request.method != "POST":
        return page()

    currency_code = (request.POST.get("currency") or "").strip().upper()
    selected = next((c for c in currencies if c.code.upper() == currency_code), None)
    if not selected:
        messages.error(request, "Please select a valid currency.")
        return page()

    amount = parse_amount(request.POST.get("amount"))
    if amount is None:
        messages.error(request, "Enter a valid payment amount.")
        return page(selected)

    description = request.POST.get("description", "").strip()[:255]

    get_or_create_wallet_token(request.user)

    try:
        _, conversation = send_wallet_payment(request.user, recipient, selected, amount, description)
    except InsufficientBalance:
        messages.error(request, "Insufficient wallet balance.")
        return page(selected)

    return redirect("chat", conversation_id=conversation.pk)


@login_required
def global_pay(request):
    currencies = Currency.objects.filter(is_active=True).order_by("code")

    if request.method == "POST":
        username = request.POST.get("username", "").strip()
        recipient = User.objects.filter(username__iexact=username, is_active=True).first()

        if not recipient:
            messages.error(request, "Pheral user not found.")
            return render(request, "global_pay.html", {"currencies": currencies})

        return redirect("pay_user", username=recipient.username)

    return render(request, "global_pay.html", {"currencies": currencies})


@login_required
def currency_converter(request):
    """
    Renders the page only. The actual quote/convert flow is two JSON calls:
    fx_quote() prices it, fx_convert() executes a previously-priced quote.
    """
    currencies = list(Currency.objects.filter(is_active=True).order_by("code"))
    return render(request, "currency_converter.html", {
        "wallet_data": wallet_snapshot(request.user, currencies),
    })


@login_required
@require_POST
def fx_quote(request):
    """
    Price a conversion between two of the user's own wallets and hold it for
    FX_QUOTE_TTL_SECONDS. Nothing moves yet — fx_convert() executes the quote
    this returns. The quote (source/target ids and the exact amounts) is kept
    server-side, keyed by an opaque token, so the browser can't alter the
    numbers between pricing and confirming.
    """
    from_code = (request.POST.get("from") or "").strip().upper()
    to_code = (request.POST.get("to") or "").strip().upper()
    amount = parse_amount(request.POST.get("amount"))

    if not from_code or not to_code:
        return JsonResponse({"success": False, "error": "Select both currencies."}, status=400)

    if from_code == to_code:
        return JsonResponse({"success": False, "error": "Choose two different currencies."}, status=400)

    if amount is None:
        return JsonResponse({"success": False, "error": "Enter a valid amount."}, status=400)

    source = Currency.objects.filter(code__iexact=from_code, is_active=True).first()
    target = Currency.objects.filter(code__iexact=to_code, is_active=True).first()

    if not source or not target:
        return JsonResponse({"success": False, "error": "Select valid currencies."}, status=400)

    wallet_obj = get_or_create_wallet(request.user, source)
    if wallet_obj.balance < amount:
        return JsonResponse({"success": False, "error": "That is more than your available balance."}, status=400)

    try:
        receive_amount, margin_amount = build_fx_quote(request.user, source, target, amount)
    except ValueError as exc:
        if str(exc) == "too_small":
            return JsonResponse({"success": False, "error": "That amount is too small to convert."}, status=400)
        return JsonResponse({"success": False, "error": "No exchange rate is available for that pair."}, status=400)

    token = uuid.uuid4().hex
    cache.set(f"fx_quote:{token}", {
        "user_id": request.user.pk,
        "source_id": source.pk,
        "target_id": target.pk,
        "amount": str(amount),
        "receive": str(receive_amount),
        "margin": str(margin_amount),
    }, FX_QUOTE_TTL_SECONDS)

    rate = (receive_amount / amount) if amount else Decimal("0")

    return JsonResponse({
        "success": True,
        "quote": token,
        "from": source.code,
        "to": target.code,
        "pay": float(amount),
        "receive": float(receive_amount),
        "rate": float(rate),
        "expires_in": FX_QUOTE_TTL_SECONDS,
    })


@login_required
@require_POST
def fx_convert(request):
    """
    Execute a quote from fx_quote(). The quote is consumed on first use; a
    resubmit of the same token (e.g. a retried request after a dropped
    response) returns the original result instead of converting twice.
    """
    token = (request.POST.get("quote") or "").strip()
    if not token:
        return JsonResponse({"success": False, "error": "Missing quote."}, status=400)

    cache_key = f"fx_quote:{token}"
    done_key = f"fx_quote_done:{token}"

    quote = cache.get(cache_key)

    if quote is None:
        done = cache.get(done_key)
        if done and done.get("user_id") == request.user.pk:
            return JsonResponse({"success": True, "already_done": True, **done["result"]})
        return JsonResponse({
            "success": False, "expired": True,
            "error": "That rate expired. Tap Get rate to refresh it.",
        }, status=400)

    # Consume immediately so a second, concurrent request can't reuse it.
    cache.delete(cache_key)

    if quote["user_id"] != request.user.pk:
        return JsonResponse({
            "success": False, "expired": True,
            "error": "That rate expired. Tap Get rate to refresh it.",
        }, status=400)

    source = Currency.objects.filter(pk=quote["source_id"], is_active=True).first()
    target = Currency.objects.filter(pk=quote["target_id"], is_active=True).first()

    if not source or not target:
        return JsonResponse({"success": False, "error": "That rate is no longer available."}, status=400)

    amount = Decimal(quote["amount"])
    receive_amount = Decimal(quote["receive"])
    margin_amount = Decimal(quote["margin"])

    try:
        execute_fx_conversion(request.user, source, target, amount, receive_amount, margin_amount)
    except InsufficientBalance:
        return JsonResponse({"success": False, "error": f"Insufficient {source.code} wallet balance."}, status=400)

    source_wallet = get_or_create_wallet(request.user, source)
    target_wallet = get_or_create_wallet(request.user, target)

    result = {
        "from": source.code,
        "to": target.code,
        "sent": float(amount),
        "received": float(receive_amount),
        "source_balance": float(source_wallet.balance),
        "target_balance": float(target_wallet.balance),
    }

    # Held for a few minutes so a retried confirm reports success instead of erroring.
    cache.set(done_key, {"user_id": request.user.pk, "result": result}, 300)

    return JsonResponse({"success": True, **result})


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
# WALLET
# ============================================================

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

    return render(request, "wallet.html", {
        "wallets": wallets,
        "transactions": transactions,
        "wallet_token": get_or_create_wallet_token(request.user),
    })


# ============================================================
# TOP-UP (Flutterwave Inline)
# ============================================================

def _complete_top_up(pheral_transaction):
    """
    Idempotently credit a pending top-up. Safe from the webhook, the callback
    and verify_top_up in any order: only the first call that finds the
    transaction still PENDING moves money.
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
            transaction=locked_txn, wallet=wallet_obj,
            entry_type=LedgerEntry.EntryType.CREDIT, amount=locked_txn.amount,
            balance_before=balance_before, balance_after=wallet_obj.balance,
            description="Wallet top-up via Flutterwave",
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
    """Ask Flutterwave whether this charge really succeeded; credit the wallet if so."""
    try:
        response = requests.get(
            f"{FLW_API}/transactions/{transaction_id}/verify",
            headers={"Authorization": f"Bearer {settings.FLW_SECRET_KEY}", "Content-Type": "application/json"},
            timeout=15,
        )
        data = response.json()
    except (requests.RequestException, ValueError):
        return False

    if data.get("status") != "success":
        return False

    verified = data.get("data") or {}

    try:
        paid_amount = Decimal(str(verified.get("amount", "0")))
    except InvalidOperation:
        return False

    if (
        verified.get("status") == "successful"
        and verified.get("tx_ref") == pheral_transaction.reference
        and verified.get("currency") == pheral_transaction.currency.code
        and paid_amount >= pheral_transaction.amount
    ):
        _complete_top_up(pheral_transaction)
        return True

    return False


@login_required
def top_up(request):
    """Renders the page only; payment starts via AJAX (init_top_up) so checkout opens as a modal."""
    currencies = list(Currency.objects.filter(is_active=True).order_by("code"))

    if not currencies:
        messages.error(request, "No wallet currencies are configured.")
        return redirect("wallet")

    default = get_default_currency()
    currency = default if default and default.is_active else currencies[0]

    return render(request, "top_up.html", {
        "currency": currency,
        "currencies": currencies,
        "wallet_data": wallet_snapshot(request.user, currencies),
        "default_phone": request.user.phone_number,
    })


@login_required
@require_POST
def init_top_up(request):
    """Creates the PENDING transaction and returns only what the browser needs (public key, never the secret)."""
    currencies = list(Currency.objects.filter(is_active=True).order_by("code"))

    amount = parse_amount(request.POST.get("amount"))
    currency_code = (request.POST.get("currency") or "").strip().upper()
    selected = next((c for c in currencies if c.code.upper() == currency_code), None)

    if amount is None:
        return JsonResponse({"success": False, "error": "Enter a valid top-up amount."}, status=400)

    if not selected:
        return JsonResponse({"success": False, "error": "Please select a valid currency."}, status=400)

    if not getattr(settings, "FLW_PUBLIC_KEY", ""):
        return JsonResponse({"success": False, "error": "Payments are not configured yet."}, status=400)

    wallet_obj = get_or_create_wallet(request.user, selected)

    txn = PheralTransaction.objects.create(
        sender=request.user, sender_wallet=wallet_obj,
        transaction_type=PheralTransaction.TransactionType.TOP_UP,
        amount=amount, currency=selected, status=PheralTransaction.Status.PENDING,
        description="Wallet top-up",
    )

    return JsonResponse({
        "success": True,
        "public_key": settings.FLW_PUBLIC_KEY,
        "tx_ref": txn.reference,
        "amount": float(amount),
        "currency": selected.code,
        "customer_email": request.user.email or f"{request.user.username}@pheral.app",
        "customer_name": request.user.get_full_name() or request.user.username,
        "redirect_url": request.build_absolute_uri(reverse("top_up_callback")),
    })


@login_required
@require_POST
def verify_top_up(request):
    """Called when the inline modal closes. Verifies and returns JSON so the page updates in place."""
    tx_ref = request.POST.get("tx_ref", "").strip()
    transaction_id = request.POST.get("transaction_id", "").strip()
    flw_status = request.POST.get("status", "").strip()

    if not tx_ref:
        return JsonResponse({"success": False, "error": "Missing payment reference."}, status=400)

    txn = PheralTransaction.objects.select_related("sender_wallet__currency").filter(
        reference=tx_ref, sender=request.user,
        transaction_type=PheralTransaction.TransactionType.TOP_UP,
    ).first()

    if not txn:
        return JsonResponse({"success": False, "error": "Transaction not found."}, status=404)

    if txn.status == PheralTransaction.Status.COMPLETED:
        return JsonResponse({
            "success": True, "already_completed": True,
            "balance": float(txn.sender_wallet.balance), "currency": txn.sender_wallet.currency.code,
        })

    if flw_status != "successful" or not transaction_id:
        _fail_top_up(txn)
        return JsonResponse({"success": False, "error": "Payment was not successful."})

    if not _verify_flutterwave_transaction(txn, transaction_id):
        return JsonResponse({
            "success": False,
            "error": "We couldn't verify this payment yet. It may still be processing.",
            "pending": True,
        })

    txn.refresh_from_db()
    wallet_obj = Wallet.objects.select_related("currency").get(pk=txn.sender_wallet_id)

    return JsonResponse({
        "success": True,
        "balance": float(wallet_obj.balance),
        "currency": wallet_obj.currency.code,
        "reference": txn.reference,
    })


@login_required
def top_up_callback(request):
    """Browser redirect target after checkout. The webhook stays the source of truth."""
    tx_ref = request.GET.get("tx_ref")
    transaction_id = request.GET.get("transaction_id")
    flw_status = request.GET.get("status")

    if not tx_ref:
        messages.error(request, "Missing payment reference.")
        return redirect("wallet")

    txn = get_object_or_404(
        PheralTransaction, reference=tx_ref, sender=request.user,
        transaction_type=PheralTransaction.TransactionType.TOP_UP,
    )

    if txn.status == PheralTransaction.Status.COMPLETED:
        messages.success(request, "Top-up successful.")
        return redirect("wallet")

    if flw_status == "cancelled":
        _fail_top_up(txn)
        messages.error(request, "Payment was cancelled.")
        return redirect("wallet")

    if flw_status != "successful" or not transaction_id:
        messages.error(request, "We couldn't verify this payment yet. It may still be processing.")
        return redirect("wallet")

    if _verify_flutterwave_transaction(txn, transaction_id):
        messages.success(request, "Top-up successful.")
    else:
        # Never mark FAILED on an unverifiable answer: the webhook may still credit it.
        messages.error(request, "We couldn't verify this payment yet. It may still be processing.")

    return redirect("wallet")


# ============================================================
# FLUTTERWAVE HELPERS
# ============================================================

def _flw_headers():
    return {
        "Authorization": f"Bearer {settings.FLW_SECRET_KEY}",
        "Content-Type": "application/json",
    }


def flw_proxies():
    """Static-IP proxy for calls Flutterwave requires from a whitelisted IP. None = go direct."""
    url = getattr(settings, "FLW_PROXY_URL", "")
    return {"http": url, "https": url} if url else None


def get_withdrawal_fee():
    """Flat fee in NGN charged on top of the amount sent (settings.WITHDRAWAL_FEE, default 0)."""
    return Decimal(str(getattr(settings, "WITHDRAWAL_FEE", "0"))).quantize(Decimal("0.01"))


def get_nigerian_banks():
    """Nigerian bank list from Flutterwave, cached for 24 hours."""
    banks = cache.get("flw_banks_ng")
    if banks is not None:
        return banks

    banks = []
    try:
        response = requests.get(f"{FLW_API}/banks/NG", headers=_flw_headers(), timeout=15)
        body = response.json()
        if body.get("status") == "success":
            banks = body.get("data") or []
    except (requests.RequestException, ValueError):
        logger.warning("Could not fetch Flutterwave bank list", exc_info=True)

    if banks:
        cache.set("flw_banks_ng", banks, 60 * 60 * 24)

    return banks


# ============================================================
# BANK ACCOUNTS
# ============================================================

@login_required
def bank_accounts(request):
    accounts = BankAccount.objects.filter(user=request.user, is_active=True).order_by("-created_at")
    return render(request, "bank_accounts.html", {"accounts": accounts})


def _flw_resolve_account(account_number, bank_code):
    """
    Ask Flutterwave who owns this account. Returns (account_name, error_message) —
    exactly one of the two is set. The ONLY place this HTTP call is made: both the
    AJAX preview (resolve_bank_account) and the actual save/transfer path
    (get_or_create_verified_bank_account) go through this, so a name is never
    trusted from anywhere but Flutterwave itself.
    """
    if not settings.FLW_SECRET_KEY:
        return None, "Bank verification is not configured yet."

    if len(account_number) != 10 or not account_number.isdigit():
        return None, "Enter a valid 10-digit Nigerian bank account number."

    try:
        response = requests.post(
            f"{FLW_API}/accounts/resolve",
            json={"account_number": account_number, "account_bank": bank_code},
            headers=_flw_headers(), timeout=15,
        )
        data = response.json()
    except (requests.RequestException, ValueError):
        return None, "We couldn't verify that bank account. Please try again."

    account_name = (data.get("data") or {}).get("account_name", "").strip()

    if data.get("status") != "success" or not account_name:
        return None, "We couldn't verify that bank account. Check the details and try again."

    return account_name, None


def get_or_create_verified_bank_account(user, account_number, bank_code, bank_name_hint=""):
    """
    Resolve account_number+bank_code with Flutterwave and save (or refresh) a
    BankAccount row carrying the verified name. Used both by the standalone
    "add bank account" form and by transfer()'s inline "new account" path, so
    typing a fresh account straight into a transfer saves it for next time too.
    Returns (bank_account, error_message) — exactly one is set.
    """
    account_name, error = _flw_resolve_account(account_number, bank_code)
    if error:
        return None, error

    bank_account, created = BankAccount.objects.get_or_create(
        user=user, account_number=account_number, bank_code=bank_code,
        defaults={"account_name": account_name, "bank_name": bank_name_hint or bank_code, "is_active": True},
    )

    if not created:
        bank_account.account_name = account_name
        bank_account.bank_name = bank_name_hint or bank_account.bank_name
        bank_account.is_active = True
        bank_account.save(update_fields=["account_name", "bank_name", "is_active"])

    return bank_account, None


@login_required
def resolve_bank_account(request):
    """AJAX: who owns this account number? The user sees the real name, never types it."""
    account_number = request.GET.get("account_number", "").strip()
    bank_code = request.GET.get("bank_code", "").strip()

    if not account_number or not bank_code:
        return JsonResponse({"success": False, "error": "Missing details."}, status=400)

    account_name, error = _flw_resolve_account(account_number, bank_code)
    if error:
        return JsonResponse({"success": False, "error": error}, status=400)

    return JsonResponse({"success": True, "account_name": account_name})


@login_required
@require_POST
def add_bank_account(request):
    """Verify a Nigerian account with Flutterwave and save it. The name always comes from the bank."""
    account_number = (request.POST.get("account_number") or "").strip()
    bank_code = (request.POST.get("bank_code") or "").strip()
    bank_name = (request.POST.get("bank_name") or "").strip()

    back = "transfer" if request.POST.get("next") == "transfer" else "withdraw"

    if not account_number or not bank_code:
        messages.error(request, "Enter an account number and select a bank.")
        return redirect(back)

    bank_account, error = get_or_create_verified_bank_account(request.user, account_number, bank_code, bank_name)
    if error:
        messages.error(request, error)
        return redirect(back)

    messages.success(request, f"Bank account verified: {bank_account.account_name}.")
    return redirect(back)


# ============================================================
# BANK TRANSFERS / WITHDRAWALS (Flutterwave Transfers)
#
# withdraw() and transfer(destination="bank") share ONE code path:
# initiate_bank_transfer(). Lifecycle:
#   debit wallet (amount + fee) + PENDING txn
#     -> POST /transfers
#          definite rejection   -> settle_withdrawal(..., "FAILED")  = refund
#          timeout / 5xx / junk -> stay PENDING (never refund on "unknown")
#          accepted             -> store transfer id, stay PENDING
#     -> webhook `transfer.completed` or the sync endpoint calls
#        settle_withdrawal() which completes or refunds exactly once.
# ============================================================

PENDING_TRANSFER_MESSAGE = (
    "We couldn't confirm your transfer yet. It is being checked and your balance "
    "will be refunded automatically if it fails."
)


def _status_label(status):
    if status == PheralTransaction.Status.COMPLETED:
        return "completed"
    if status == PheralTransaction.Status.FAILED:
        return "failed"
    return "pending"


def settle_withdrawal(reference, flw_status, transfer_id=""):
    """
    The only place a bank transfer leaves PENDING. Idempotent and safe to call
    from the view, the sync endpoint and the webhook, in any order, any number
    of times. Returns "completed" / "failed" / "pending", or None if unknown.
    """
    flw_status = (flw_status or "").upper()

    if flw_status not in ("SUCCESSFUL", "FAILED"):
        return "pending"  # NEW / PENDING / anything else: still in flight

    lookup = Q()
    if reference:
        lookup |= Q(reference=reference)
    if transfer_id:
        lookup |= Q(external_reference=str(transfer_id))
    if not lookup:
        return None

    with transaction.atomic():
        txn = (
            PheralTransaction.objects.select_for_update()
            .filter(lookup, transaction_type=PheralTransaction.TransactionType.WITHDRAWAL)
            .first()
        )

        if txn is None:
            return None

        if txn.status != PheralTransaction.Status.PENDING:
            return _status_label(txn.status)

        txn.external_reference = str(transfer_id or txn.external_reference or "")
        txn.completed_at = timezone.now()

        if flw_status == "SUCCESSFUL":
            txn.status = PheralTransaction.Status.COMPLETED
            txn.save(update_fields=["status", "completed_at", "external_reference"])

            if txn.fee and txn.fee > 0:  # the fee is only earned once the transfer succeeds
                RevenueRecord.objects.create(
                    user_id=txn.sender_id,
                    revenue_type=RevenueRecord.RevenueType.WITHDRAWAL_FEE,
                    amount=txn.fee, currency_id=txn.currency_id,
                    transaction=txn, description="Withdrawal fee",
                )
            return "completed"

        # FAILED: give back amount + fee
        wallet_obj = Wallet.objects.select_for_update().get(pk=txn.sender_wallet_id)
        refund_total = txn.amount + (txn.fee or Decimal("0.00"))
        balance_before = wallet_obj.balance
        wallet_obj.balance += refund_total
        wallet_obj.save(update_fields=["balance", "updated_at"])

        txn.status = PheralTransaction.Status.FAILED
        txn.save(update_fields=["status", "completed_at", "external_reference"])

        LedgerEntry.objects.create(
            transaction=txn, wallet=wallet_obj,
            entry_type=LedgerEntry.EntryType.CREDIT, amount=refund_total,
            balance_before=balance_before, balance_after=wallet_obj.balance,
            description="Transfer failed, refunded",
        )
        return "failed"


def initiate_bank_transfer(user, bank_account, amount, currency, description):
    """
    Debit + create PENDING txn + submit to Flutterwave.
    Returns (level, message); level is "success", "warning" or "error".
    """
    fee = get_withdrawal_fee()
    total = amount + fee
    wallet_obj = get_or_create_wallet(user, currency)

    with transaction.atomic():
        locked_wallet = Wallet.objects.select_for_update().get(pk=wallet_obj.pk)

        if locked_wallet.balance < total:
            note = f" (including the {currency.symbol}{fee:,.2f} fee)" if fee > 0 else ""
            return "error", f"Insufficient wallet balance. You need {currency.symbol}{total:,.2f}{note}."

        reference = generate_reference()
        # Sandbox only: e.g. FLW_SANDBOX_REFERENCE_SUFFIX = "_PMCKDU_1" makes test transfers succeed.
        reference += getattr(settings, "FLW_SANDBOX_REFERENCE_SUFFIX", "")

        balance_before = locked_wallet.balance
        locked_wallet.balance -= total
        locked_wallet.save(update_fields=["balance", "updated_at"])

        txn = PheralTransaction.objects.create(
            reference=reference,
            sender=user, sender_wallet=locked_wallet,
            transaction_type=PheralTransaction.TransactionType.WITHDRAWAL,
            amount=amount, fee=fee, currency=currency,
            status=PheralTransaction.Status.PENDING,
            description=description,
            metadata={
                "bank_name": bank_account.bank_name,
                "bank_code": bank_account.bank_code,
                "account_last4": bank_account.account_number[-4:],
            },
        )

        LedgerEntry.objects.create(
            transaction=txn, wallet=locked_wallet,
            entry_type=LedgerEntry.EntryType.DEBIT, amount=total,
            balance_before=balance_before, balance_after=locked_wallet.balance,
            description="Bank transfer (pending)",
        )

    # ---- Money is now held. Talk to Flutterwave OUTSIDE the DB lock. ----
    try:
        response = requests.post(
            f"{FLW_API}/transfers",
            json={
                "account_bank": bank_account.bank_code,
                "account_number": bank_account.account_number,
                "amount": float(amount),
                "currency": currency.code.upper(),
                "narration": "Pheral wallet transfer",
                "reference": txn.reference,
                "beneficiary_name": bank_account.account_name,
            },
            headers=_flw_headers(),
            proxies=flw_proxies(),
            timeout=20,
        )
    except requests.RequestException:
        logger.exception("Flutterwave transfer request failed (outcome unknown): %s", txn.reference)
        return "warning", PENDING_TRANSFER_MESSAGE

    if response.status_code >= 500:
        logger.error("Flutterwave transfer HTTP %s (outcome unknown): %s", response.status_code, txn.reference)
        return "warning", PENDING_TRANSFER_MESSAGE

    try:
        data = response.json()
    except ValueError:
        logger.error("Flutterwave transfer returned non-JSON (outcome unknown): %s", txn.reference)
        return "warning", PENDING_TRANSFER_MESSAGE

    logger.info(
        "Flutterwave transfer ref=%s http=%s status=%s message=%s",
        txn.reference, response.status_code, data.get("status"), data.get("message"),
    )

    # ---- Definite "no": refund now. ----
    if data.get("status") != "success":
        provider_message = data.get("message") or "no reason given"
        logger.warning("Flutterwave REJECTED transfer %s: %s", txn.reference, provider_message)
        settle_withdrawal(txn.reference, "FAILED")

        message = "Transfer could not be started. Your balance has been refunded."
        if settings.DEBUG:
            # Common causes: server IP not whitelisted, low Flutterwave balance, wrong bank code.
            message += f" (Flutterwave said: {provider_message})"
        return "error", message

    # ---- Accepted / queued. Keep the transfer id; the webhook or sync settles it. ----
    transfer_data = data.get("data") or {}

    PheralTransaction.objects.filter(
        pk=txn.pk, status=PheralTransaction.Status.PENDING,
    ).update(external_reference=str(transfer_data.get("id") or ""))

    settle_withdrawal(txn.reference, transfer_data.get("status"), transfer_data.get("id"))

    return "success", "Transfer initiated. It may take a few minutes."


def _bank_page_context(user):
    currency = get_default_currency()
    return {
        "accounts": BankAccount.objects.filter(user=user, is_active=True).order_by("-created_at"),
        "fee": get_withdrawal_fee(),
        "wallet": get_or_create_wallet(user, currency) if currency else None,
        "currency": currency,
        "banks": get_nigerian_banks() if settings.FLW_SECRET_KEY else [],
    }


def _bank_transfer_preconditions(request, context):
    """Shared checks. Returns a redirect response when the request can't proceed, else None."""
    if not context["currency"] or not context["wallet"]:
        messages.error(request, "No withdrawal currency is configured.")
        return redirect("wallet")

    if not settings.FLW_SECRET_KEY:
        messages.error(request, "Bank transfers are not configured yet.")
        return redirect("wallet")

    if context["currency"].code.upper() != "NGN":
        messages.error(request, "Nigerian bank transfers are currently available in NGN only.")
        return redirect("wallet")

    return None


@login_required
def withdraw(request):
    context = _bank_page_context(request.user)

    if request.method != "POST":
        return render(request, "withdraw.html", context)

    blocked = _bank_transfer_preconditions(request, context)
    if blocked:
        return blocked

    amount = parse_amount(request.POST.get("amount"))
    if amount is None:
        messages.error(request, "Enter a valid withdrawal amount.")
        return render(request, "withdraw.html", context)

    bank_account = context["accounts"].filter(pk=request.POST.get("bank_account")).first()
    if not bank_account:
        messages.error(request, "Select a valid bank account.")
        return render(request, "withdraw.html", context)

    level, message = initiate_bank_transfer(
        request.user, bank_account, amount, context["currency"],
        description=f"Withdrawal to {bank_account.bank_name}",
    )
    getattr(messages, level)(request, message)

    if level == "error":
        return render(request, "withdraw.html", _bank_page_context(request.user))

    return redirect("wallet")


@login_required
def transfer(request):
    """One screen, two destinations: another Pheral user (instant) or a bank account (async)."""
    context = _bank_page_context(request.user)

    if request.method != "POST":
        return render(request, "transfer.html", context)

    destination = request.POST.get("destination", "").strip()  # "pheral" | "bank"
    amount = parse_amount(request.POST.get("amount"))
    currency = context["currency"]

    if amount is None:
        messages.error(request, "Enter a valid amount.")
        return render(request, "transfer.html", context)

    if not currency or not context["wallet"]:
        messages.error(request, "No wallet currency is configured.")
        return redirect("wallet")

    # ---------------- Pheral user ----------------
    if destination == "pheral":
        username = request.POST.get("recipient_username", "").strip().lstrip("@")
        description = request.POST.get("description", "").strip()[:255]
        recipient = User.objects.filter(username__iexact=username, is_active=True).first()

        if not recipient:
            messages.error(request, "That Pheral user could not be found.")
            return render(request, "transfer.html", context)

        if recipient == request.user:
            messages.error(request, "You cannot send money to yourself.")
            return render(request, "transfer.html", context)

        try:
            _, conversation = send_wallet_payment(request.user, recipient, currency, amount, description)
        except InsufficientBalance:
            messages.error(request, "Insufficient wallet balance.")
            return render(request, "transfer.html", context)

        messages.success(request, f"{currency.symbol}{amount:,.2f} sent to @{recipient.username}.")
        return redirect("chat", conversation_id=conversation.pk)

    # ---------------- Bank account ----------------
    if destination == "bank":
        blocked = _bank_transfer_preconditions(request, context)
        if blocked:
            return blocked

        # A saved account (radio button) wins if one was picked. Otherwise, if the
        # person typed a fresh account number + picked a bank, resolve and save
        # that instead — so a first-time recipient never needs a separate trip to
        # "Add bank account" first. The account name is always re-verified with
        # Flutterwave here, never trusted from the form.
        bank_account = context["accounts"].filter(pk=request.POST.get("bank_account")).first()

        if not bank_account:
            new_account_number = (request.POST.get("new_account_number") or "").strip()
            new_bank_code = (request.POST.get("new_bank_code") or "").strip()
            new_bank_name = (request.POST.get("new_bank_name") or "").strip()

            if new_account_number and new_bank_code:
                bank_account, error = get_or_create_verified_bank_account(
                    request.user, new_account_number, new_bank_code, new_bank_name,
                )
                if error:
                    messages.error(request, error)
                    return render(request, "transfer.html", context)

        if not bank_account:
            messages.error(request, "Select a saved account, or enter an account number and bank.")
            return render(request, "transfer.html", context)

        level, message = initiate_bank_transfer(
            request.user, bank_account, amount, currency,
            description=f"Transfer to {bank_account.bank_name}",
        )
        getattr(messages, level)(request, message)

        if level == "error":
            return render(request, "transfer.html", _bank_page_context(request.user))

        return redirect("wallet")

    messages.error(request, "Select where you'd like to send money.")
    return render(request, "transfer.html", context)


def _fetch_transfer(txn):
    """Look the transfer up on Flutterwave. Dict or None; may raise requests/ValueError errors."""
    if txn.external_reference:
        response = requests.get(
            f"{FLW_API}/transfers/{txn.external_reference}", headers=_flw_headers(), timeout=15,
        )
        data = response.json().get("data")
        if isinstance(data, dict):
            return data

    response = requests.get(
        f"{FLW_API}/transfers", params={"reference": txn.reference}, headers=_flw_headers(), timeout=15,
    )
    data = response.json().get("data")

    if isinstance(data, list):
        return next((t for t in data if t.get("reference") == txn.reference), None)
    return data if isinstance(data, dict) else None


@login_required
@require_POST
def sync_withdrawal_status(request, reference):
    """Polled from the wallet page while a transfer is pending."""
    txn = PheralTransaction.objects.filter(
        sender=request.user, reference=reference,
        transaction_type=PheralTransaction.TransactionType.WITHDRAWAL,
    ).first()

    if not txn:
        return JsonResponse({"status": "error", "message": "Transfer not found."}, status=404)

    if txn.status != PheralTransaction.Status.PENDING:
        return JsonResponse({"status": "success", "transaction_status": _status_label(txn.status)})

    if not settings.FLW_SECRET_KEY:
        return JsonResponse({"status": "error", "message": "Flutterwave is not configured."}, status=500)

    try:
        flw_transfer = _fetch_transfer(txn)
    except (requests.RequestException, ValueError):
        logger.exception("Could not sync transfer %s", reference)
        return JsonResponse({"status": "error", "message": "Could not contact Flutterwave."}, status=502)

    # Not visible on Flutterwave (yet): never refund on absence.
    if not flw_transfer:
        return JsonResponse({"status": "success", "transaction_status": "pending"})

    result = settle_withdrawal(reference, flw_transfer.get("status"), flw_transfer.get("id"))

    return JsonResponse({
        "status": "success",
        "transaction_status": result or "pending",
        "flutterwave_status": (flw_transfer.get("status") or "").upper(),
    })


# ============================================================
# FLUTTERWAVE WEBHOOK
# Dashboard: Settings > Webhooks. Set the URL to /flutterwave/webhook/ and a
# "secret hash"; put the same value in settings.FLW_WEBHOOK_HASH.
# ============================================================

def _valid_webhook_signature(request):
    secret = getattr(settings, "FLW_WEBHOOK_HASH", "")
    if not secret:
        logger.error("FLW_WEBHOOK_HASH is not set; rejecting webhook")
        return False

    # Legacy: header carries the secret hash itself.
    legacy = request.headers.get("verif-hash")
    if legacy and hmac.compare_digest(legacy, secret):
        return True

    # Newer: base64(HMAC-SHA256(body, secret hash)).
    signature = request.headers.get("flutterwave-signature")
    if signature:
        digest = base64.b64encode(
            hmac.new(secret.encode(), request.body, hashlib.sha256).digest()
        ).decode()
        return hmac.compare_digest(signature, digest)

    return False


def _credit_virtual_account_deposit(data):
    """
    Money sent to a user's permanent virtual account arrives as a charge.completed
    event with a tx_ref that is not one of our top-up references. We re-verify with
    Flutterwave, match the account, and credit exactly once (keyed on Flutterwave's
    transaction id). Test in the Flutterwave sandbox before relying on it.
    """
    flw_id = data.get("id")
    if not flw_id or data.get("status") != "successful":
        return False

    try:
        response = requests.get(
            f"{FLW_API}/transactions/{flw_id}/verify", headers=_flw_headers(), timeout=15,
        )
        body = response.json()
    except (requests.RequestException, ValueError):
        return False

    verified = body.get("data") or {}
    if body.get("status") != "success" or verified.get("status") != "successful":
        return False

    tx_ref = verified.get("tx_ref") or ""
    account = (
        VirtualAccount.objects.select_related("wallet__currency")
        .filter(is_active=True)
        .filter(Q(flw_reference=tx_ref) | Q(order_ref=tx_ref))
        .first()
    ) if tx_ref else None

    if not account:
        logger.warning("Virtual account deposit %s matches no account (tx_ref=%s)", flw_id, tx_ref)
        return False

    try:
        amount = Decimal(str(verified.get("amount", "0"))).quantize(Decimal("0.01"))
    except InvalidOperation:
        return False

    wallet_currency = account.wallet.currency
    if amount <= 0 or verified.get("currency") != wallet_currency.code:
        logger.warning("Virtual account deposit %s rejected: %s %s", flw_id, amount, verified.get("currency"))
        return False

    with transaction.atomic():
        locked_wallet = Wallet.objects.select_for_update().get(pk=account.wallet_id)

        # The wallet lock serialises concurrent deliveries, so this check is race-free.
        if PheralTransaction.objects.filter(
            transaction_type=PheralTransaction.TransactionType.TOP_UP,
            external_reference=f"flw:{flw_id}",
        ).exists():
            return True

        balance_before = locked_wallet.balance
        locked_wallet.balance += amount
        locked_wallet.save(update_fields=["balance", "updated_at"])

        txn = PheralTransaction.objects.create(
            sender=account.user, sender_wallet=locked_wallet,
            transaction_type=PheralTransaction.TransactionType.TOP_UP,
            amount=amount, currency=wallet_currency,
            status=PheralTransaction.Status.COMPLETED, completed_at=timezone.now(),
            description="Deposit to virtual account", external_reference=f"flw:{flw_id}",
        )

        LedgerEntry.objects.create(
            transaction=txn, wallet=locked_wallet,
            entry_type=LedgerEntry.EntryType.CREDIT, amount=amount,
            balance_before=balance_before, balance_after=locked_wallet.balance,
            description="Virtual account deposit",
        )

        Notification.objects.create(
            user=account.user, notification_type=Notification.NotificationType.PAYMENT,
            title="Deposit received", body=f"{wallet_currency.symbol}{amount:,.2f} was added to your wallet.",
        )

    return True


@csrf_exempt
@require_POST
def flutterwave_webhook(request):
    if not _valid_webhook_signature(request):
        return HttpResponseForbidden()

    try:
        payload = json.loads(request.body)
    except ValueError:
        return HttpResponse(status=400)

    event = payload.get("event") or payload.get("type") or ""
    data = payload.get("data") or {}

    try:
        if event == "transfer.completed":
            settle_withdrawal(data.get("reference"), data.get("status"), data.get("id"))

        elif event == "charge.completed":
            txn = PheralTransaction.objects.filter(
                reference=data.get("tx_ref"),
                transaction_type=PheralTransaction.TransactionType.TOP_UP,
            ).select_related("currency").first()

            if txn:
                if data.get("status") == "successful" and data.get("id"):
                    _verify_flutterwave_transaction(txn, data["id"])
                elif data.get("status") == "failed":
                    _fail_top_up(txn)
            else:
                _credit_virtual_account_deposit(data)
    except Exception:
        # Non-2xx makes Flutterwave retry, which is what we want for a transient failure.
        logger.exception("Webhook processing failed: event=%s", event)
        return HttpResponse(status=500)

    return HttpResponse(status=200)


# ============================================================
# AIRTIME / DATA
# ============================================================

BILL_OK = "ok"            # Flutterwave accepted the payment
BILL_FAILED = "failed"    # Flutterwave answered and said no: safe to refund
BILL_UNKNOWN = "unknown"  # no usable answer: the payment may have gone through


def _local_phone_format(phone_number):
    """Flutterwave NG billers expect 08012345678, not +2348012345678."""
    digits = "".join(ch for ch in str(phone_number) if ch.isdigit())
    if digits.startswith("234") and len(digits) == 13:
        return "0" + digits[3:]
    return digits


def _bill_currency():
    """Bills are Naira-only: no silent fallback to another currency."""
    return Currency.objects.filter(code__iexact="NGN", is_active=True).first()


def _ng_bill_phone(raw):
    normalized = normalize_phone_number(raw, "NG")
    if not normalized.startswith("+234"):
        return None
    return _local_phone_format(normalized)


def _debit_wallet_for_bill(user, amount, currency, transaction_type, description, metadata):
    """Debit + PENDING txn BEFORE calling Flutterwave (same pattern as bank transfers)."""
    wallet_obj = get_or_create_wallet(user, currency)

    with transaction.atomic():
        locked_wallet = Wallet.objects.select_for_update().get(pk=wallet_obj.pk)

        if locked_wallet.balance < amount:
            return None, "Insufficient wallet balance."

        balance_before = locked_wallet.balance
        locked_wallet.balance -= amount
        locked_wallet.save(update_fields=["balance", "updated_at"])

        txn = PheralTransaction.objects.create(
            sender=user, sender_wallet=locked_wallet,
            transaction_type=transaction_type,
            amount=amount, currency=currency,
            status=PheralTransaction.Status.PENDING,
            description=description[:255], metadata=metadata,
        )

        LedgerEntry.objects.create(
            transaction=txn, wallet=locked_wallet,
            entry_type=LedgerEntry.EntryType.DEBIT, amount=amount,
            balance_before=balance_before, balance_after=locked_wallet.balance,
            description=description[:255],
        )

    return txn, None


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
            description="Purchase failed, refunded",
        )
        return locked_txn


def _complete_bill(pheral_transaction, external_reference):
    """Idempotent: only a PENDING bill is completed."""
    with transaction.atomic():
        locked_txn = PheralTransaction.objects.select_for_update().get(pk=pheral_transaction.pk)
        if locked_txn.status != PheralTransaction.Status.PENDING:
            return locked_txn

        locked_txn.status = PheralTransaction.Status.COMPLETED
        locked_txn.external_reference = external_reference
        locked_txn.completed_at = timezone.now()
        locked_txn.save(update_fields=["status", "external_reference", "completed_at"])
        return locked_txn


def _call_flutterwave_bill(*, biller_code, item_code, customer_phone, amount, reference):
    """
    Send an airtime/data payment. Returns (outcome, flw_reference).
    BILL_UNKNOWN (timeout / 5xx / junk) leaves the txn PENDING: refunding then
    could hand the customer free airtime if Flutterwave did pay.
    """
    try:
        response = requests.post(
            f"{FLW_API}/billers/{biller_code}/items/{item_code}/payment",
            json={
                "country": "NG",
                "customer_id": customer_phone,
                "amount": float(amount),
                "reference": reference,
            },
            headers=_flw_headers(), proxies=flw_proxies(), timeout=30,
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
        body = data.get("data") or {}
        return BILL_OK, body.get("flw_ref") or body.get("reference") or reference

    logger.warning("Flutterwave rejected bill (ref %s): %s", reference, data.get("message"))
    return BILL_FAILED, None


def fetch_data_plans(network):
    """
    Live data bundle plans + prices from Flutterwave, cached 5 minutes.
    Returns [{name, amount, item_code}] or [] on failure.
    Verify the response shape against the current Flutterwave docs before going live.
    """
    if not settings.FLW_SECRET_KEY or not network.flutterwave_data_biller:
        return []

    cache_key = f"flw_data_plans:{network.pk}"
    cached = cache.get(cache_key)
    if cached is not None:
        return cached

    try:
        response = requests.get(
            f"{FLW_API}/bill-categories",
            params={"country": "NG", "biller_name": network.flutterwave_data_biller},
            headers=_flw_headers(), timeout=20,
        )
        data = response.json()
    except (requests.RequestException, ValueError):
        return []

    if data.get("status") != "success":
        return []

    plans = [
        {
            "name": item.get("name") or item.get("biller_name", "Data plan"),
            "amount": item.get("amount"),
            "item_code": item.get("item_code") or item.get("biller_code"),
        }
        for item in data.get("data", [])
    ]
    plans = [p for p in plans if p["amount"] and p["item_code"]]

    if plans:  # never cache a failed lookup
        cache.set(cache_key, plans, 300)

    return plans


def _finish_bill(request, txn, outcome, flw_reference, success_message):
    if outcome == BILL_OK:
        _complete_bill(txn, flw_reference)
        messages.success(request, success_message)
    elif outcome == BILL_FAILED:
        _refund_failed_bill(txn)
        messages.error(request, "Purchase failed. Your wallet has been refunded.")
    else:
        messages.warning(
            request,
            "We're still confirming this purchase. Your balance is on hold until it "
            "settles, so check your transactions before trying again.",
        )
    return redirect("wallet")


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

    # Needs `flutterwave_airtime_item_code` on NetworkProvider (see notes).
    item_code = getattr(network, "flutterwave_airtime_item_code", "")
    if not (network.flutterwave_airtime_biller_code and item_code):
        return fail("Airtime isn't set up for this network yet.")

    phone = _ng_bill_phone(request.POST.get("phone_number", ""))
    if not phone:
        return fail("Airtime is only available for Nigerian phone numbers.")

    amount = parse_amount(request.POST.get("amount"))
    if amount is None:
        return fail("Enter a valid amount.")

    txn, error = _debit_wallet_for_bill(
        request.user, amount, currency, PheralTransaction.TransactionType.AIRTIME,
        description=f"{network.name} airtime - {phone}",
        metadata={"network": network.code, "phone_number": phone},
    )
    if error:
        return fail(error)

    outcome, flw_reference = _call_flutterwave_bill(
        biller_code=network.flutterwave_airtime_biller_code, item_code=item_code,
        customer_phone=phone, amount=amount, reference=txn.reference,
    )

    return _finish_bill(
        request, txn, outcome, flw_reference,
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

    # SECURITY: price and name come from Flutterwave, never from the form.
    # The browser only says WHICH plan (item_code).
    item_code = request.POST.get("item_code", "").strip()
    plan = next((p for p in fetch_data_plans(network) if p["item_code"] == item_code), None)
    if not plan:
        return fail("That data plan is no longer available. Please pick another.")

    amount = parse_amount(plan["amount"])
    if amount is None:
        return fail("That data plan is unavailable right now.")

    txn, error = _debit_wallet_for_bill(
        request.user, amount, currency, PheralTransaction.TransactionType.DATA,
        description=f"{network.name} {plan['name']} - {phone}",
        metadata={"network": network.code, "phone_number": phone, "plan": plan["name"]},
    )
    if error:
        return fail(error)

    outcome, flw_reference = _call_flutterwave_bill(
        biller_code=network.flutterwave_data_biller_code, item_code=item_code,
        customer_phone=phone, amount=amount, reference=txn.reference,
    )

    return _finish_bill(request, txn, outcome, flw_reference, f"{plan['name']} sent to {phone}.")


@login_required
@require_POST
def sync_bill_status(request, reference):
    """
    Settles an airtime/data purchase left PENDING after a timeout / 5xx.
    Flutterwave is asked about OUR reference; we never refund on "not found".
    Verify the status endpoint/fields against the current Flutterwave docs.
    """
    txn = PheralTransaction.objects.filter(
        sender=request.user, reference=reference,
        transaction_type__in=[PheralTransaction.TransactionType.AIRTIME, PheralTransaction.TransactionType.DATA],
    ).first()

    if not txn:
        return JsonResponse({"status": "error", "message": "Purchase not found."}, status=404)

    if txn.status != PheralTransaction.Status.PENDING:
        return JsonResponse({"status": "success", "transaction_status": _status_label(txn.status)})

    try:
        response = requests.get(f"{FLW_API}/bills/{txn.reference}", headers=_flw_headers(), timeout=15)
        data = response.json()
    except (requests.RequestException, ValueError):
        return JsonResponse({"status": "error", "message": "Could not contact Flutterwave."}, status=502)

    body = data.get("data") if isinstance(data.get("data"), dict) else {}
    flw_status = str(body.get("status") or "").lower()

    if data.get("status") == "success" and flw_status in ("successful", "success", "completed"):
        _complete_bill(txn, body.get("flw_ref") or body.get("reference") or txn.reference)
    elif data.get("status") == "success" and flw_status in ("failed", "reversed"):
        _refund_failed_bill(txn)
    # anything else (including "not found"): stay pending

    txn.refresh_from_db()
    return JsonResponse({"status": "success", "transaction_status": _status_label(txn.status)})


@login_required
def data_plans_api(request, network_id):
    network = get_object_or_404(NetworkProvider, pk=network_id, is_active=True)
    return JsonResponse({"plans": fetch_data_plans(network)})


# ============================================================
# VIRTUAL CARDS / VIRTUAL ACCOUNT
# ============================================================

@login_required
def cards_and_accounts(request):
    currency = get_default_currency()
    usd_currency = Currency.objects.filter(code="USD", is_active=True).first()

    return render(request, "cards_and_accounts.html", {
        "wallet": get_or_create_wallet(request.user, currency) if currency else None,
        "currency": currency,
        "account": VirtualAccount.objects.filter(user=request.user).first(),
        "cards": VirtualCard.objects.filter(user=request.user).order_by("-created_at"),
        "usd_currency": usd_currency,
        "usd_wallet": get_or_create_wallet(request.user, usd_currency) if usd_currency else None,
    })


@login_required
@require_POST
def create_virtual_card(request):
    """
    Issue a USD card funded from the USD wallet. The wallet is debited only after
    Flutterwave confirms creation (single synchronous call, no webhook to fall back on).
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
                f"{FLW_API}/virtual-cards",
                json={
                    "currency": "USD",
                    "amount": float(amount),
                    "billing_name": request.user.get_full_name() or request.user.username,
                },
                headers=_flw_headers(), timeout=20,
            )
            data = response.json()
        except (requests.RequestException, ValueError):
            return JsonResponse({"success": False, "error": "Could not reach the card issuer. Please try again."}, status=502)

        if data.get("status") != "success":
            return JsonResponse({"success": False, "error": data.get("message", "Could not create card.")})

        card_data = data.get("data") or {}
        expiration = str(card_data.get("expiration", ""))

        balance_before = locked_wallet.balance
        locked_wallet.balance -= amount
        locked_wallet.save(update_fields=["balance", "updated_at"])

        card = VirtualCard.objects.create(
            user=request.user, currency=usd_currency,
            flw_card_id=str(card_data.get("id", "")),
            masked_pan=card_data.get("masked_pan", ""),
            expiry_month=expiration.split("/")[0] if expiration else "",
            expiry_year=expiration.split("/")[-1] if expiration else "",
            card_name=card_data.get("name_on_card", ""),
            balance=amount,
        )

        txn = PheralTransaction.objects.create(
            sender=request.user, sender_wallet=locked_wallet,
            transaction_type=PheralTransaction.TransactionType.CARD_FUNDING,
            amount=amount, currency=usd_currency, status=PheralTransaction.Status.COMPLETED,
            completed_at=timezone.now(), description="Virtual card funding",
        )

        LedgerEntry.objects.create(
            transaction=txn, wallet=locked_wallet,
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
            f"{FLW_API}/virtual-cards/{card.flw_card_id}/status/{flw_action}",
            headers=_flw_headers(), timeout=15,
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
    card = get_object_or_404(
        VirtualCard.objects.select_related("currency"),
        pk=card_id, user=request.user, status=VirtualCard.Status.ACTIVE,
    )
    amount = parse_amount(request.POST.get("amount"))

    if amount is None:
        return JsonResponse({"success": False, "error": "Enter a valid amount."}, status=400)

    wallet_obj = get_or_create_wallet(request.user, card.currency)

    with transaction.atomic():
        locked_wallet = Wallet.objects.select_for_update().get(pk=wallet_obj.pk)
        locked_card = VirtualCard.objects.select_for_update().get(pk=card.pk)

        if locked_wallet.balance < amount:
            return JsonResponse({"success": False, "error": "Insufficient balance."})

        try:
            response = requests.post(
                f"{FLW_API}/virtual-cards/{card.flw_card_id}/fund",
                json={"amount": float(amount), "debit_currency": card.currency.code},
                headers=_flw_headers(), timeout=20,
            )
            data = response.json()
        except (requests.RequestException, ValueError):
            return JsonResponse({"success": False, "error": "Could not reach the card issuer."}, status=502)

        if data.get("status") != "success":
            return JsonResponse({"success": False, "error": data.get("message", "Funding failed.")})

        balance_before = locked_wallet.balance
        locked_wallet.balance -= amount
        locked_wallet.save(update_fields=["balance", "updated_at"])

        locked_card.balance += amount
        locked_card.save(update_fields=["balance"])

        txn = PheralTransaction.objects.create(
            sender=request.user, sender_wallet=locked_wallet,
            transaction_type=PheralTransaction.TransactionType.CARD_FUNDING,
            amount=amount, currency=card.currency, status=PheralTransaction.Status.COMPLETED,
            completed_at=timezone.now(), description=f"Card funding - {card.masked_pan}",
        )

        LedgerEntry.objects.create(
            transaction=txn, wallet=locked_wallet,
            entry_type=LedgerEntry.EntryType.DEBIT, amount=amount,
            balance_before=balance_before, balance_after=locked_wallet.balance,
            description="Card funding",
        )

    return JsonResponse({
        "success": True,
        "wallet_balance": float(locked_wallet.balance),
        "card_balance": float(locked_card.balance),
    })


@login_required
@require_POST
def reveal_card_details(request, card_id):
    """Full PAN/CVV fetched fresh from Flutterwave each time. Never cached, stored or logged."""
    card = get_object_or_404(VirtualCard, pk=card_id, user=request.user)

    try:
        response = requests.get(
            f"{FLW_API}/virtual-cards/{card.flw_card_id}", headers=_flw_headers(), timeout=15,
        )
        data = response.json()
    except (requests.RequestException, ValueError):
        return JsonResponse({"success": False, "error": "Could not reach the card issuer."}, status=502)

    if data.get("status") != "success":
        return JsonResponse({"success": False, "error": "Could not retrieve card details."})

    card_data = data.get("data") or {}
    return JsonResponse({
        "success": True,
        "card_pan": card_data.get("card_pan", ""),
        "cvv": card_data.get("cvv", ""),
        "expiration": card_data.get("expiration", ""),
    })


@login_required
@require_POST
def create_virtual_account(request):
    """Permanent virtual account via Flutterwave. Nigerian accounts legally require a BVN."""
    bvn = request.POST.get("bvn", "").strip()
    currency = get_default_currency()

    if not currency:
        return JsonResponse({"success": False, "error": "No wallet currency is configured."}, status=400)

    if VirtualAccount.objects.filter(user=request.user, is_active=True).exists():
        return JsonResponse({"success": False, "error": "You already have a virtual account."}, status=400)

    if currency.code == "NGN" and not (bvn.isdigit() and len(bvn) == 11):
        return JsonResponse({"success": False, "error": "A valid 11-digit BVN is required to create a Nigerian bank account."}, status=400)

    if not settings.FLW_SECRET_KEY:
        return JsonResponse({"success": False, "error": "Not configured yet."}, status=400)

    wallet_obj = get_or_create_wallet(request.user, currency)
    tx_ref = generate_reference(prefix="VA")

    try:
        response = requests.post(
            f"{FLW_API}/virtual-account-numbers",
            json={
                "email": request.user.email or f"{request.user.username}@pheral.app",
                "is_permanent": True,
                "bvn": bvn,
                "tx_ref": tx_ref,
                "phonenumber": request.user.phone_number,
                "firstname": request.user.first_name,
                "lastname": request.user.last_name,
                "narration": f"Pheral - {request.user.username}",
            },
            headers=_flw_headers(), timeout=20,
        )
        data = response.json()
    except (requests.RequestException, ValueError):
        return JsonResponse({"success": False, "error": "Could not reach the payment network."}, status=502)

    if data.get("status") != "success":
        return JsonResponse({"success": False, "error": data.get("message", "Could not create account.")})

    va_data = data.get("data") or {}
    account_number = va_data.get("account_number", "")

    # Flutterwave's TEST mode hands back the same demo account number to every
    # merchant, regardless of who asks. account_number is (correctly) globally
    # unique on this model, since in LIVE mode two real people must never share
    # a real bank account number — so this collision only ever happens in
    # sandbox, and only lets one Pheral account hold the shared test number at
    # a time. Explain that plainly instead of letting the IntegrityError 500.
    if VirtualAccount.objects.filter(account_number=account_number).exclude(user=request.user).exists():
        return JsonResponse({
            "success": False,
            "error": (
                "Flutterwave's test mode reuses one demo account number for every "
                "merchant, so only one Pheral account can hold it at a time in "
                "sandbox. This is a test-mode-only limitation and won't happen "
                "with a live Flutterwave key."
            ),
        }, status=409)

    try:
        account = VirtualAccount.objects.create(
            user=request.user, wallet=wallet_obj,
            account_number=account_number,
            bank_name=va_data.get("bank_name", ""),
            account_name=va_data.get("account_name") or f"{request.user.first_name} {request.user.last_name}".strip(),
            flw_reference=va_data.get("flw_ref", tx_ref),
            order_ref=tx_ref,  # our tx_ref: deposits arrive tagged with it, see _credit_virtual_account_deposit
        )
    except IntegrityError:
        # Someone else's request for the same shared sandbox number landed in
        # the gap between the check above and this insert. Same sandbox-only
        # cause, same message — not a second bug.
        return JsonResponse({
            "success": False,
            "error": (
                "Flutterwave's test mode reuses one demo account number for every "
                "merchant, so only one Pheral account can hold it at a time in "
                "sandbox. This is a test-mode-only limitation and won't happen "
                "with a live Flutterwave key."
            ),
        }, status=409)

    return JsonResponse({
        "success": True,
        "account_number": account.account_number,
        "bank_name": account.bank_name,
        "account_name": account.account_name,
    })


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

    groups = {}
    for item in active_statuses:
        groups.setdefault(item.user_id, []).append(item)

    status_groups_list = [
        {
            "user": user_statuses[0].user,
            "latest": user_statuses[0],
            "statuses": user_statuses,
            "count": len(user_statuses),
            "is_owner": user_id == request.user.id,
        }
        for user_id, user_statuses in groups.items()
    ]

    return render(request, "status_list.html", {
        "my_status": next((g for g in status_groups_list if g["is_owner"]), None),
        "other_statuses": [g for g in status_groups_list if not g["is_owner"]],
    })


@login_required
def status_detail(request, status_id):
    now = timezone.now()

    status_obj = get_object_or_404(
        Status.objects.select_related("user"), id=status_id, is_active=True, expires_at__gt=now,
    )

    is_owner = status_obj.user_id == request.user.id

    if not is_owner:
        StatusView.objects.get_or_create(status=status_obj, viewer=request.user)

    user_statuses = list(
        Status.objects.filter(user=status_obj.user, is_active=True, expires_at__gt=now).order_by("created_at")
    )

    current_index = next((i for i, s in enumerate(user_statuses) if s.id == status_obj.id), 0)
    previous_status = user_statuses[current_index - 1] if current_index > 0 else None
    next_status = user_statuses[current_index + 1] if current_index < len(user_statuses) - 1 else None

    other_statuses = (
        Status.objects.filter(is_active=True, expires_at__gt=now)
        .exclude(user=status_obj.user).select_related("user").order_by("user_id", "created_at")
    )

    seen_users = set()
    other_users = []
    for item in other_statuses:
        if item.user_id not in seen_users:
            seen_users.add(item.user_id)
            other_users.append(item)

    viewers = (
        StatusView.objects.filter(status=status_obj).select_related("viewer").order_by("-viewed_at")
        if is_owner else []
    )

    return render(request, "status_detail.html", {
        "status": status_obj,
        "user_statuses": user_statuses,
        "current_index": current_index,
        "previous_status": previous_status,
        "next_status": next_status,
        "next_user_status": other_users[0] if other_users else None,
        "is_owner": is_owner,
        "view_count": StatusView.objects.filter(status=status_obj).count(),
        "viewers": viewers,
    })


@login_required
@require_POST
def delete_status(request, status_id):
    get_object_or_404(Status, id=status_id, user=request.user).delete()
    messages.success(request, "Status deleted.")
    return redirect("status_list")


@login_required
def create_status(request):
    if request.method != "POST":
        return render(request, "create_status.html")

    text = request.POST.get("text", "").strip()
    media = request.FILES.get("media")

    status_type = Status.StatusType.TEXT
    if media:
        content_type = media.content_type or ""
        if content_type.startswith("image/"):
            status_type = Status.StatusType.IMAGE
        elif content_type.startswith("video/"):
            status_type = Status.StatusType.VIDEO

    if not text and not media:
        messages.error(request, "A status needs some text or media.")
        return render(request, "create_status.html")

    Status.objects.create(
        user=request.user, status_type=status_type, text=text, media=media,
        expires_at=timezone.now() + timedelta(hours=24),
    )
    return redirect("status_list")


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
            is_liked=Exists(PostLike.objects.filter(post=OuterRef("pk"), user=request.user)),
            likes_count=Count("likes", distinct=True),
            comments_count=Count("comments", distinct=True),
        )
        .order_by("-created_at")
    )
    return render(request, "feed.html", {"posts": posts})


@login_required
def create_post(request):
    if request.method != "POST":
        return render(request, "create_post.html")

    content = request.POST.get("content", "").strip()
    media = request.FILES.get("media")

    if not content and not media:
        messages.error(request, "Post cannot be empty.")
        return redirect("feed")

    Post.objects.create(author=request.user, content=content, media=media)
    return redirect("feed")


@login_required
def like_post(request, post_id):
    post = get_object_or_404(Post, pk=post_id, is_deleted=False)

    _, created = PostLike.objects.get_or_create(post=post, user=request.user)
    if not created:
        PostLike.objects.filter(post=post, user=request.user).delete()

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
#
# A HireRequest connects an "offer" between a job's employer and a worker.
# Either side can start it:
#   - the employer searches for someone and sends a request to them
#     (requester == employer, worker == the person being offered the job), or
#   - a worker applies to an open job themselves
#     (requester == worker == the applicant).
# Whichever side did NOT start it is the one who has to accept or decline —
# see _hire_responder(). That one rule covers both directions without any
# extra field on the model.
#
# Once a request is accepted, every other still-pending request on that job
# is auto-declined and the job moves to IN_PROGRESS — this is a one-hire-per-
# job model, matching HirePayment's one-to-one link to a HireRequest. Payment
# happens explicitly via complete_hire_request(), which is the only place
# money actually moves for a job.
# ============================================================

HIRE_JOBS_PER_PAGE = 24


def _hire_responder(hire_request):
    """Whoever did NOT send this request is the one who must accept/decline it."""
    if hire_request.requester_id == hire_request.worker_id:
        return hire_request.job.employer
    return hire_request.worker


@login_required
def hire(request):
    jobs_qs = (
        HireJob.objects.filter(status=HireJob.Status.OPEN)
        .select_related("employer", "currency").order_by("-created_at")
    )

    paginator = Paginator(jobs_qs, HIRE_JOBS_PER_PAGE)
    page_obj = paginator.get_page(request.GET.get("page"))

    open_count = jobs_qs.count()
    recent_count = jobs_qs.filter(created_at__gte=timezone.now() - timedelta(days=7)).count()

    return render(request, "hire.html", {
        "jobs": page_obj.object_list,
        "page_obj": page_obj,
        "open_count": open_count,
        "recent_count": recent_count,
    })


@login_required
def create_hire_job(request):
    currencies = Currency.objects.filter(is_active=True).order_by("code")

    if request.method != "POST":
        return render(request, "create_hire_job.html", {"currencies": currencies})

    title = request.POST.get("title", "").strip()
    description = request.POST.get("description", "").strip()
    amount = parse_amount(request.POST.get("budget"))
    currency = Currency.objects.filter(pk=request.POST.get("currency"), is_active=True).first()

    def fail(message):
        messages.error(request, message)
        return render(request, "create_hire_job.html", {"currencies": currencies})

    if not title:
        return fail("Job title is required.")
    if not amount:
        return fail("Enter a valid budget.")
    if not currency:
        return fail("Select a valid currency.")

    job = HireJob.objects.create(
        employer=request.user, title=title, description=description, budget=amount, currency=currency,
    )
    return redirect("hire_job_detail", job_id=job.pk)


@login_required
def hire_job_detail(request, job_id):
    job = get_object_or_404(HireJob.objects.select_related("employer", "currency"), pk=job_id)
    is_employer = request.user == job.employer

    hire_requests = list(
        job.requests.select_related("requester", "worker")
        .annotate(is_paid=Exists(HirePayment.objects.filter(hire_request=OuterRef("pk"))))
        .order_by("-created_at")
    )

    # Attach per-request, view-only flags so the template never has to
    # re-derive the responder rule or re-check permissions itself.
    my_request = None
    for hr in hire_requests:
        hr.responder = _hire_responder(hr)
        hr.can_respond = (request.user == hr.responder and hr.status == HireRequest.Status.PENDING)
        hr.can_cancel = (request.user == hr.requester and hr.status == HireRequest.Status.PENDING)
        hr.can_pay = (is_employer and hr.status == HireRequest.Status.ACCEPTED and not hr.is_paid)
        hr.is_mine = request.user in (hr.requester, hr.worker)
        if not is_employer and hr.requester_id == hr.worker_id and hr.requester == request.user:
            my_request = hr

    can_apply = (
        not is_employer
        and job.status == HireJob.Status.OPEN
        and my_request is None
    )

    return render(request, "hire_job_detail.html", {
        "job": job,
        "hire_requests": hire_requests,
        "is_employer": is_employer,
        "my_request": my_request,
        "can_apply": can_apply,
        "can_close": is_employer and job.status == HireJob.Status.OPEN,
    })


@login_required
@require_POST
def apply_to_hire_job(request, job_id):
    """A worker applies to an open job directly, instead of waiting to be found."""
    job = get_object_or_404(HireJob, pk=job_id)

    if request.user == job.employer:
        messages.error(request, "You can't apply to your own job.")
        return redirect("hire_job_detail", job_id=job.pk)

    if job.status != HireJob.Status.OPEN:
        messages.error(request, "This job is no longer open.")
        return redirect("hire_job_detail", job_id=job.pk)

    already_applied = HireRequest.objects.filter(
        job=job, requester=request.user, worker=request.user,
    ).exclude(status__in=[HireRequest.Status.DECLINED, HireRequest.Status.CANCELLED]).exists()

    if already_applied:
        messages.error(request, "You've already applied to this job.")
        return redirect("hire_job_detail", job_id=job.pk)

    message_text = request.POST.get("message", "").strip()
    proposed_amount = parse_amount(request.POST.get("proposed_amount"))

    with transaction.atomic():
        HireRequest.objects.create(
            job=job, requester=request.user, worker=request.user,
            message=message_text, proposed_amount=proposed_amount,
        )

        Notification.objects.create(
            user=job.employer, notification_type=Notification.NotificationType.HIRE,
            title="New applicant", body=f"@{request.user.username} applied to \"{job.title}\".",
            link=reverse("hire_job_detail", args=[job.pk]),
        )

    messages.success(request, "Application sent.")
    return redirect("hire_job_detail", job_id=job.pk)


@login_required
def send_hire_request(request, job_id, username):
    job = get_object_or_404(HireJob, pk=job_id, status=HireJob.Status.OPEN)
    worker = get_object_or_404(User, username__iexact=username, is_active=True)

    if request.user != job.employer:
        messages.error(request, "Only the job's employer can send a hire request.")
        return redirect("hire_job_detail", job_id=job.pk)

    if worker == request.user:
        messages.error(request, "You cannot hire yourself.")
        return redirect("hire_job_detail", job_id=job.pk)

    already_sent = HireRequest.objects.filter(job=job, worker=worker).exclude(
        status__in=[HireRequest.Status.DECLINED, HireRequest.Status.CANCELLED],
    ).exists()
    if already_sent:
        messages.info(request, f"You already have an open request with @{worker.username} for this job.")
        return redirect("hire_job_detail", job_id=job.pk)

    if request.method != "POST":
        return render(request, "send_hire_request.html", {"job": job, "worker": worker})

    message_text = request.POST.get("message", "").strip()
    proposed_amount = parse_amount(request.POST.get("proposed_amount"))

    with transaction.atomic():
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
            link=reverse("hire_job_detail", args=[job.pk]),
        )

    return redirect("chat", conversation_id=conversation.pk)


@login_required
@require_POST
def respond_hire_request(request, request_id, action):
    """Accept or decline a pending request. Only _hire_responder() may do this."""
    if action not in ("accept", "decline"):
        raise Http404

    hire_request = get_object_or_404(
        HireRequest.objects.select_related("job", "job__employer", "requester", "worker"), pk=request_id,
    )

    if request.user != _hire_responder(hire_request):
        messages.error(request, "You can't respond to this request.")
        return redirect("hire_job_detail", job_id=hire_request.job_id)

    if hire_request.status != HireRequest.Status.PENDING:
        messages.info(request, "This request has already been responded to.")
        return redirect("hire_job_detail", job_id=hire_request.job_id)

    job = hire_request.job
    other_party = hire_request.requester if request.user == hire_request.worker else hire_request.worker

    with transaction.atomic():
        if action == "accept":
            hire_request.status = HireRequest.Status.ACCEPTED
            hire_request.save(update_fields=["status", "updated_at"])

            if job.status == HireJob.Status.OPEN:
                job.status = HireJob.Status.IN_PROGRESS
                job.save(update_fields=["status", "updated_at"])

            # One hire per job: everything else still waiting on an answer is moot now.
            other_pending = HireRequest.objects.filter(
                job=job, status=HireRequest.Status.PENDING,
            ).exclude(pk=hire_request.pk)
            for pending in other_pending.select_related("requester", "worker"):
                pending.status = HireRequest.Status.DECLINED
                pending.save(update_fields=["status", "updated_at"])
                declined_other = pending.requester if pending.requester != pending.worker else pending.worker
                Notification.objects.create(
                    user=declined_other, notification_type=Notification.NotificationType.HIRE,
                    title="Hire request closed", body=f"\"{job.title}\" has been filled.",
                    link=reverse("hire_job_detail", args=[job.pk]),
                )

            Notification.objects.create(
                user=other_party, notification_type=Notification.NotificationType.HIRE,
                title="Hire request accepted", body=f"@{request.user.username} accepted \"{job.title}\".",
                link=reverse("hire_job_detail", args=[job.pk]),
            )
            messages.success(request, "Accepted. The job is now in progress.")

        else:
            hire_request.status = HireRequest.Status.DECLINED
            hire_request.save(update_fields=["status", "updated_at"])

            Notification.objects.create(
                user=other_party, notification_type=Notification.NotificationType.HIRE,
                title="Hire request declined", body=f"@{request.user.username} declined \"{job.title}\".",
                link=reverse("hire_job_detail", args=[job.pk]),
            )
            messages.success(request, "Declined.")

    return redirect("hire_job_detail", job_id=job.pk)


@login_required
@require_POST
def cancel_hire_request(request, request_id):
    """The side that sent a request can retract it while it's still pending."""
    hire_request = get_object_or_404(HireRequest.objects.select_related("job"), pk=request_id)

    if request.user != hire_request.requester:
        messages.error(request, "You can't cancel this request.")
        return redirect("hire_job_detail", job_id=hire_request.job_id)

    if hire_request.status != HireRequest.Status.PENDING:
        messages.info(request, "This request can no longer be cancelled.")
        return redirect("hire_job_detail", job_id=hire_request.job_id)

    hire_request.status = HireRequest.Status.CANCELLED
    hire_request.save(update_fields=["status", "updated_at"])
    messages.success(request, "Request cancelled.")
    return redirect("hire_job_detail", job_id=hire_request.job_id)


@login_required
@require_POST
def complete_hire_request(request, request_id):
    """
    Employer pays the accepted worker and closes out the job. The only place
    money moves for a hire — uses send_wallet_payment() for the same locking,
    ledger and receipt discipline as every other payment, tagged as a
    HIRE_PAYMENT, with a HirePayment row linking it back to this request.
    """
    hire_request = get_object_or_404(
        HireRequest.objects.select_related("job", "job__employer", "job__currency", "worker"), pk=request_id,
    )
    job = hire_request.job

    if request.user != job.employer:
        messages.error(request, "Only the job's employer can release payment.")
        return redirect("hire_job_detail", job_id=job.pk)

    if hire_request.status != HireRequest.Status.ACCEPTED:
        messages.error(request, "This request isn't in a payable state.")
        return redirect("hire_job_detail", job_id=job.pk)

    if HirePayment.objects.filter(hire_request=hire_request).exists():
        messages.info(request, "This request has already been paid.")
        return redirect("hire_job_detail", job_id=job.pk)

    amount = hire_request.proposed_amount or job.budget

    try:
        with transaction.atomic():
            txn, _ = send_wallet_payment(
                job.employer, hire_request.worker, job.currency, amount,
                description=f"Hire payment: {job.title}",
                transaction_type=PheralTransaction.TransactionType.HIRE_PAYMENT,
            )
            HirePayment.objects.create(hire_request=hire_request, transaction=txn)

            hire_request.status = HireRequest.Status.COMPLETED
            hire_request.save(update_fields=["status", "updated_at"])

            job.status = HireJob.Status.COMPLETED
            job.save(update_fields=["status", "updated_at"])

            receipt_reference = Receipt.objects.filter(transaction=txn).values_list("reference", flat=True).first()
            Notification.objects.create(
                user=hire_request.worker, notification_type=Notification.NotificationType.PAYMENT,
                title="Hire payment received",
                body=f"@{job.employer.username} paid {job.currency.symbol}{amount:,.2f} for \"{job.title}\".",
                link=reverse("receipt_detail", args=[receipt_reference]) if receipt_reference else "",
            )
    except InsufficientBalance:
        messages.error(request, f"Insufficient {job.currency.code} wallet balance to pay {job.currency.symbol}{amount:,.2f}.")
        return redirect("hire_job_detail", job_id=job.pk)

    messages.success(request, f"Paid {job.currency.symbol}{amount:,.2f} and marked the job complete.")
    return redirect("hire_job_detail", job_id=job.pk)


@login_required
@require_POST
def close_hire_job(request, job_id):
    """Employer withdraws an open listing that hasn't been filled yet."""
    job = get_object_or_404(HireJob, pk=job_id, employer=request.user)

    if job.status != HireJob.Status.OPEN:
        messages.error(request, "Only an open job can be closed this way.")
        return redirect("hire_job_detail", job_id=job.pk)

    with transaction.atomic():
        job.status = HireJob.Status.CANCELLED
        job.save(update_fields=["status", "updated_at"])

        pending = HireRequest.objects.filter(job=job, status=HireRequest.Status.PENDING).select_related(
            "requester", "worker",
        )
        for hr in pending:
            hr.status = HireRequest.Status.CANCELLED
            hr.save(update_fields=["status", "updated_at"])
            other = hr.requester if hr.requester != hr.worker else hr.worker
            Notification.objects.create(
                user=other, notification_type=Notification.NotificationType.HIRE,
                title="Job closed", body=f"\"{job.title}\" was closed by the employer.",
            )

    messages.success(request, "Job closed.")
    return redirect("hire_job_detail", job_id=job.pk)


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
# AGENT MODE
# ============================================================

def _finish_agent_command(command, status, result):
    command.status = status
    command.result = result
    command.completed_at = timezone.now()
    command.save(update_fields=["status", "result", "completed_at"])


def _run_agent_command(command):
    """Supports:  @agent msg @username text...   and   @agent pay @username amount"""
    user = command.user
    raw = command.command.strip()
    lowered = raw.lower()

    command.status = AgentCommand.Status.PROCESSING
    command.save(update_fields=["status"])

    if lowered.startswith("@agent msg "):
        parts = raw.split(" ", 3)
        if len(parts) >= 4:
            recipient = User.objects.filter(username__iexact=parts[2].lstrip("@"), is_active=True).first()
            text = parts[3].strip()

            if recipient and recipient != user and text:
                conversation = get_or_create_direct_conversation(user, recipient)
                Message.objects.create(
                    conversation=conversation, sender=user,
                    message_type=Message.MessageType.AGENT, content=text,
                )
                AgentActivity.objects.create(
                    user=user, command=command, conversation=conversation, action="send_message",
                    details={"recipient": recipient.username, "message": text},
                )
                _finish_agent_command(command, AgentCommand.Status.COMPLETED, {
                    "action": "send_message", "recipient": recipient.username, "message": text,
                })
                return

    elif lowered.startswith("@agent pay "):
        parts = raw.split()
        if len(parts) >= 4:
            recipient = User.objects.filter(username__iexact=parts[2].lstrip("@"), is_active=True).first()
            amount = parse_amount(parts[3])
            currency = get_default_currency()

            if recipient and recipient != user and amount and currency:
                try:
                    txn, conversation = send_wallet_payment(user, recipient, currency, amount, "Agent payment")
                except InsufficientBalance:
                    _finish_agent_command(command, AgentCommand.Status.FAILED, {"error": "Insufficient wallet balance."})
                    return

                AgentActivity.objects.create(
                    user=user, command=command, conversation=conversation, transaction=txn, action="payment",
                    details={"recipient": recipient.username, "amount": str(amount), "currency": currency.code},
                )
                _finish_agent_command(command, AgentCommand.Status.COMPLETED, {
                    "action": "payment", "recipient": recipient.username,
                    "amount": str(amount), "currency": currency.code, "reference": txn.reference,
                })
                return

    _finish_agent_command(command, AgentCommand.Status.FAILED, {"error": "Command could not be understood or completed."})


@login_required
def agent(request):
    if request.method == "POST":
        command_text = request.POST.get("command", "").strip()

        if not command_text:
            messages.error(request, "Enter an agent command.")
            return redirect("agent")

        command = AgentCommand.objects.create(
            user=request.user, command=command_text, status=AgentCommand.Status.PENDING,
        )
        _run_agent_command(command)
        return redirect("agent")

    commands = AgentCommand.objects.filter(user=request.user).order_by("-created_at")[:50]
    return render(request, "agent.html", {"commands": commands})


@login_required
def agent_command(request, command_id):
    """Kept for the existing URL. Only a POST runs a pending command (a GET must never move money)."""
    command = get_object_or_404(AgentCommand, pk=command_id, user=request.user)

    if request.method == "POST" and command.status == AgentCommand.Status.PENDING:
        _run_agent_command(command)

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

    if platform not in PushDevice.Platform.values:
        platform = PushDevice.Platform.WEB

    if not token:
        return JsonResponse({"error": "Push token is required."}, status=400)

    _, created = PushDevice.objects.update_or_create(
        token=token, defaults={"user": request.user, "platform": platform, "is_active": True},
    )
    return JsonResponse({"success": True, "created": created})


def pwa_manifest(request):
    manifest = {
        "name": "Pheral",
        "short_name": "Pheral",
        "description": "Chat. Pay. Hire.",
        "start_url": "/chat_list",
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
const APP_SHELL = ["/"];

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