# Paste this into views.py as the ONLY flutterwave_webhook.
# It uses helpers that already exist in your views.py:
#   _verify_flutterwave_transaction, settle_withdrawal
# and settings you already have: FLW_SECRET_HASH
#
# hashlib, hmac, base64, json, csrf_exempt, require_POST, HttpResponse
# are already imported at the top of your views.py.


def _webhook_signature_ok(request):
    """
    Accepts Flutterwave's newer HMAC signature header, or the older
    verif-hash header (which is just the secret hash itself).
    Both are checked against the same setting: FLW_SECRET_HASH.
    """
    secret = getattr(settings, "FLW_SECRET_HASH", "")
    if not secret:
        return False

    signature = request.headers.get("flutterwave-signature", "")
    if signature:
        expected = base64.b64encode(
            hmac.new(secret.encode("utf-8"), request.body, hashlib.sha256).digest()
        ).decode("utf-8")
        return hmac.compare_digest(signature, expected)

    legacy = request.headers.get("verif-hash", "")
    return bool(legacy) and hmac.compare_digest(legacy, secret)


@csrf_exempt
@require_POST
def flutterwave_webhook(request):
    if not _webhook_signature_ok(request):
        return HttpResponse(status=401)

    try:
        event = json.loads(request.body)
    except ValueError:
        return HttpResponse(status=400)

    event_type = event.get("event")
    data = event.get("data") or {}

    # ---- Top-up ------------------------------------------------
    if event_type == "charge.completed":
        txn = PheralTransaction.objects.filter(
            reference=data.get("tx_ref"),
            transaction_type=PheralTransaction.TransactionType.TOP_UP,
        ).first()

        if txn and txn.status == PheralTransaction.Status.PENDING and data.get("id"):
            _verify_flutterwave_transaction(txn, data["id"])

    # ---- Withdrawal (success OR failure arrives on this event) --
    elif event_type == "transfer.completed":
        settle_withdrawal(
            data.get("reference"),
            data.get("status"),   # SUCCESSFUL or FAILED
            data.get("id"),
        )

    return HttpResponse(status=200)