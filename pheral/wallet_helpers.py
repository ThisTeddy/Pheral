from .models import Wallet

from datetime import timedelta
from decimal import Decimal, InvalidOperation


def get_or_create_wallet(user, currency):
    wallet_obj, _ = Wallet.objects.get_or_create(
        user=user, currency=currency, defaults={"balance": Decimal("0.00")}
    )
    return wallet_obj



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


def wallet_snapshot(user, currencies, get_or_create_wallet):
    """Balances for every active currency, for the pay / top-up screens."""

    return [
        {
            "code": currency.code,
            "name": currency.name,
            "symbol": currency.symbol,
            "balance": float(
                get_or_create_wallet(user, currency).balance
            ),
        }
        for currency in currencies
    ]