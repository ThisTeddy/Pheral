from decimal import Decimal, ROUND_DOWN

from django.core.exceptions import ValidationError
from django.db import transaction
from django.utils import timezone

from .models import (
    AgentAction,
    AgentActionStatus,
    AgentActionType,
    Currency,
    GroupPayment,
    GroupPaymentContribution,
    GroupPaymentStatus,
    GroupWithdrawal,
    GroupWithdrawalStatus,
    LedgerEntry,
    PheralTransaction,
    Receipt,
    TransactionStatus,
    TransactionType,
    User,
    Wallet,
)


# =============================================================================
# CONSTANTS / HELPERS
# =============================================================================

ZERO = Decimal("0.00")


def money(value):
    """
    Normalize incoming monetary values to Decimal.
    """
    try:
        value = Decimal(str(value))
    except Exception:
        raise ValidationError("Invalid monetary value.")

    if value < ZERO:
        raise ValidationError("Amount cannot be negative.")

    return value


def quantize_money(value, currency):
    """
    Apply the currency's configured decimal precision.
    """
    value = money(value)

    exponent = Decimal("1").scaleb(-currency.decimal_places)

    return value.quantize(
        exponent,
        rounding=ROUND_DOWN,
    )


def get_wallet(user, currency, lock=False):
    """
    Retrieve a user's wallet for a currency.

    If lock=True, the wallet row is locked for the duration
    of the surrounding database transaction.
    """
    queryset = Wallet.objects

    if lock:
        queryset = queryset.select_for_update()

    try:
        return queryset.get(
            user=user,
            currency=currency,
            is_active=True,
        )
    except Wallet.DoesNotExist:
        raise ValidationError(
            f"{user.username} does not have an active "
            f"{currency.code} wallet."
        )


def create_wallet(user, currency):
    """
    Create a wallet if one does not already exist.
    """
    wallet, _ = Wallet.objects.get_or_create(
        user=user,
        currency=currency,
        defaults={
            "balance": ZERO,
            "is_active": True,
        },
    )

    return wallet


# =============================================================================
# RECEIPTS
# =============================================================================

def create_receipt(transaction):
    """
    Create a receipt for a successful transaction.
    """
    receipt, _ = Receipt.objects.get_or_create(
        transaction=transaction,
    )

    return receipt


# =============================================================================
# LEDGER
# =============================================================================

def create_ledger_entry(
    *,
    transaction,
    wallet,
    entry_type,
    amount,
    balance_before,
    balance_after,
    description="",
):
    """
    Create an immutable financial ledger entry.

    Wallet balances should always be changed together with a
    corresponding ledger entry.
    """
    amount = quantize_money(amount, wallet.currency)
    balance_before = quantize_money(
        balance_before,
        wallet.currency,
    )
    balance_after = quantize_money(
        balance_after,
        wallet.currency,
    )

    return LedgerEntry.objects.create(
        transaction=transaction,
        wallet=wallet,
        entry_type=entry_type,
        amount=amount,
        balance_before=balance_before,
        balance_after=balance_after,
        description=description,
    )


# =============================================================================
# WALLET CREDIT / DEBIT
# =============================================================================

def credit_wallet(
    *,
    wallet,
    amount,
    transaction,
    description="",
):
    """
    Credit a wallet and create its ledger entry.

    Must be called inside an atomic transaction.
    """
    amount = quantize_money(amount, wallet.currency)

    if amount <= ZERO:
        raise ValidationError("Credit amount must be greater than zero.")

    balance_before = wallet.balance
    balance_after = balance_before + amount

    wallet.balance = balance_after
    wallet.save(
        update_fields=[
            "balance",
            "updated_at",
        ]
    )

    return create_ledger_entry(
        transaction=transaction,
        wallet=wallet,
        entry_type=LedgerEntry.EntryType.CREDIT,
        amount=amount,
        balance_before=balance_before,
        balance_after=balance_after,
        description=description,
    )


def debit_wallet(
    *,
    wallet,
    amount,
    transaction,
    description="",
):
    """
    Debit a wallet and create its ledger entry.

    Must be called inside an atomic transaction.
    """
    amount = quantize_money(amount, wallet.currency)

    if amount <= ZERO:
        raise ValidationError("Debit amount must be greater than zero.")

    if wallet.balance < amount:
        raise ValidationError("Insufficient wallet balance.")

    balance_before = wallet.balance
    balance_after = balance_before - amount

    wallet.balance = balance_after
    wallet.save(
        update_fields=[
            "balance",
            "updated_at",
        ]
    )

    return create_ledger_entry(
        transaction=transaction,
        wallet=wallet,
        entry_type=LedgerEntry.EntryType.DEBIT,
        amount=amount,
        balance_before=balance_before,
        balance_after=balance_after,
        description=description,
    )


# =============================================================================
# INTERNAL TRANSFERS
# =============================================================================

@transaction.atomic
def transfer(
    *,
    sender,
    receiver,
    amount,
    currency,
    note="",
):
    """
    Pheral-to-Pheral wallet transfer.

    No external payment provider is required.

    Money moves:

        sender wallet
            ↓
        ledger debit
            ↓
        receiver wallet
            ↓
        ledger credit
    """
    if sender.pk == receiver.pk:
        raise ValidationError(
            "You cannot transfer money to yourself."
        )

    if not sender.is_active:
        raise ValidationError("Sender account is inactive.")

    if not receiver.is_active:
        raise ValidationError("Receiver account is inactive.")

    amount = quantize_money(amount, currency)

    if amount <= ZERO:
        raise ValidationError(
            "Transfer amount must be greater than zero."
        )

    sender_wallet = get_wallet(
        sender,
        currency,
        lock=True,
    )

    receiver_wallet = get_wallet(
        receiver,
        currency,
        lock=True,
    )

    transfer_transaction = PheralTransaction.objects.create(
        transaction_type=TransactionType.TRANSFER,
        status=TransactionStatus.PROCESSING,
        sender=sender,
        receiver=receiver,
        sender_wallet=sender_wallet,
        receiver_wallet=receiver_wallet,
        amount=amount,
        currency=currency,
        fee=ZERO,
        note=note,
    )

    debit_wallet(
        wallet=sender_wallet,
        amount=amount,
        transaction=transfer_transaction,
        description=f"Transfer to @{receiver.username}",
    )

    credit_wallet(
        wallet=receiver_wallet,
        amount=amount,
        transaction=transfer_transaction,
        description=f"Transfer from @{sender.username}",
    )

    transfer_transaction.status = TransactionStatus.SUCCESS
    transfer_transaction.save(
        update_fields=[
            "status",
            "updated_at",
        ]
    )

    receipt = create_receipt(
        transfer_transaction
    )

    return transfer_transaction, receipt


# =============================================================================
# GLOBAL TRANSFER
# =============================================================================

@transaction.atomic
def global_transfer(
    *,
    sender,
    receiver,
    source_amount,
    source_currency,
    destination_currency,
    exchange_rate,
    note="",
    fee=ZERO,
):
    """
    Cross-currency Pheral transfer.

    Example:

        Sender:
            NGN 100,000

        Receiver:
            USD equivalent

    The exchange rate used for the transaction is stored permanently
    on the transaction itself.
    """
    if sender.pk == receiver.pk:
        raise ValidationError(
            "You cannot transfer money to yourself."
        )

    source_amount = quantize_money(
        source_amount,
        source_currency,
    )

    exchange_rate = Decimal(str(exchange_rate))

    if exchange_rate <= ZERO:
        raise ValidationError(
            "Exchange rate must be greater than zero."
        )

    fee = quantize_money(
        fee,
        source_currency,
    )

    destination_raw = (
        source_amount * exchange_rate
    )

    destination_amount = quantize_money(
        destination_raw,
        destination_currency,
    )

    if destination_amount <= ZERO:
        raise ValidationError(
            "Calculated destination amount is invalid."
        )

    sender_wallet = get_wallet(
        sender,
        source_currency,
        lock=True,
    )

    receiver_wallet = get_wallet(
        receiver,
        destination_currency,
        lock=True,
    )

    total_debit = source_amount + fee

    if sender_wallet.balance < total_debit:
        raise ValidationError(
            "Insufficient wallet balance."
        )

    transfer_transaction = PheralTransaction.objects.create(
        transaction_type=TransactionType.GLOBAL_TRANSFER,
        status=TransactionStatus.PROCESSING,
        sender=sender,
        receiver=receiver,
        sender_wallet=sender_wallet,
        receiver_wallet=receiver_wallet,
        amount=destination_amount,
        currency=destination_currency,
        source_amount=source_amount,
        source_currency=source_currency,
        destination_amount=destination_amount,
        destination_currency=destination_currency,
        exchange_rate=exchange_rate,
        fee=fee,
        note=note,
    )

    debit_wallet(
        wallet=sender_wallet,
        amount=total_debit,
        transaction=transfer_transaction,
        description=(
            f"Global transfer to "
            f"@{receiver.username}"
        ),
    )

    credit_wallet(
        wallet=receiver_wallet,
        amount=destination_amount,
        transaction=transfer_transaction,
        description=(
            f"Global transfer from "
            f"@{sender.username}"
        ),
    )

    transfer_transaction.status = TransactionStatus.SUCCESS
    transfer_transaction.save(
        update_fields=[
            "status",
            "updated_at",
        ]
    )

    receipt = create_receipt(
        transfer_transaction
    )

    return transfer_transaction, receipt


# =============================================================================
# TOP UP
# =============================================================================

@transaction.atomic
def top_up_wallet(
    *,
    user,
    amount,
    currency,
    external_reference,
    provider="",
    metadata=None,
):
    """
    Credit a Pheral wallet after an external payment
    has been verified.

    IMPORTANT:
    This function assumes the payment provider has already
    confirmed the payment.

    It should NOT be called directly from an unverified
    frontend request.
    """
    amount = quantize_money(
        amount,
        currency,
    )

    if amount <= ZERO:
        raise ValidationError(
            "Top-up amount must be greater than zero."
        )

    if not external_reference:
        raise ValidationError(
            "External payment reference is required."
        )

    existing = PheralTransaction.objects.filter(
        transaction_type=TransactionType.TOP_UP,
        external_reference=external_reference,
    ).first()

    if existing:
        return existing, getattr(
            existing,
            "receipt",
            None,
        )

    wallet = get_wallet(
        user,
        currency,
        lock=True,
    )

    top_up_transaction = PheralTransaction.objects.create(
        transaction_type=TransactionType.TOP_UP,
        status=TransactionStatus.PROCESSING,
        receiver=user,
        receiver_wallet=wallet,
        amount=amount,
        currency=currency,
        external_reference=external_reference,
        metadata={
            **(metadata or {}),
            "provider": provider,
        },
    )

    credit_wallet(
        wallet=wallet,
        amount=amount,
        transaction=top_up_transaction,
        description="Pheral wallet top-up",
    )

    top_up_transaction.status = TransactionStatus.SUCCESS
    top_up_transaction.save(
        update_fields=[
            "status",
            "updated_at",
        ]
    )

    receipt = create_receipt(
        top_up_transaction
    )

    return top_up_transaction, receipt


# =============================================================================
# WITHDRAWAL
# =============================================================================

@transaction.atomic
def withdraw(
    *,
    user,
    amount,
    currency,
    destination,
    fee=ZERO,
    metadata=None,
):
    """
    Withdraw funds from Pheral to an external destination.

    The actual provider/bank payout should be processed separately.
    """
    amount = quantize_money(
        amount,
        currency,
    )

    fee = quantize_money(
        fee,
        currency,
    )

    if amount <= ZERO:
        raise ValidationError(
            "Withdrawal amount must be greater than zero."
        )

    if not destination:
        raise ValidationError(
            "Withdrawal destination is required."
        )

    wallet = get_wallet(
        user,
        currency,
        lock=True,
    )

    total_debit = amount + fee

    if wallet.balance < total_debit:
        raise ValidationError(
            "Insufficient wallet balance."
        )

    withdrawal_transaction = PheralTransaction.objects.create(
        transaction_type=TransactionType.WITHDRAWAL,
        status=TransactionStatus.PROCESSING,
        sender=user,
        sender_wallet=wallet,
        amount=amount,
        currency=currency,
        fee=fee,
        external_reference="",
        metadata={
            **(metadata or {}),
            "destination": destination,
        },
    )

    debit_wallet(
        wallet=wallet,
        amount=total_debit,
        transaction=withdrawal_transaction,
        description="Pheral withdrawal",
    )

    return withdrawal_transaction


# =============================================================================
# GROUP PAYMENTS
# =============================================================================

@transaction.atomic
def contribute_to_group_payment(
    *,
    contributor,
    group_payment,
    amount,
):
    """
    Contribute money to a group payment.
    """
    if group_payment.status != GroupPaymentStatus.OPEN:
        raise ValidationError(
            "This group payment is no longer open."
        )

    if (
        group_payment.deadline
        and timezone.now() >= group_payment.deadline
    ):
        group_payment.status = GroupPaymentStatus.EXPIRED
        group_payment.save(
            update_fields=[
                "status",
                "updated_at",
            ]
        )

        raise ValidationError(
            "This group payment has expired."
        )

    amount = quantize_money(
        amount,
        group_payment.currency,
    )

    remaining = group_payment.remaining_amount

    if amount > remaining:
        raise ValidationError(
            "Contribution exceeds the remaining amount."
        )

    wallet = get_wallet(
        contributor,
        group_payment.currency,
        lock=True,
    )

    group_transaction = PheralTransaction.objects.create(
        transaction_type=TransactionType.GROUP_PAYMENT,
        status=TransactionStatus.PROCESSING,
        sender=contributor,
        sender_wallet=wallet,
        amount=amount,
        currency=group_payment.currency,
        metadata={
            "group_payment_id": group_payment.pk,
        },
    )

    debit_wallet(
        wallet=wallet,
        amount=amount,
        transaction=group_transaction,
        description=(
            f"Contribution to "
            f"{group_payment.title}"
        ),
    )

    contribution = GroupPaymentContribution.objects.create(
        group_payment=group_payment,
        contributor=contributor,
        amount=amount,
        transaction=group_transaction,
    )

    group_transaction.status = TransactionStatus.SUCCESS
    group_transaction.save(
        update_fields=[
            "status",
            "updated_at",
        ]
    )

    if (
        group_payment.total_contributed
        >= group_payment.target_amount
    ):
        group_payment.status = GroupPaymentStatus.COMPLETED
        group_payment.save(
            update_fields=[
                "status",
                "updated_at",
            ]
        )

    receipt = create_receipt(
        group_transaction
    )

    return contribution, group_transaction, receipt


# =============================================================================
# GROUP WITHDRAWAL
# =============================================================================

@transaction.atomic
def request_group_withdrawal(
    *,
    requester,
    conversation,
    amount,
    currency,
    destination,
    fee=ZERO,
):
    """
    Create a group withdrawal request.

    Approval/authorization is intentionally separate.
    """
    amount = quantize_money(
        amount,
        currency,
    )

    fee = quantize_money(
        fee,
        currency,
    )

    if amount <= ZERO:
        raise ValidationError(
            "Withdrawal amount must be greater than zero."
        )

    if not destination:
        raise ValidationError(
            "Withdrawal destination is required."
        )

    withdrawal = GroupWithdrawal.objects.create(
        conversation=conversation,
        requester=requester,
        amount=amount,
        currency=currency,
        fee=fee,
        destination=destination,
        status=GroupWithdrawalStatus.PENDING,
    )

    return withdrawal


# =============================================================================
# AGENT MODE
# =============================================================================

@transaction.atomic
def execute_agent_payment(
    *,
    user,
    target_user,
    amount,
    currency,
    command,
):
    """
    Execute an Agent Mode payment.

    Example command:

        @agent pay @teddy 2000
    """
    action = AgentAction.objects.create(
        user=user,
        action_type=AgentActionType.PAY,
        command=command,
        target_user=target_user,
        status=AgentActionStatus.PENDING,
    )

    try:
        transfer_transaction, receipt = transfer(
            sender=user,
            receiver=target_user,
            amount=amount,
            currency=currency,
        )

        action.transaction = transfer_transaction
        action.status = AgentActionStatus.SUCCESS
        action.result_message = (
            f"Payment of "
            f"{amount} {currency.code} sent to "
            f"@{target_user.username}."
        )
        action.completed_at = timezone.now()

        action.save(
            update_fields=[
                "transaction",
                "status",
                "result_message",
                "completed_at",
            ]
        )

        return action, transfer_transaction, receipt

    except Exception as exc:
        action.status = AgentActionStatus.FAILED
        action.result_message = str(exc)
        action.completed_at = timezone.now()

        action.save(
            update_fields=[
                "status",
                "result_message",
                "completed_at",
            ]
        )

        raise


# =============================================================================
# AGENT MESSAGE
# =============================================================================

def create_agent_message_action(
    *,
    user,
    target_user,
    command,
):
    """
    Record an Agent Mode message action.

    The actual Message object is created by the chat service/view
    after the target conversation is resolved.
    """
    return AgentAction.objects.create(
        user=user,
        action_type=AgentActionType.MESSAGE,
        command=command,
        target_user=target_user,
        status=AgentActionStatus.SUCCESS,
        result_message=(
            f"Message action initialized for "
            f"@{target_user.username}."
        ),
        completed_at=timezone.now(),
    )