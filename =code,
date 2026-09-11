from decimal import Decimal

from django.core.management.base import BaseCommand
from django.db import transaction

from pheral.models import Currency, ExchangeRate


# Rates are NGN per 1 unit of the foreign currency. This is a
# one-time seed, not a live feed — FX moves daily. Replace with a
# real provider on a scheduled job before relying on this for
# anything beyond development.
CURRENCIES = [
    # code, name, symbol, decimal_places, rate_to_ngn
    ("NGN", "Nigerian Naira", "₦", 2, Decimal("1")),
    ("GHS", "Ghanaian Cedi", "₵", 2, Decimal("95.00")),
    ("KES", "Kenyan Shilling", "KSh", 2, Decimal("10.80")),
    ("ZAR", "South African Rand", "R", 2, Decimal("78.00")),
    ("UGX", "Ugandan Shilling", "USh", 2, Decimal("0.38")),
    ("TZS", "Tanzanian Shilling", "TSh", 2, Decimal("0.55")),
    ("RWF", "Rwandan Franc", "FRw", 2, Decimal("0.98")),
    ("EGP", "Egyptian Pound", "E£", 2, Decimal("29.00")),
    ("USD", "US Dollar", "$", 2, Decimal("1400.00")),
    ("GBP", "British Pound", "£", 2, Decimal("1880.00")),
    ("EUR", "Euro", "€", 2, Decimal("1590.00")),
    ("CAD", "Canadian Dollar", "C$", 2, Decimal("1020.00")),
    ("AUD", "Australian Dollar", "A$", 2, Decimal("920.00")),
]


class Command(BaseCommand):
    help = (
        "Seeds Currency rows and ExchangeRate pairs against NGN "
        "(both directions) so cross-currency payments and "
        "currency_converter can resolve a rate for any seeded "
        "currency. Safe to re-run — uses update_or_create throughout."
    )

    def add_arguments(self, parser):
        parser.add_argument("--dry-run", action="store_true")

    def handle(self, *args, **options):
        dry_run = options["dry_run"]
        ngn_rate_by_code = {code: rate for code, *_r, rate in CURRENCIES}

        if dry_run:
            self.stdout.write(self.style.NOTICE(
                f"[DRY RUN] Would seed {len(CURRENCIES)} currencies and "
                f"{2 * (len(CURRENCIES) - 1)} exchange rate rows against NGN."
            ))
            return

        with transaction.atomic():
            currency_objs = {}
            for code, name, symbol, decimal_places, _rate in CURRENCIES:
                currency, _ = Currency.objects.update_or_create(
                    code=code,
                    defaults={
                        "name": name, "symbol": symbol,
                        "decimal_places": decimal_places, "is_active": True,
                    },
                )
                currency_objs[code] = currency

            ngn = currency_objs["NGN"]
            for code, currency in currency_objs.items():
                if code == "NGN":
                    continue
                rate_to_ngn = ngn_rate_by_code[code]
                rate_from_ngn = (Decimal("1") / rate_to_ngn).quantize(Decimal("0.00000001"))

                ExchangeRate.objects.update_or_create(
                    source_currency=currency, target_currency=ngn,
                    defaults={"rate": rate_to_ngn, "is_active": True},
                )
                ExchangeRate.objects.update_or_create(
                    source_currency=ngn, target_currency=currency,
                    defaults={"rate": rate_from_ngn, "is_active": True},
                )

        self.stdout.write(self.style.SUCCESS(
            f"Seeded {len(currency_objs)} currencies and their NGN exchange rate pairs."
        ))