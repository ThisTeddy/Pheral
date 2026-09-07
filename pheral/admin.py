
from django.contrib import admin

from .models import (
    User,
    PhoneOTP,
    Currency,
    ExchangeRate,
    Wallet,
    WalletToken,
    PheralTransaction,
    LedgerEntry,
    Receipt,
    Conversation,
    ConversationParticipant,
    Message,
    MessageRead,
    GroupLedger,
    GroupLedgerEntry,
    Status,
    StatusView,
    Post,
    PostLike,
    PostComment,
    HireJob,
    HireRequest,
    HirePayment,
    Notification,
    PushDevice,
    AgentCommand,
    AgentActivity,
    Contact,
)


# ============================================================
# USER
# ============================================================

@admin.register(User)
class UserAdmin(admin.ModelAdmin):
    list_display = (
        "username",
        "phone_number",
        "first_name",
        "last_name",
        "is_phone_verified",
        "is_active_user",
        "is_staff",
        "last_seen",
        "created_at",
    )

    search_fields = (
        "username",
        "phone_number",
        "first_name",
        "last_name",
        "email",
    )

    list_filter = (
        "is_phone_verified",
        "is_active_user",
        "is_staff",
        "is_superuser",
        "date_joined",
    )

    readonly_fields = (
        "created_at",
        "updated_at",
        "last_seen",
    )

    ordering = ("-created_at",)


# ============================================================
# PHONE OTP
# ============================================================

@admin.register(PhoneOTP)
class PhoneOTPAdmin(admin.ModelAdmin):
    list_display = (
        "phone_number",
        "user",
        "code",
        "is_used",
        "attempts",
        "expires_at",
        "created_at",
    )

    search_fields = (
        "phone_number",
        "user__username",
    )

    list_filter = (
        "is_used",
        "created_at",
    )

    readonly_fields = (
        "created_at",
    )

    ordering = ("-created_at",)


# ============================================================
# CURRENCY
# ============================================================

@admin.register(Currency)
class CurrencyAdmin(admin.ModelAdmin):
    list_display = (
        "code",
        "name",
        "symbol",
        "decimal_places",
        "is_active",
        "created_at",
    )

    search_fields = (
        "code",
        "name",
        "symbol",
    )

    list_filter = (
        "is_active",
    )

    readonly_fields = (
        "created_at",
    )

    ordering = ("code",)


# ============================================================
# EXCHANGE RATE
# ============================================================

@admin.register(ExchangeRate)
class ExchangeRateAdmin(admin.ModelAdmin):
    list_display = (
        "source_currency",
        "target_currency",
        "rate",
        "is_active",
        "updated_at",
    )

    search_fields = (
        "source_currency__code",
        "target_currency__code",
    )

    list_filter = (
        "is_active",
        "source_currency",
        "target_currency",
    )

    readonly_fields = (
        "updated_at",
    )

    ordering = (
        "source_currency",
        "target_currency",
    )


# ============================================================
# WALLET
# ============================================================

@admin.register(Wallet)
class WalletAdmin(admin.ModelAdmin):
    list_display = (
        "user",
        "currency",
        "balance",
        "is_active",
        "created_at",
        "updated_at",
    )

    search_fields = (
        "user__username",
        "user__phone_number",
        "currency__code",
    )

    list_filter = (
        "currency",
        "is_active",
    )

    readonly_fields = (
        "created_at",
        "updated_at",
    )

    ordering = ("-updated_at",)


# ============================================================
# WALLET TOKEN
# ============================================================

@admin.register(WalletToken)
class WalletTokenAdmin(admin.ModelAdmin):
    list_display = (
        "user",
        "token",
        "is_active",
        "issued_at",
        "last_used_at",
    )

    search_fields = (
        "user__username",
        "user__phone_number",
        "token",
    )

    list_filter = (
        "is_active",
    )

    readonly_fields = (
        "token",
        "issued_at",
        "last_used_at",
    )

    ordering = ("-issued_at",)


# ============================================================
# PHERAL TRANSACTION
# ============================================================

@admin.register(PheralTransaction)
class PheralTransactionAdmin(admin.ModelAdmin):
    list_display = (
        "reference",
        "transaction_type",
        "sender",
        "recipient",
        "amount",
        "currency",
        "fee",
        "status",
        "created_at",
        "completed_at",
    )

    search_fields = (
        "reference",
        "sender__username",
        "recipient__username",
        "external_reference",
        "description",
    )

    list_filter = (
        "transaction_type",
        "status",
        "currency",
        "created_at",
    )

    readonly_fields = (
        "reference",
        "created_at",
        "completed_at",
    )

    ordering = ("-created_at",)


# ============================================================
# LEDGER ENTRY
# ============================================================

@admin.register(LedgerEntry)
class LedgerEntryAdmin(admin.ModelAdmin):
    list_display = (
        "transaction",
        "wallet",
        "entry_type",
        "amount",
        "balance_before",
        "balance_after",
        "created_at",
    )

    search_fields = (
        "transaction__reference",
        "wallet__user__username",
        "description",
    )

    list_filter = (
        "entry_type",
        "created_at",
    )

    readonly_fields = (
        "created_at",
    )

    ordering = ("-created_at",)


# ============================================================
# RECEIPT
# ============================================================

@admin.register(Receipt)
class ReceiptAdmin(admin.ModelAdmin):
    list_display = (
        "reference",
        "transaction",
        "payer",
        "recipient",
        "amount",
        "currency",
        "created_at",
    )

    search_fields = (
        "reference",
        "transaction__reference",
        "payer__username",
        "recipient__username",
        "description",
    )

    list_filter = (
        "currency",
        "created_at",
    )

    readonly_fields = (
        "reference",
        "created_at",
    )

    ordering = ("-created_at",)


# ============================================================
# CONVERSATION
# ============================================================

@admin.register(Conversation)
class ConversationAdmin(admin.ModelAdmin):
    list_display = (
        "id",
        "conversation_type",
        "name",
        "created_by",
        "is_active",
        "created_at",
        "updated_at",
    )

    search_fields = (
        "name",
        "description",
        "created_by__username",
    )

    list_filter = (
        "conversation_type",
        "is_active",
        "created_at",
    )

    readonly_fields = (
        "created_at",
        "updated_at",
    )

    ordering = ("-updated_at",)


# ============================================================
# CONVERSATION PARTICIPANT
# ============================================================

@admin.register(ConversationParticipant)
class ConversationParticipantAdmin(admin.ModelAdmin):
    list_display = (
        "conversation",
        "user",
        "is_admin",
        "is_muted",
        "is_archived",
        "joined_at",
        "last_read_at",
    )

    search_fields = (
        "user__username",
        "conversation__name",
    )

    list_filter = (
        "is_admin",
        "is_muted",
        "is_archived",
    )

    readonly_fields = (
        "joined_at",
    )

    ordering = ("-joined_at",)


# ============================================================
# MESSAGE
# ============================================================

@admin.register(Message)
class MessageAdmin(admin.ModelAdmin):
    list_display = (
        "id",
        "conversation",
        "sender",
        "message_type",
        "short_content",
        "transaction",
        "receipt",
        "is_deleted",
        "created_at",
    )

    search_fields = (
        "content",
        "sender__username",
        "conversation__name",
        "transaction__reference",
        "receipt__reference",
    )

    list_filter = (
        "message_type",
        "is_deleted",
        "created_at",
    )

    readonly_fields = (
        "created_at",
        "updated_at",
    )

    ordering = ("-created_at",)

    @admin.display(description="Content")
    def short_content(self, obj):
        if not obj.content:
            return "—"

        return obj.content[:80]


# ============================================================
# MESSAGE READ
# ============================================================

@admin.register(MessageRead)
class MessageReadAdmin(admin.ModelAdmin):
    list_display = (
        "message",
        "user",
        "read_at",
    )

    search_fields = (
        "user__username",
        "message__content",
    )

    readonly_fields = (
        "read_at",
    )

    ordering = ("-read_at",)


# ============================================================
# GROUP LEDGER
# ============================================================

@admin.register(GroupLedger)
class GroupLedgerAdmin(admin.ModelAdmin):
    list_display = (
        "conversation",
        "currency",
        "balance",
        "withdrawal_fee",
        "created_at",
        "updated_at",
    )

    search_fields = (
        "conversation__name",
        "currency__code",
    )

    list_filter = (
        "currency",
    )

    readonly_fields = (
        "created_at",
        "updated_at",
    )

    ordering = ("-updated_at",)


# ============================================================
# GROUP LEDGER ENTRY
# ============================================================

@admin.register(GroupLedgerEntry)
class GroupLedgerEntryAdmin(admin.ModelAdmin):
    list_display = (
        "reference",
        "ledger",
        "user",
        "entry_type",
        "amount",
        "created_at",
    )

    search_fields = (
        "reference",
        "user__username",
        "description",
    )

    list_filter = (
        "entry_type",
        "created_at",
    )

    readonly_fields = (
        "reference",
        "created_at",
    )

    ordering = ("-created_at",)


# ============================================================
# STATUS
# ============================================================

@admin.register(Status)
class StatusAdmin(admin.ModelAdmin):
    list_display = (
        "user",
        "status_type",
        "short_text",
        "expires_at",
        "is_active",
        "created_at",
    )

    search_fields = (
        "user__username",
        "text",
    )

    list_filter = (
        "status_type",
        "is_active",
        "created_at",
        "expires_at",
    )

    readonly_fields = (
        "created_at",
    )

    ordering = ("-created_at",)

    @admin.display(description="Text")
    def short_text(self, obj):
        if not obj.text:
            return "—"

        return obj.text[:80]


# ============================================================
# STATUS VIEW
# ============================================================

@admin.register(StatusView)
class StatusViewAdmin(admin.ModelAdmin):
    list_display = (
        "status",
        "viewer",
        "viewed_at",
    )

    search_fields = (
        "viewer__username",
        "status__user__username",
    )

    readonly_fields = (
        "viewed_at",
    )

    ordering = ("-viewed_at",)


# ============================================================
# POST
# ============================================================

@admin.register(Post)
class PostAdmin(admin.ModelAdmin):
    list_display = (
        "id",
        "author",
        "short_content",
        "created_at",
        "updated_at",
        "is_deleted",
    )

    search_fields = (
        "content",
        "author__username",
    )

    list_filter = (
        "is_deleted",
        "created_at",
    )

    readonly_fields = (
        "created_at",
        "updated_at",
    )

    ordering = ("-created_at",)

    @admin.display(description="Content")
    def short_content(self, obj):
        if not obj.content:
            return "—"

        return obj.content[:100]


# ============================================================
# POST LIKE
# ============================================================

@admin.register(PostLike)
class PostLikeAdmin(admin.ModelAdmin):
    list_display = (
        "post",
        "user",
        "created_at",
    )

    search_fields = (
        "user__username",
        "post__content",
    )

    readonly_fields = (
        "created_at",
    )

    ordering = ("-created_at",)


# ============================================================
# POST COMMENT
# ============================================================

@admin.register(PostComment)
class PostCommentAdmin(admin.ModelAdmin):
    list_display = (
        "post",
        "user",
        "short_content",
        "created_at",
        "updated_at",
    )

    search_fields = (
        "content",
        "user__username",
        "post__content",
    )

    readonly_fields = (
        "created_at",
        "updated_at",
    )

    ordering = ("-created_at",)

    @admin.display(description="Comment")
    def short_content(self, obj):
        return obj.content[:100]


# ============================================================
# HIRE JOB
# ============================================================

@admin.register(HireJob)
class HireJobAdmin(admin.ModelAdmin):
    list_display = (
        "title",
        "employer",
        "budget",
        "currency",
        "status",
        "created_at",
        "updated_at",
    )

    search_fields = (
        "title",
        "description",
        "employer__username",
    )

    list_filter = (
        "status",
        "currency",
        "created_at",
    )

    readonly_fields = (
        "created_at",
        "updated_at",
    )

    ordering = ("-created_at",)


# ============================================================
# HIRE REQUEST
# ============================================================

@admin.register(HireRequest)
class HireRequestAdmin(admin.ModelAdmin):
    list_display = (
        "job",
        "requester",
        "worker",
        "proposed_amount",
        "status",
        "created_at",
        "updated_at",
    )

    search_fields = (
        "job__title",
        "requester__username",
        "worker__username",
        "message",
    )

    list_filter = (
        "status",
        "created_at",
    )

    readonly_fields = (
        "created_at",
        "updated_at",
    )

    ordering = ("-created_at",)


# ============================================================
# HIRE PAYMENT
# ============================================================

@admin.register(HirePayment)
class HirePaymentAdmin(admin.ModelAdmin):
    list_display = (
        "hire_request",
        "transaction",
        "created_at",
    )

    search_fields = (
        "hire_request__job__title",
        "hire_request__requester__username",
        "hire_request__worker__username",
        "transaction__reference",
    )

    readonly_fields = (
        "created_at",
    )

    ordering = ("-created_at",)


# ============================================================
# NOTIFICATION
# ============================================================

@admin.register(Notification)
class NotificationAdmin(admin.ModelAdmin):
    list_display = (
        "user",
        "notification_type",
        "title",
        "is_read",
        "created_at",
    )

    search_fields = (
        "user__username",
        "title",
        "body",
    )

    list_filter = (
        "notification_type",
        "is_read",
        "created_at",
    )

    readonly_fields = (
        "created_at",
    )

    ordering = ("-created_at",)


# ============================================================
# PUSH DEVICE
# ============================================================

@admin.register(PushDevice)
class PushDeviceAdmin(admin.ModelAdmin):
    list_display = (
        "user",
        "platform",
        "token_preview",
        "is_active",
        "created_at",
        "updated_at",
    )

    search_fields = (
        "user__username",
        "user__phone_number",
        "token",
    )

    list_filter = (
        "platform",
        "is_active",
    )

    readonly_fields = (
        "created_at",
        "updated_at",
    )

    ordering = ("-updated_at",)

    @admin.display(description="Token")
    def token_preview(self, obj):
        if not obj.token:
            return "—"

        return f"{obj.token[:20]}..."


# ============================================================
# AGENT COMMAND
# ============================================================

@admin.register(AgentCommand)
class AgentCommandAdmin(admin.ModelAdmin):
    list_display = (
        "id",
        "user",
        "short_command",
        "action",
        "status",
        "created_at",
        "completed_at",
    )

    search_fields = (
        "user__username",
        "command",
        "action",
    )

    list_filter = (
        "status",
        "created_at",
    )

    readonly_fields = (
        "created_at",
        "completed_at",
    )

    ordering = ("-created_at",)

    @admin.display(description="Command")
    def short_command(self, obj):
        return obj.command[:100]


# ============================================================
# AGENT ACTIVITY
# ============================================================

@admin.register(AgentActivity)
class AgentActivityAdmin(admin.ModelAdmin):
    list_display = (
        "user",
        "command",
        "action",
        "conversation",
        "transaction",
        "created_at",
    )

    search_fields = (
        "user__username",
        "action",
        "command__command",
        "transaction__reference",
    )

    list_filter = (
        "action",
        "created_at",
    )

    readonly_fields = (
        "created_at",
    )

    ordering = ("-created_at",)


# ============================================================
# CONTACT
# ============================================================

@admin.register(Contact)
class ContactAdmin(admin.ModelAdmin):
    list_display = (
        "owner",
        "contact_user",
        "phone_number",
        "nickname",
        "created_at",
    )

    search_fields = (
        "owner__username",
        "contact_user__username",
        "phone_number",
        "nickname",
    )

    readonly_fields = (
        "created_at",
    )

    ordering = ("-created_at",)

# admin.py
from django.contrib import admin
from .models import Currency
