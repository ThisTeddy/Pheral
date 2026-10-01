from decimal import Decimal
import uuid

from cloudinary.models import CloudinaryField
from django.contrib.auth.models import AbstractUser
from django.core.validators import MinValueValidator, MaxValueValidator
from django.db import models
from django.utils import timezone


# ============================================================
# HELPERS
# ============================================================

def generate_reference(prefix="PHR"):
    """
    Generate a human-readable unique Pheral reference.
    Example: PHR-7F4A91C2
    """
    return f"{prefix}-{uuid.uuid4().hex[:8].upper()}"


# ============================================================
# USER
# ============================================================

class User(AbstractUser):

    username = models.CharField(max_length=30, unique=True, db_index=True)
    phone_number = models.CharField(max_length=30, unique=True, db_index=True)
    email = models.EmailField(blank=True, null=True)
    first_name = models.CharField(max_length=100, blank=False)
    last_name = models.CharField(max_length=100, blank=False)
    avatar = CloudinaryField("image", folder="avatars", blank=True, null=True)
    bio = models.TextField(blank=True, max_length=500)
    is_phone_verified = models.BooleanField(default=False)
    is_active_user = models.BooleanField(default=True)
    last_seen = models.DateTimeField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    USERNAME_FIELD = "phone_number"
    REQUIRED_FIELDS = ["username", "first_name", "last_name"]


    def __str__(self):
        return f"@{self.username}"

    @property
    def display_name(self):
        return f"{self.first_name} {self.last_name}".strip()

    @property
    def is_online(self):
        if not self.last_seen:
            return False
        return self.last_seen >= timezone.now() - timezone.timedelta(minutes=5)


# ============================================================
# PHONE OTP
# ============================================================

class PhoneOTP(models.Model):

    PURPOSE_VERIFICATION = "verification"
    PURPOSE_PASSWORD_RESET = "password_reset"

    PURPOSE_CHOICES = [
        (PURPOSE_VERIFICATION, "Phone Verification"),
        (PURPOSE_PASSWORD_RESET, "Password Reset"),
    ]

    user = models.ForeignKey(User, on_delete=models.CASCADE, related_name="phone_otps")
    phone_number = models.CharField(max_length=30, db_index=True)
    code = models.CharField(max_length=6)
    purpose = models.CharField(max_length=30, choices=PURPOSE_CHOICES, default=PURPOSE_VERIFICATION, db_index=True)
    is_used = models.BooleanField(default=False)
    attempts = models.PositiveIntegerField(default=0)
    expires_at = models.DateTimeField()
    created_at = models.DateTimeField(auto_now_add=True)

    def __str__(self):
        return f"OTP for {self.phone_number} ({self.purpose})"

    @property
    def is_expired(self):
        return timezone.now() >= self.expires_at


# ============================================================
# CURRENCY
# ============================================================

class Currency(models.Model):

    code = models.CharField(max_length=10, unique=True, db_index=True)
    name = models.CharField(max_length=100)
    symbol = models.CharField(max_length=10)
    is_active = models.BooleanField(default=True)
    decimal_places = models.PositiveSmallIntegerField(default=2)
    created_at = models.DateTimeField(auto_now_add=True)

    def __str__(self):
        return f"{self.code} ({self.symbol})"


# ============================================================
# EXCHANGE RATE
# ============================================================

class ExchangeRate(models.Model):

    source_currency = models.ForeignKey(Currency, on_delete=models.CASCADE, related_name="source_exchange_rates")
    target_currency = models.ForeignKey(Currency, on_delete=models.CASCADE, related_name="target_exchange_rates")
    rate = models.DecimalField(max_digits=20, decimal_places=8, validators=[MinValueValidator(Decimal("0"))])
    is_active = models.BooleanField(default=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        constraints = [
            models.UniqueConstraint(fields=["source_currency", "target_currency"], name="unique_currency_pair"),
        ]

    def __str__(self):
        return f"{self.source_currency.code}/{self.target_currency.code}: {self.rate}"


# ============================================================
# WALLET
# ============================================================

class Wallet(models.Model):
    """
    A user's fiat wallet. A user can have wallets in multiple
    currencies. Internal Pheral transfers move balances directly;
    external payment providers are only needed for top-up and
    withdrawal.
    """

    user = models.ForeignKey(User, on_delete=models.CASCADE, related_name="wallets")
    currency = models.ForeignKey(Currency, on_delete=models.PROTECT, related_name="wallets")
    balance = models.DecimalField(
        max_digits=20, decimal_places=2, default=Decimal("0.00"),
        validators=[MinValueValidator(Decimal("0"))],
    )
    is_active = models.BooleanField(default=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        constraints = [
            models.UniqueConstraint(fields=["user", "currency"], name="unique_user_currency_wallet"),
            models.CheckConstraint(
                condition=models.Q(balance__gte=Decimal("0.00")),
                name="wallet_balance_non_negative",
            ),
        ]

    def __str__(self):
        return f"{self.user.username} - {self.currency.code} - {self.balance}"


# ============================================================
# WALLET TOKEN
# ============================================================

class WalletToken(models.Model):

    user = models.OneToOneField(User, on_delete=models.CASCADE, related_name="wallet_token")
    token = models.UUIDField(default=uuid.uuid4, unique=True, editable=False)
    is_active = models.BooleanField(default=True)
    issued_at = models.DateTimeField(auto_now_add=True)
    last_used_at = models.DateTimeField(null=True, blank=True)

    def __str__(self):
        return f"Wallet token - {self.user.username}"


# ============================================================
# BANK ACCOUNT (for withdrawals)
# ============================================================

class BankAccount(models.Model):
    """
    A verified external bank account a user can withdraw to.

    account_name is fetched from Paystack's account resolution
    API at add-time, never typed freely by the user, so it can't
    be spoofed to a name that doesn't match the real account.
    """

    user = models.ForeignKey(User, on_delete=models.CASCADE, related_name="bank_accounts")
    account_number = models.CharField(max_length=20)
    account_name = models.CharField(max_length=200)
    bank_code = models.CharField(max_length=10)
    bank_name = models.CharField(max_length=150)
    flutterwave_recipient_id = models.CharField(
    max_length=100,
    blank=True,
    default="",
    )
    is_active = models.BooleanField(default=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        constraints = [
            models.UniqueConstraint(
                fields=["user", "account_number", "bank_code"],
                name="unique_user_bank_account",
            ),
        ]

    def __str__(self):
        return f"{self.account_name} — {self.bank_name} ({self.account_number})"


# ============================================================
# TRANSACTION
# ============================================================

class PheralTransaction(models.Model):

    class TransactionType(models.TextChoices):
        TRANSFER = "transfer", "Transfer"
        TOP_UP = "top_up", "Top Up"
        WITHDRAWAL = "withdrawal", "Withdrawal"
        FX = "fx", "Currency Conversion"
        GROUP_TRANSFER = "group_transfer", "Group Transfer"
        HIRE_PAYMENT = "hire_payment", "Hire Payment"
        REFUND = "refund", "Refund"
        AIRTIME = "airtime", "Airtime"
        DATA = "data", "Data"

    class Status(models.TextChoices):
        PENDING = "pending", "Pending"
        COMPLETED = "completed", "Completed"
        FAILED = "failed", "Failed"
        CANCELLED = "cancelled", "Cancelled"
        REVERSED = "reversed", "Reversed"

    reference = models.CharField(max_length=30, unique=True, default=generate_reference, editable=False, db_index=True)
    sender = models.ForeignKey(User, on_delete=models.PROTECT, related_name="sent_transactions", null=True, blank=True)
    recipient = models.ForeignKey(User, on_delete=models.PROTECT, related_name="received_transactions", null=True, blank=True)
    sender_wallet = models.ForeignKey(Wallet, on_delete=models.PROTECT, related_name="outgoing_transactions", null=True, blank=True)
    recipient_wallet = models.ForeignKey(Wallet, on_delete=models.PROTECT, related_name="incoming_transactions", null=True, blank=True)
    transaction_type = models.CharField(max_length=30, choices=TransactionType.choices)
    amount = models.DecimalField(max_digits=20, decimal_places=2, validators=[MinValueValidator(Decimal("0.01"))])
    currency = models.ForeignKey(Currency, on_delete=models.PROTECT, related_name="transactions")
    fee = models.DecimalField(max_digits=20, decimal_places=2, default=Decimal("0.00"))
    status = models.CharField(max_length=20, choices=Status.choices, default=Status.PENDING, db_index=True)
    description = models.CharField(max_length=255, blank=True)
    external_reference = models.CharField(max_length=255, blank=True, null=True)
    metadata = models.JSONField(default=dict, blank=True)
    created_at = models.DateTimeField(auto_now_add=True, db_index=True)
    completed_at = models.DateTimeField(null=True, blank=True)

    def __str__(self):
        return self.reference


# ============================================================
# LEDGER ENTRY
# ============================================================

class LedgerEntry(models.Model):

    class EntryType(models.TextChoices):
        DEBIT = "debit", "Debit"
        CREDIT = "credit", "Credit"

    transaction = models.ForeignKey(PheralTransaction, on_delete=models.PROTECT, related_name="ledger_entries")
    wallet = models.ForeignKey(Wallet, on_delete=models.PROTECT, related_name="ledger_entries")
    entry_type = models.CharField(max_length=10, choices=EntryType.choices)
    amount = models.DecimalField(max_digits=20, decimal_places=2, validators=[MinValueValidator(Decimal("0.01"))])
    balance_before = models.DecimalField(max_digits=20, decimal_places=2)
    balance_after = models.DecimalField(max_digits=20, decimal_places=2)
    description = models.CharField(max_length=255, blank=True)
    created_at = models.DateTimeField(auto_now_add=True, db_index=True)

    def __str__(self):
        return f"{self.wallet.user.username} - {self.entry_type} - {self.amount}"


# ============================================================
# RECEIPT
# ============================================================

class Receipt(models.Model):

    transaction = models.OneToOneField(PheralTransaction, on_delete=models.PROTECT, related_name="receipt")
    reference = models.CharField(max_length=30, unique=True, default=generate_reference, editable=False, db_index=True)
    payer = models.ForeignKey(User, on_delete=models.PROTECT, related_name="payment_receipts")
    recipient = models.ForeignKey(User, on_delete=models.PROTECT, related_name="received_receipts")
    amount = models.DecimalField(max_digits=20, decimal_places=2)
    currency = models.ForeignKey(Currency, on_delete=models.PROTECT, related_name="receipts")
    description = models.CharField(max_length=255, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    def __str__(self):
        return self.reference


# ============================================================
# CONVERSATION
# ============================================================

class Conversation(models.Model):

    class ConversationType(models.TextChoices):
        DIRECT = "direct", "Direct"
        GROUP = "group", "Group"

    conversation_type = models.CharField(max_length=10, choices=ConversationType.choices, default=ConversationType.DIRECT)
    name = models.CharField(max_length=100, blank=True)
    description = models.TextField(blank=True)
    avatar = CloudinaryField("image", folder="conversation_avatars", blank=True, null=True)
    created_by = models.ForeignKey(User, on_delete=models.SET_NULL, null=True, blank=True, related_name="created_conversations")

    direct_pair_key = models.CharField(max_length=40, blank=True, null=True, db_index=True)

    is_active = models.BooleanField(default=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        constraints = [
            models.UniqueConstraint(
                fields=["conversation_type", "direct_pair_key"],
                condition=models.Q(conversation_type="direct"),
                name="unique_direct_conversation_per_pair",
            ),
        ]

    def __str__(self):
        return self.name or f"Conversation {self.pk}"


# ============================================================
# CONVERSATION PARTICIPANT
# ============================================================

class ConversationParticipant(models.Model):

    conversation = models.ForeignKey(Conversation, on_delete=models.CASCADE, related_name="participants")
    user = models.ForeignKey(User, on_delete=models.CASCADE, related_name="conversation_participations")
    is_admin = models.BooleanField(default=False)
    is_muted = models.BooleanField(default=False)
    is_archived = models.BooleanField(default=False)
    is_pinned = models.BooleanField(default=False)
    joined_at = models.DateTimeField(auto_now_add=True)
    last_read_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        constraints = [
            models.UniqueConstraint(fields=["conversation", "user"], name="unique_conversation_participant"),
        ]

    def __str__(self):
        return f"{self.user.username} - {self.conversation_id}"


# ============================================================
# MESSAGE
# ============================================================

class Message(models.Model):

    class MessageType(models.TextChoices):
        TEXT = "text", "Text"
        IMAGE = "image", "Image"
        VIDEO = "video", "Video"
        FILE = "file", "File"
        VOICE = "voice", "Voice"
        PAYMENT = "payment", "Payment"
        HIRE = "hire", "Hire"
        SYSTEM = "system", "System"
        AGENT = "agent", "Agent"

    conversation = models.ForeignKey(Conversation, on_delete=models.CASCADE, related_name="messages")
    sender = models.ForeignKey(User, on_delete=models.SET_NULL, null=True, related_name="messages")
    message_type = models.CharField(max_length=20, choices=MessageType.choices, default=MessageType.TEXT)
    content = models.TextField(blank=True)
    attachment = CloudinaryField("file", folder="message_files", resource_type="auto", blank=True, null=True)
    reply_to = models.ForeignKey("self", on_delete=models.SET_NULL, null=True, blank=True, related_name="replies")
    transaction = models.ForeignKey(PheralTransaction, on_delete=models.SET_NULL, null=True, blank=True, related_name="messages")
    receipt = models.ForeignKey(Receipt, on_delete=models.SET_NULL, null=True, blank=True, related_name="messages")
    is_deleted = models.BooleanField(default=False)
    created_at = models.DateTimeField(auto_now_add=True, db_index=True)
    updated_at = models.DateTimeField(auto_now=True)

    def __str__(self):
        return f"Message {self.pk}"


# ============================================================
# MESSAGE READ
# ============================================================

class MessageRead(models.Model):

    message = models.ForeignKey(Message, on_delete=models.CASCADE, related_name="read_receipts")
    user = models.ForeignKey(User, on_delete=models.CASCADE, related_name="message_reads")
    read_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        constraints = [
            models.UniqueConstraint(fields=["message", "user"], name="unique_message_read"),
        ]


# ============================================================
# GROUP LEDGER
# ============================================================

class GroupLedger(models.Model):

    conversation = models.OneToOneField(Conversation, on_delete=models.CASCADE, related_name="group_ledger")
    currency = models.ForeignKey(Currency, on_delete=models.PROTECT, related_name="group_ledgers")
    balance = models.DecimalField(max_digits=20, decimal_places=2, default=Decimal("0.00"))
    withdrawal_fee = models.DecimalField(max_digits=20, decimal_places=2, default=Decimal("0.00"))
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    def __str__(self):
        return f"Group ledger - {self.conversation_id}"


# ============================================================
# GROUP LEDGER ENTRY
# ============================================================

class GroupLedgerEntry(models.Model):

    class EntryType(models.TextChoices):
        CONTRIBUTION = "contribution", "Contribution"
        WITHDRAWAL = "withdrawal", "Withdrawal"
        FEE = "fee", "Fee"
        REFUND = "refund", "Refund"

    ledger = models.ForeignKey(GroupLedger, on_delete=models.CASCADE, related_name="entries")
    user = models.ForeignKey(User, on_delete=models.PROTECT, related_name="group_ledger_entries")
    entry_type = models.CharField(max_length=20, choices=EntryType.choices)
    amount = models.DecimalField(max_digits=20, decimal_places=2)
    reference = models.CharField(max_length=30, unique=True, default=generate_reference, editable=False)
    description = models.CharField(max_length=255, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)


# ============================================================
# STATUS
# ============================================================

class Status(models.Model):

    class StatusType(models.TextChoices):
        TEXT = "text", "Text"
        IMAGE = "image", "Image"
        VIDEO = "video", "Video"

    user = models.ForeignKey(User, on_delete=models.CASCADE, related_name="statuses")
    status_type = models.CharField(max_length=10, choices=StatusType.choices, default=StatusType.TEXT)
    text = models.TextField(blank=True, max_length=1000)
    media = CloudinaryField("file", folder="statuses", resource_type="auto", blank=True, null=True)
    created_at = models.DateTimeField(auto_now_add=True)
    expires_at = models.DateTimeField(db_index=True)
    is_active = models.BooleanField(default=True)

    def __str__(self):
        return f"{self.user.username} status"


# ============================================================
# STATUS VIEW
# ============================================================

class StatusView(models.Model):

    status = models.ForeignKey(Status, on_delete=models.CASCADE, related_name="views")
    viewer = models.ForeignKey(User, on_delete=models.CASCADE, related_name="status_views")
    viewed_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        constraints = [
            models.UniqueConstraint(fields=["status", "viewer"], name="unique_status_view"),
        ]
        ordering = ["-viewed_at"]

    def __str__(self):
        return f"{self.viewer.username} viewed {self.status.user.username}'s status"


# ============================================================
# POST / FEED
# ============================================================

class Post(models.Model):

    author = models.ForeignKey(User, on_delete=models.CASCADE, related_name="posts")
    content = models.TextField(blank=True, max_length=5000)
    media = CloudinaryField("file", folder="posts", resource_type="auto", blank=True, null=True)
    created_at = models.DateTimeField(auto_now_add=True, db_index=True)
    updated_at = models.DateTimeField(auto_now=True)
    is_deleted = models.BooleanField(default=False)

    def __str__(self):
        return f"Post {self.pk} - {self.author.username}"


# ============================================================
# POST LIKE
# ============================================================

class PostLike(models.Model):

    post = models.ForeignKey(Post, on_delete=models.CASCADE, related_name="likes")
    user = models.ForeignKey(User, on_delete=models.CASCADE, related_name="post_likes")
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        constraints = [
            models.UniqueConstraint(fields=["post", "user"], name="unique_post_like"),
        ]


# ============================================================
# POST COMMENT
# ============================================================

class PostComment(models.Model):

    post = models.ForeignKey(Post, on_delete=models.CASCADE, related_name="comments")
    user = models.ForeignKey(User, on_delete=models.CASCADE, related_name="post_comments")
    content = models.TextField(max_length=1000)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)


# ============================================================
# HIRE JOB
# ============================================================

class HireJob(models.Model):

    class Status(models.TextChoices):
        OPEN = "open", "Open"
        IN_PROGRESS = "in_progress", "In Progress"
        COMPLETED = "completed", "Completed"
        CANCELLED = "cancelled", "Cancelled"

    employer = models.ForeignKey(User, on_delete=models.CASCADE, related_name="hire_jobs")
    title = models.CharField(max_length=200)
    description = models.TextField(max_length=5000)
    budget = models.DecimalField(max_digits=20, decimal_places=2, validators=[MinValueValidator(Decimal("0.01"))])
    currency = models.ForeignKey(Currency, on_delete=models.PROTECT, related_name="hire_jobs")
    status = models.CharField(max_length=20, choices=Status.choices, default=Status.OPEN, db_index=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    def __str__(self):
        return self.title


# ============================================================
# HIRE REQUEST
# ============================================================

class HireRequest(models.Model):

    class Status(models.TextChoices):
        PENDING = "pending", "Pending"
        ACCEPTED = "accepted", "Accepted"
        DECLINED = "declined", "Declined"
        CANCELLED = "cancelled", "Cancelled"
        COMPLETED = "completed", "Completed"

    job = models.ForeignKey(HireJob, on_delete=models.CASCADE, related_name="requests")
    requester = models.ForeignKey(User, on_delete=models.CASCADE, related_name="hire_requests_sent")
    worker = models.ForeignKey(User, on_delete=models.CASCADE, related_name="hire_requests_received")
    message = models.TextField(blank=True, max_length=2000)
    proposed_amount = models.DecimalField(max_digits=20, decimal_places=2, null=True, blank=True)
    status = models.CharField(max_length=20, choices=Status.choices, default=Status.PENDING)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    def __str__(self):
        return f"{self.requester.username} -> {self.worker.username}"


# ============================================================
# HIRE PAYMENT
# ============================================================

class HirePayment(models.Model):

    hire_request = models.OneToOneField(HireRequest, on_delete=models.PROTECT, related_name="payment")
    transaction = models.OneToOneField(PheralTransaction, on_delete=models.PROTECT, related_name="hire_payment")
    created_at = models.DateTimeField(auto_now_add=True)


# ============================================================
# NOTIFICATION
# ============================================================

class Notification(models.Model):

    class NotificationType(models.TextChoices):
        MESSAGE = "message", "Message"
        PAYMENT = "payment", "Payment"
        HIRE = "hire", "Hire"
        STATUS = "status", "Status"
        POST = "post", "Post"
        GROUP = "group", "Group"
        SYSTEM = "system", "System"
        AGENT = "agent", "Agent"

    user = models.ForeignKey(User, on_delete=models.CASCADE, related_name="notifications")
    notification_type = models.CharField(max_length=20, choices=NotificationType.choices)
    title = models.CharField(max_length=200)
    body = models.TextField(max_length=1000)
    link = models.CharField(max_length=500, blank=True)
    is_read = models.BooleanField(default=False)
    created_at = models.DateTimeField(auto_now_add=True, db_index=True)

    def __str__(self):
        return f"{self.user.username} - {self.title}"


# ============================================================
# PUSH DEVICE
# ============================================================

class PushDevice(models.Model):

    class Platform(models.TextChoices):
        WEB = "web", "Web"
        ANDROID = "android", "Android"
        IOS = "ios", "iOS"

    user = models.ForeignKey(User, on_delete=models.CASCADE, related_name="push_devices")
    platform = models.CharField(max_length=20, choices=Platform.choices, default=Platform.WEB)
    token = models.TextField(unique=True)
    is_active = models.BooleanField(default=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)


# ============================================================
# AGENT COMMAND
# ============================================================

class AgentCommand(models.Model):

    class Status(models.TextChoices):
        PENDING = "pending", "Pending"
        PROCESSING = "processing", "Processing"
        COMPLETED = "completed", "Completed"
        FAILED = "failed", "Failed"

    user = models.ForeignKey(User, on_delete=models.CASCADE, related_name="agent_commands")
    command = models.TextField(max_length=1000)
    action = models.CharField(max_length=100, blank=True)
    status = models.CharField(max_length=20, choices=Status.choices, default=Status.PENDING)
    result = models.JSONField(default=dict, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    completed_at = models.DateTimeField(null=True, blank=True)

    def __str__(self):
        return f"Agent command {self.pk}"


# ============================================================
# AGENT ACTIVITY
# ============================================================

class AgentActivity(models.Model):

    user = models.ForeignKey(User, on_delete=models.CASCADE, related_name="agent_activities")
    command = models.ForeignKey(AgentCommand, on_delete=models.CASCADE, related_name="activities")
    conversation = models.ForeignKey(Conversation, on_delete=models.SET_NULL, null=True, blank=True, related_name="agent_activities")
    transaction = models.ForeignKey(PheralTransaction, on_delete=models.SET_NULL, null=True, blank=True, related_name="agent_activities")
    action = models.CharField(max_length=100)
    details = models.JSONField(default=dict, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    def __str__(self):
        return f"Agent activity {self.pk}"


# ============================================================
# CONTACT / DISCOVERY
# ============================================================

class Contact(models.Model):

    owner = models.ForeignKey(User, on_delete=models.CASCADE, related_name="contacts")
    contact_user = models.ForeignKey(User, on_delete=models.CASCADE, related_name="contacted_by")
    phone_number = models.CharField(max_length=30, blank=True)
    nickname = models.CharField(max_length=100, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        constraints = [
            models.UniqueConstraint(fields=["owner", "contact_user"], name="unique_user_contact"),
        ]

    def __str__(self):
        return f"{self.owner.username} -> {self.contact_user.username}"


# ============================================================
# PRESENCE
# ============================================================

class UserPresence(models.Model):

    user = models.OneToOneField(User, on_delete=models.CASCADE, related_name="presence")
    is_online = models.BooleanField(default=False)
    last_seen_at = models.DateTimeField(null=True, blank=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        verbose_name = "User Presence"
        verbose_name_plural = "User Presence"

    def __str__(self):
        return f"{self.user.username} presence"


# ============================================================
# MODEL ADDITIONS — models.py
#
# 1. Add AIRTIME and DATA to PheralTransaction.TransactionType.
#    Find this class in models.py and add the two new lines:
#
#    class TransactionType(models.TextChoices):
#        TRANSFER = "transfer", "Transfer"
#        TOP_UP = "top_up", "Top Up"
#        WITHDRAWAL = "withdrawal", "Withdrawal"
#        FX = "fx", "Currency Conversion"
#        GROUP_TRANSFER = "group_transfer", "Group Transfer"
#        HIRE_PAYMENT = "hire_payment", "Hire Payment"
#        REFUND = "refund", "Refund"
#        AIRTIME = "airtime", "Airtime"          # <-- add
#        DATA = "data", "Data Bundle"            # <-- add
#
# 2. Add this new model anywhere below PheralTransaction:
# ============================================================
class NetworkProvider(models.Model):
    """
    Mobile network provider used for airtime and data purchases.

    The Flutterwave biller/item codes should match the values
    configured for the corresponding network in Flutterwave.
    """

    name = models.CharField(max_length=50, unique=True)
    code = models.CharField(max_length=20, unique=True)

    # Flutterwave biller information
    flutterwave_airtime_biller = models.CharField(
        max_length=100,
        blank=True,
        default="",
    )
    flutterwave_airtime_biller_code = models.CharField(
        max_length=50,
        blank=True,
        default="",
    )
    flutterwave_airtime_item_code = models.CharField(
        max_length=50,
        blank=True,
        default="",
    )
    flutterwave_data_biller = models.CharField(
        max_length=100,
        blank=True,
        default="",
    )
    flutterwave_data_biller_code = models.CharField(
        max_length=50,
        blank=True,
        default="",
    )

    logo = models.ImageField(
        upload_to="networks/",
        blank=True,
        null=True,
    )

    is_active = models.BooleanField(default=True)

    class Meta:
        ordering = ["name"]

    def __str__(self):
        return self.name
# Seed data (run once via shell or a data migration):
#
# from yourapp.models import NetworkProvider
# NetworkProvider.objects.bulk_create([
#     NetworkProvider(name="MTN", code="mtn",
#         flutterwave_airtime_biller="MTN Airtime", flutterwave_data_biller="MTN Data"),
#     NetworkProvider(name="Glo", code="glo",
#         flutterwave_airtime_biller="GLO Airtime", flutterwave_data_biller="GLO Data"),
#     NetworkProvider(name="Airtel", code="airtel",
#         flutterwave_airtime_biller="Airtel Airtime", flutterwave_data_biller="Airtel Data"),
#     NetworkProvider(name="9mobile", code="9mobile",
#         flutterwave_airtime_biller="9mobile Airtime", flutterwave_data_biller="9mobile Data"),
# ])
#
# Double-check the exact biller_name strings against
# GET https://api.flutterwave.com/v3/bill-categories?country=NG
# before relying on these — Flutterwave's naming isn't perfectly
# consistent across their own documentation versions.

class VirtualCard(models.Model):
    """
    A Flutterwave-issued virtual card funded from the user's wallet.
    The full card number/CVV are NEVER stored here — only what's
    safe to keep: masked PAN, expiry, and Flutterwave's own card id
    (used to re-fetch full details through their secure, one-time
    reveal endpoint when the user explicitly asks to view them).
    """

    class Status(models.TextChoices):
        ACTIVE = "active", "Active"
        FROZEN = "frozen", "Frozen"
        TERMINATED = "terminated", "Terminated"

    user = models.ForeignKey(User, on_delete=models.CASCADE, related_name="virtual_cards")
    currency = models.ForeignKey(Currency, on_delete=models.PROTECT, related_name="virtual_cards")
    flw_card_id = models.CharField(max_length=100, unique=True)
    masked_pan = models.CharField(max_length=25, blank=True)  # e.g. "5399 23** **** 3782"
    expiry_month = models.CharField(max_length=2, blank=True)
    expiry_year = models.CharField(max_length=4, blank=True)
    card_name = models.CharField(max_length=100, blank=True)
    balance = models.DecimalField(max_digits=20, decimal_places=2, default=Decimal("0.00"))
    status = models.CharField(max_length=20, choices=Status.choices, default=Status.ACTIVE)
    created_at = models.DateTimeField(auto_now_add=True)

    def __str__(self):
        return f"{self.user.username} — {self.masked_pan or self.flw_card_id}"

class VirtualAccount(models.Model):
    """
    A dedicated bank account number issued by Flutterwave for one
    user's wallet — money sent to this account is picked up by the
    webhook and credited automatically, no manual top-up needed.
    """

    user = models.OneToOneField(User, on_delete=models.CASCADE, related_name="virtual_account")
    wallet = models.ForeignKey(Wallet, on_delete=models.CASCADE, related_name="virtual_accounts")
    account_number = models.CharField(max_length=20, unique=True)
    bank_name = models.CharField(max_length=150)
    account_name = models.CharField(max_length=200)
    flw_reference = models.CharField(max_length=100, unique=True)
    order_ref = models.CharField(max_length=100, blank=True)
    is_active = models.BooleanField(default=True)
    created_at = models.DateTimeField(auto_now_add=True)

    def __str__(self):
        return f"{self.account_name} — {self.bank_name} ({self.account_number})"

# ============================================================
# MONETIZATION / GROWTH MODELS
# ============================================================

def generate_genz_reference():
    return generate_reference("GENZ")


def generate_revenue_reference():
    return generate_reference("REV")


class PheralSubscription(models.Model):
    class Plan(models.TextChoices):
        PRO = "pro", "Pheral Pro"

    class Status(models.TextChoices):
        ACTIVE = "active", "Active"
        EXPIRED = "expired", "Expired"
        CANCELLED = "cancelled", "Cancelled"
        PENDING = "pending", "Pending"

    user = models.OneToOneField(
        User,
        on_delete=models.CASCADE,
        related_name="pheral_subscription",
    )

    plan = models.CharField(
        max_length=30,
        choices=Plan.choices,
        default=Plan.PRO,
    )

    status = models.CharField(
        max_length=20,
        choices=Status.choices,
        default=Status.PENDING,
        db_index=True,
    )

    price = models.DecimalField(
        max_digits=12,
        decimal_places=2,
    )

    currency = models.ForeignKey(
        Currency,
        on_delete=models.PROTECT,
        related_name="pheral_subscriptions",
    )

    started_at = models.DateTimeField(
        null=True,
        blank=True,
    )

    expires_at = models.DateTimeField(
        null=True,
        blank=True,
    )

    provider = models.CharField(
        max_length=50,
        blank=True,
    )

    provider_reference = models.CharField(
        max_length=255,
        blank=True,
        db_index=True,
    )

    created_at = models.DateTimeField(
        auto_now_add=True,
    )

    updated_at = models.DateTimeField(
        auto_now=True,
    )

    @property
    def is_active(self):
        return (
            self.status == self.Status.ACTIVE
            and self.expires_at is not None
            and self.expires_at > timezone.now()
        )

    def __str__(self):
        return f"{self.user} — {self.plan} — {self.status}"


class GenZBadge(models.Model):
    class Status(models.TextChoices):
        ACTIVE = "active", "Active"
        EXPIRED = "expired", "Expired"
        CANCELLED = "cancelled", "Cancelled"

    user = models.OneToOneField(
        User,
        on_delete=models.CASCADE,
        related_name="genz_badge",
    )

    status = models.CharField(
        max_length=20,
        choices=Status.choices,
        default=Status.ACTIVE,
        db_index=True,
    )

    price = models.DecimalField(
        max_digits=12,
        decimal_places=2,
        default=Decimal("0.00"),
    )

    currency = models.ForeignKey(
        Currency,
        on_delete=models.PROTECT,
        related_name="genz_badges",
    )

    reference = models.CharField(
        max_length=30,
        unique=True,
        default=generate_genz_reference,
        editable=False,
    )

    activated_at = models.DateTimeField(
        auto_now_add=True,
    )

    expires_at = models.DateTimeField(
        null=True,
        blank=True,
    )

    def __str__(self):
        return f"{self.user} — GenZ Badge — {self.status}"


class AIUsage(models.Model):
    user = models.ForeignKey(
        User,
        on_delete=models.CASCADE,
        related_name="ai_usage",
    )

    date = models.DateField(
        default=timezone.localdate,
        db_index=True,
    )

    requests = models.PositiveIntegerField(
        default=0,
    )

    input_tokens = models.PositiveBigIntegerField(
        default=0,
    )

    output_tokens = models.PositiveBigIntegerField(
        default=0,
    )

    credits_used = models.DecimalField(
        max_digits=14,
        decimal_places=4,
        default=Decimal("0.0000"),
    )

    class Meta:
        constraints = [
            models.UniqueConstraint(
                fields=["user", "date"],
                name="unique_user_ai_usage_per_day",
            ),
        ]

    def __str__(self):
        return f"{self.user} — {self.date}"


class AICreditBalance(models.Model):
    user = models.OneToOneField(
        User,
        on_delete=models.CASCADE,
        related_name="ai_credit_balance",
    )

    credits = models.DecimalField(
        max_digits=14,
        decimal_places=2,
        default=Decimal("0.00"),
    )

    lifetime_purchased = models.DecimalField(
        max_digits=14,
        decimal_places=2,
        default=Decimal("0.00"),
    )

    lifetime_used = models.DecimalField(
        max_digits=14,
        decimal_places=2,
        default=Decimal("0.00"),
    )

    updated_at = models.DateTimeField(
        auto_now=True,
    )

    def __str__(self):
        return f"{self.user} — {self.credits} AI credits"


class SponsoredPost(models.Model):
    class Status(models.TextChoices):
        DRAFT = "draft", "Draft"
        PENDING = "pending", "Pending Review"
        ACTIVE = "active", "Active"
        PAUSED = "paused", "Paused"
        COMPLETED = "completed", "Completed"
        REJECTED = "rejected", "Rejected"

    advertiser = models.ForeignKey(
        User,
        on_delete=models.CASCADE,
        related_name="sponsored_posts",
    )

    post = models.OneToOneField(
        Post,
        on_delete=models.CASCADE,
        related_name="sponsorship",
    )

    budget = models.DecimalField(
        max_digits=14,
        decimal_places=2,
        validators=[
            MinValueValidator(Decimal("0.01")),
        ],
    )

    spent = models.DecimalField(
        max_digits=14,
        decimal_places=2,
        default=Decimal("0.00"),
    )

    currency = models.ForeignKey(
        Currency,
        on_delete=models.PROTECT,
        related_name="sponsored_posts",
    )

    impressions = models.PositiveBigIntegerField(
        default=0,
    )

    clicks = models.PositiveBigIntegerField(
        default=0,
    )

    status = models.CharField(
        max_length=20,
        choices=Status.choices,
        default=Status.DRAFT,
        db_index=True,
    )

    starts_at = models.DateTimeField(
        null=True,
        blank=True,
    )

    ends_at = models.DateTimeField(
        null=True,
        blank=True,
    )

    created_at = models.DateTimeField(
        auto_now_add=True,
    )

    updated_at = models.DateTimeField(
        auto_now=True,
    )

    def __str__(self):
        return f"{self.advertiser} — Sponsored Post — {self.status}"


class RevenueRecord(models.Model):
    class RevenueType(models.TextChoices):
        PAYMENT_FEE = "payment_fee", "Payment Fee"
        WITHDRAWAL_FEE = "withdrawal_fee", "Withdrawal Fee"
        FX_MARGIN = "fx_margin", "FX Margin"
        CARD_FEE = "card_fee", "Virtual Card Fee"
        BILL_COMMISSION = "bill_commission", "Bill Commission"
        JOB_FEE = "job_fee", "Job Fee"
        SPONSORED_POST = "sponsored_post", "Sponsored Post"
        GENZ_BADGE = "genz_badge", "GenZ Badge"
        PRO_SUBSCRIPTION = "pro_subscription", "Pheral Pro"
        AI = "ai", "AI"
        FLOAT = "float", "Float Income"
        OTHER = "other", "Other"

    reference = models.CharField(
        max_length=30,
        unique=True,
        default=generate_revenue_reference,
        editable=False,
    )

    user = models.ForeignKey(
        User,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="revenue_records",
    )

    revenue_type = models.CharField(
        max_length=30,
        choices=RevenueType.choices,
        db_index=True,
    )

    amount = models.DecimalField(
        max_digits=20,
        decimal_places=2,
        validators=[
            MinValueValidator(Decimal("0.00")),
        ],
    )

    currency = models.ForeignKey(
        Currency,
        on_delete=models.PROTECT,
        related_name="revenue_records",
    )

    transaction = models.ForeignKey(
        PheralTransaction,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="revenue_records",
    )

    description = models.CharField(
        max_length=255,
        blank=True,
    )

    metadata = models.JSONField(
        default=dict,
        blank=True,
    )

    created_at = models.DateTimeField(
        auto_now_add=True,
        db_index=True,
    )

    def __str__(self):
        return f"{self.reference} — {self.revenue_type} — {self.amount}"


class WaitlistEntry(models.Model):
    """One person on the private-beta waitlist. `contact` is a lower-cased
    email or an E.164 phone number, so duplicates are caught reliably."""
 
    class Kind(models.TextChoices):
        EMAIL = "email", "Email"
        PHONE = "phone", "Phone"
 
    contact = models.CharField(max_length=254, unique=True)
    kind = models.CharField(max_length=10, choices=Kind.choices)
    source = models.CharField(max_length=50, blank=True)   # from ?src=tiktok etc.
    invited = models.BooleanField(default=False, db_index=True)
    created_at = models.DateTimeField(auto_now_add=True, db_index=True)
 
    class Meta:
        ordering = ["created_at"]
 
    def __str__(self):
        return self.contact