# ============================================================
# WALLET CONVERSION (e.g. NGN wallet -> USD wallet)
# Paste into views.py, below currency_converter().
#
# 1. Imports to add/change at the top of views.py:
#       from decimal import Decimal, InvalidOperation, ROUND_DOWN
#       from django.core import signing
#
# 2. settings.py:
#       FX_SPREAD = "0.01"     # 1% kept by Pheral on each conversion
#
# 3. Admin: make sure ONE active ExchangeRate row exists between NGN and USD
#    (either direction; convert_amount() handles the inverse).
#
# 4. urls.py:
#       path("convert/", views.convert_wallet_page, name="convert_wallet"),
#       path("convert/quote/", views.fx_quote, name="fx_quote"),
#       path("convert/confirm/", views.fx_convert, name="fx_convert"),
#
# Flow: browser asks fx_quote for a price -> gets a signed, 60-second quote
# -> user confirms -> fx_convert re-verifies the signature and expiry, locks
# both wallets, moves the money once (idempotent on the quote id).
# The browser never sends a rate or a "you receive" amount that we trust.
# ============================================================
from django.contrib.auth.decorators import login_required
FX_QUOTE_TTL_SECONDS = 60
FX_QUOTE_SALT = "pheral.fx.quote"


def get_fx_spread():
    """Fraction Pheral keeps on each conversion (settings.FX_SPREAD, default 1%)."""
    return Decimal(str(getattr(settings, "FX_SPREAD", "0.01")))


def build_fx_quote(source, target, amount):
    """
    {"mid": Decimal, "receive": Decimal} or None if there is no rate.
    mid     = what the exchange rate says the user would get (no spread)
    receive = what they actually get after our spread, rounded DOWN
    """
    mid = convert_amount(amount, source, target)
    if mid is None:
        return None

    quantize_to = Decimal("1").scaleb(-target.decimal_places)
    receive = (mid * (Decimal("1") - get_fx_spread())).quantize(quantize_to, rounding=ROUND_DOWN)

    if receive < Decimal("0.01"):
        return None

    return {"mid": mid, "receive": receive}


@login_required
def convert_wallet_page(request):
    currencies = list(Currency.objects.filter(is_active=True).order_by("code"))

    return render(request, "convert.html", {
        "currencies": currencies,
        "wallet_data": wallet_snapshot(request.user, currencies),
        "quote_ttl": FX_QUOTE_TTL_SECONDS,
    })


@login_required
@require_POST
def fx_quote(request):
    """Step 1 (AJAX): price a conversion and return a signed quote that expires."""
    source = Currency.objects.filter(code__iexact=(request.POST.get("from") or "").strip(), is_active=True).first()
    target = Currency.objects.filter(code__iexact=(request.POST.get("to") or "").strip(), is_active=True).first()
    amount = parse_amount(request.POST.get("amount"))

    if not source or not target or source.pk == target.pk or amount is None:
        return JsonResponse(
            {"success": False, "error": "Choose two different currencies and enter a valid amount."},
            status=400,
        )

    quote = build_fx_quote(source, target, amount)
    if quote is None:
        return JsonResponse(
            {"success": False, "error": "No exchange rate is available for that amount or pair."},
            status=400,
        )

    token = signing.dumps(
        {
            "u": request.user.pk,
            "f": source.pk,
            "t": target.pk,
            "a": str(amount),
            "r": str(quote["receive"]),
            "m": str(quote["mid"]),
            "q": uuid.uuid4().hex,  # quote id = idempotency key
        },
        salt=FX_QUOTE_SALT,
    )

    return JsonResponse({
        "success": True,
        "quote": token,
        "expires_in": FX_QUOTE_TTL_SECONDS,
        "from": source.code,
        "to": target.code,
        "pay": str(amount),
        "receive": str(quote["receive"]),
        "rate": str((quote["receive"] / amount).quantize(Decimal("0.00000001"))),
    })


@login_required
@require_POST
def fx_convert(request):
    """Step 2 (AJAX): execute a quote. Debit source wallet, credit target wallet, once."""
    try:
        q = signing.loads(
            request.POST.get("quote", ""), salt=FX_QUOTE_SALT, max_age=FX_QUOTE_TTL_SECONDS,
        )
    except signing.SignatureExpired:
        return JsonResponse(
            {"success": False, "expired": True, "error": "That rate has expired. Please get a new one."},
            status=400,
        )
    except signing.BadSignature:
        return JsonResponse({"success": False, "error": "Invalid quote."}, status=400)

    if q.get("u") != request.user.pk:
        return JsonResponse({"success": False, "error": "Invalid quote."}, status=403)

    source = Currency.objects.filter(pk=q["f"], is_active=True).first()
    target = Currency.objects.filter(pk=q["t"], is_active=True).first()

    try:
        amount = Decimal(q["a"])
        receive = Decimal(q["r"])
        mid = Decimal(q["m"])
    except (InvalidOperation, KeyError):
        return JsonResponse({"success": False, "error": "Invalid quote."}, status=400)

    if not source or not target or source.pk == target.pk or amount <= 0 or receive <= 0:
        return JsonResponse({"success": False, "error": "This conversion is no longer available."}, status=400)

    source_wallet = get_or_create_wallet(request.user, source)
    target_wallet = get_or_create_wallet(request.user, target)
    idempotency_key = f"fx:{q['q']}"

    with transaction.atomic():
        # Lock both wallets in pk order (same rule as send_wallet_payment: no deadlocks).
        locked = {
            w.pk: w
            for w in Wallet.objects.select_for_update()
            .filter(pk__in=[source_wallet.pk, target_wallet.pk])
            .order_by("pk")
        }
        src = locked[source_wallet.pk]
        dst = locked[target_wallet.pk]

        # Checked AFTER the lock, so a double-tap or retry can't convert twice.
        existing = PheralTransaction.objects.filter(
            sender=request.user,
            transaction_type=PheralTransaction.TransactionType.FX,
            external_reference=idempotency_key,
        ).first()
        if existing:
            return JsonResponse({"success": True, "already_done": True, "reference": existing.reference})

        if not src.is_active or not dst.is_active:
            return JsonResponse({"success": False, "error": "One of your wallets is inactive."}, status=400)

        if src.balance < amount:
            return JsonResponse({"success": False, "error": "Insufficient wallet balance."}, status=400)

        src_before = src.balance
        dst_before = dst.balance

        src.balance -= amount
        src.save(update_fields=["balance", "updated_at"])
        dst.balance += receive
        dst.save(update_fields=["balance", "updated_at"])

        txn = PheralTransaction.objects.create(
            sender=request.user, recipient=request.user,
            sender_wallet=src, recipient_wallet=dst,
            transaction_type=PheralTransaction.TransactionType.FX,
            amount=amount, currency=source, fee=Decimal("0.00"),
            status=PheralTransaction.Status.COMPLETED, completed_at=timezone.now(),
            description=f"Converted {source.code} to {target.code}",
            external_reference=idempotency_key,
            metadata={
                "from": source.code,
                "to": target.code,
                "sent": str(amount),
                "received": str(receive),
                "mid_market_received": str(mid),
                "rate": str((receive / amount).quantize(Decimal("0.00000001"))),
                "spread": str(get_fx_spread()),
            },
        )

        LedgerEntry.objects.create(
            transaction=txn, wallet=src,
            entry_type=LedgerEntry.EntryType.DEBIT, amount=amount,
            balance_before=src_before, balance_after=src.balance,
            description=f"Converted to {target.code}",
        )
        LedgerEntry.objects.create(
            transaction=txn, wallet=dst,
            entry_type=LedgerEntry.EntryType.CREDIT, amount=receive,
            balance_before=dst_before, balance_after=dst.balance,
            description=f"Converted from {source.code}",
        )

        # Our margin, recorded in the currency it was earned in (the target currency).
        margin = mid - receive
        if margin > 0:
            RevenueRecord.objects.create(
                user=request.user,
                revenue_type=RevenueRecord.RevenueType.FX_MARGIN,
                amount=margin, currency=target, transaction=txn,
                description=f"FX margin {source.code}->{target.code}",
            )

    return JsonResponse({
        "success": True,
        "reference": txn.reference,
        "from": source.code,
        "to": target.code,
        "sent": str(amount),
        "received": str(receive),
        "source_balance": str(src.balance),
        "target_balance": str(dst.balance),
    })


# ------------------------------------------------------------
# Minimal front-end contract for convert.html (Alpine or plain JS)
#
#   1. POST /convert/quote/   from=NGN&to=USD&amount=50000   (+ csrf token)
#        -> { quote, pay, receive, rate, expires_in }
#      Show "You pay / You get", start a countdown from expires_in.
#
#   2. POST /convert/confirm/ quote=<the token from step 1>  (+ csrf token)
#        -> { success, received, source_balance, target_balance }
#      If the response has expired: true, request a fresh quote and show it.
#
# Generate nothing on the client except the amount and currencies.
# ------------------------------------------------------------