import re

from django.db import transaction
from django.utils import timezone

from .models import (
    User,
    Currency,
    MessageType,
    AgentAction,
    AgentActionType,
    AgentActionStatus,
)
from .services import (
    transfer_money,
    send_message,
)


# =============================================================================
# COMMAND PATTERNS
# =============================================================================

PAY_PATTERN = re.compile(
    r"^@agent\s+pay\s+@?([a-zA-Z0-9_.-]+)\s+([0-9]+(?:\.[0-9]{1,2})?)"
    r"(?:\s+([A-Z]{3}))?$",
    re.IGNORECASE,
)

MESSAGE_PATTERN = re.compile(
    r"^@agent\s+msg\s+@?([a-zA-Z0-9_.-]+)\s+(.+)$",
    re.IGNORECASE,
)


# =============================================================================
# RESULT
# =============================================================================

class AgentResult:
    def __init__(
        self,
        success,
        message,
        action=None,
    ):
        self.success = success
        self.message = message
        self.action = action

    def __bool__(self):
        return self.success


# =============================================================================
# USER LOOKUP
# =============================================================================

def find_user(username):
    username = username.lstrip("@").lower().strip()

    return (
        User.objects
        .filter(
            username__iexact=username,
            is_active=True,
        )
        .first()
    )


# =============================================================================
# CURRENCY LOOKUP
# =============================================================================

def get_currency(code):
    return (
        Currency.objects
        .filter(
            code__iexact=code,
            is_active=True,
        )
        .first()
    )


# =============================================================================
# COMMAND PARSER
# =============================================================================

def parse_command(command):
    """
    Converts an Agent Mode command into a structured action.

    Supported MVP commands:

        @agent pay @username 2000
        @agent pay @username 2000 NGN

        @agent msg @username Hello bro
    """

    if not command:
        return None

    command = command.strip()

    pay_match = PAY_PATTERN.match(command)

    if pay_match:
        username = pay_match.group(1)
        amount = pay_match.group(2)
        currency_code = pay_match.group(3)

        return {
            "action_type": AgentActionType.PAY,
            "username": username,
            "amount": amount,
            "currency_code": (
                currency_code.upper()
                if currency_code
                else None
            ),
            "command": command,
        }

    message_match = MESSAGE_PATTERN.match(command)

    if message_match:
        username = message_match.group(1)
        message = message_match.group(2).strip()

        return {
            "action_type": AgentActionType.MESSAGE,
            "username": username,
            "message": message,
            "command": command,
        }

    return None


# =============================================================================
# HELP
# =============================================================================

def agent_help():
    return (
        "Pheral Agent commands:\n"
        "@agent pay @username 2000\n"
        "@agent pay @username 2000 NGN\n"
        "@agent msg @username Hello there"
    )


# =============================================================================
# PAY
# =============================================================================

@transaction.atomic
def execute_pay(user, parsed_command):
    target_user = find_user(
        parsed_command["username"]
    )

    if not target_user:
        return AgentResult(
            success=False,
            message=(
                f"@{parsed_command['username']} "
                "could not be found on Pheral."
            ),
        )

    if target_user == user:
        return AgentResult(
            success=False,
            message="You cannot pay yourself.",
        )

    try:
        amount = parsed_command["amount"]

        currency_code = (
            parsed_command["currency_code"]
            or "NGN"
        )

        currency = get_currency(currency_code)

        if not currency:
            return AgentResult(
                success=False,
                message=(
                    f"{currency_code} is not an active "
                    "Pheral currency."
                ),
            )

        transaction_record, receipt = transfer_money(
            sender=user,
            receiver=target_user,
            amount=amount,
            currency=currency,
            note="Pheral Agent payment",
        )

    except (ValueError, PermissionError) as exc:
        return AgentResult(
            success=False,
            message=str(exc),
        )

    action = AgentAction.objects.create(
        user=user,
        action_type=AgentActionType.PAY,
        command=parsed_command["command"],
        target_user=target_user,
        status=AgentActionStatus.SUCCESS,
        result_message=(
            f"Paid {transaction_record.amount} "
            f"{transaction_record.currency.code} "
            f"to @{target_user.username}."
        ),
        transaction=transaction_record,
        metadata={
            "receipt_number": receipt.receipt_number,
        },
        completed_at=timezone.now(),
    )

    return AgentResult(
        success=True,
        message=(
            f"Payment successful. "
            f"{transaction_record.amount} "
            f"{transaction_record.currency.code} "
            f"sent to @{target_user.username}."
        ),
        action=action,
    )


# =============================================================================
# MESSAGE
# =============================================================================

@transaction.atomic
def execute_message(user, parsed_command):
    target_user = find_user(
        parsed_command["username"]
    )

    if not target_user:
        return AgentResult(
            success=False,
            message=(
                f"@{parsed_command['username']} "
                "could not be found on Pheral."
            ),
        )

    if target_user == user:
        return AgentResult(
            success=False,
            message="You cannot send an agent message to yourself.",
        )

    message_content = parsed_command["message"]

    if not message_content:
        return AgentResult(
            success=False,
            message="Message cannot be empty.",
        )

    try:
        from .services import get_or_create_direct_conversation

        conversation = get_or_create_direct_conversation(
            user,
            target_user,
        )

        message = send_message(
            sender=user,
            conversation=conversation,
            content=message_content,
            message_type=MessageType.AGENT_ACTION,
        )

    except (ValueError, PermissionError) as exc:
        return AgentResult(
            success=False,
            message=str(exc),
        )

    action = AgentAction.objects.create(
        user=user,
        action_type=AgentActionType.MESSAGE,
        command=parsed_command["command"],
        target_user=target_user,
        status=AgentActionStatus.SUCCESS,
        result_message=(
            f"Message sent to @{target_user.username}."
        ),
        metadata={
            "message_id": message.id,
            "conversation_id": conversation.id,
        },
        completed_at=timezone.now(),
    )

    return AgentResult(
        success=True,
        message=(
            f"Message sent to @{target_user.username}."
        ),
        action=action,
    )


# =============================================================================
# EXECUTE COMMAND
# =============================================================================

def execute_agent_command(user, command):
    """
    Main entry point for Agent Mode.

    Returns an AgentResult.
    """

    if not user or not user.is_authenticated:
        return AgentResult(
            success=False,
            message="Authentication is required.",
        )

    if not command:
        return AgentResult(
            success=False,
            message="Enter an Agent Mode command.",
        )

    parsed_command = parse_command(command)

    if not parsed_command:
        return AgentResult(
            success=False,
            message=agent_help(),
        )

    if parsed_command["action_type"] == AgentActionType.PAY:
        return execute_pay(
            user,
            parsed_command,
        )

    if parsed_command["action_type"] == AgentActionType.MESSAGE:
        return execute_message(
            user,
            parsed_command,
        )

    return AgentResult(
        success=False,
        message="Unsupported Agent Mode command.",
    )