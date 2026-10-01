
from django.contrib import admin

from .models import (
    User,
    PhoneOTP,
    Currency,
    ExchangeRate,
    Wallet,
    WalletToken,
    BankAccount,
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
    NetworkProvider,
    VirtualCard,
    VirtualAccount,

    # Monetization / Growth
    PheralSubscription,
    GenZBadge,
    AIUsage,
    AICreditBalance,
    SponsoredPost,
    RevenueRecord,
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
# BANK ACCOUNT
# ============================================================

@admin.register(BankAccount)
class BankAccountAdmin(admin.ModelAdmin):
    list_display = (
        "user",
        "bank_name",
        "account_number",
        "account_name",
        "created_at",
    )

    search_fields = (
        "user__username",
        "user__phone_number",
        "bank_name",
        "account_number",
        "account_name",
    )

    list_filter = (
        "bank_name",
    )

    readonly_fields = (
        "created_at",
    )

    ordering = ("-created_at",)


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
# VIRTUAL CARD
# ============================================================

@admin.register(VirtualCard)
class VirtualCardAdmin(admin.ModelAdmin):
    list_display = (
        "user",
        "card_name",
        "currency",
        "masked_pan",
        "expiry_month",
        "expiry_year",
        "balance",
        "status",
        "created_at",
    )

    search_fields = (
        "user__username",
        "user__phone_number",
        "card_name",
        "masked_pan",
        "flw_card_id",
    )

    list_filter = (
        "status",
        "currency",
        "created_at",
    )

    readonly_fields = (
        "flw_card_id",
        "masked_pan",
        "expiry_month",
        "expiry_year",
        "created_at",
    )

    ordering = ("-created_at",)


# ============================================================
# VIRTUAL ACCOUNT
# ============================================================

@admin.register(VirtualAccount)
class VirtualAccountAdmin(admin.ModelAdmin):
    list_display = (
        "user",
        "account_number",
        "bank_name",
        "account_name",
        "is_active",
        "created_at",
    )

    search_fields = (
        "user__username",
        "user__phone_number",
        "account_number",
        "bank_name",
        "account_name",
        "flw_reference",
        "order_ref",
    )

    list_filter = (
        "is_active",
        "bank_name",
        "created_at",
    )

    readonly_fields = (
        "account_number",
        "bank_name",
        "account_name",
        "flw_reference",
        "order_ref",
        "created_at",
    )

    ordering = ("-created_at",)


# ============================================================
# NETWORK PROVIDER
# ============================================================

@admin.register(NetworkProvider)
class NetworkProviderAdmin(admin.ModelAdmin):
    list_display = (
        "name",
        "code",
        "is_active",
    )

    search_fields = (
        "name",
        "code",
    )

    list_filter = (
        "is_active",
    )

    ordering = ("name",)


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
        if not obj.content:
            return "—"
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
        if not obj.command:
            return "—"
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


# ============================================================
# PHERAL PRO SUBSCRIPTION
# ============================================================

@admin.register(PheralSubscription)
class PheralSubscriptionAdmin(admin.ModelAdmin):
    list_display = (
        "user",
        "plan",
        "status",
        "price",
        "currency",
        "started_at",
        "expires_at",
        "provider",
        "created_at",
    )

    search_fields = (
        "user__username",
        "user__phone_number",
        "provider_reference",
    )

    list_filter = (
        "plan",
        "status",
        "currency",
        "provider",
    )

    readonly_fields = (
        "created_at",
        "updated_at",
    )

    ordering = ("-created_at",)


# ============================================================
# GENZ BADGE
# ============================================================

@admin.register(GenZBadge)
class GenZBadgeAdmin(admin.ModelAdmin):
    list_display = (
        "user",
        "reference",
        "status",
        "price",
        "currency",
        "activated_at",
        "expires_at",
    )

    search_fields = (
        "user__username",
        "user__phone_number",
        "reference",
    )

    list_filter = (
        "status",
        "currency",
    )

    readonly_fields = (
        "reference",
        "activated_at",
    )

    ordering = ("-activated_at",)


# ============================================================
# AI USAGE
# ============================================================

@admin.register(AIUsage)
class AIUsageAdmin(admin.ModelAdmin):
    list_display = (
        "user",
        "date",
        "requests",
        "input_tokens",
        "output_tokens",
        "credits_used",
    )

    search_fields = (
        "user__username",
        "user__phone_number",
    )

    list_filter = (
        "date",
    )

    readonly_fields = (
        "date",
        "requests",
        "input_tokens",
        "output_tokens",
        "credits_used",
    )

    ordering = (
        "-date",
        "-requests",
    )


# ============================================================
# AI CREDIT BALANCE
# ============================================================

@admin.register(AICreditBalance)
class AICreditBalanceAdmin(admin.ModelAdmin):
    list_display = (
        "user",
        "credits",
        "lifetime_purchased",
        "lifetime_used",
        "updated_at",
    )

    search_fields = (
        "user__username",
        "user__phone_number",
    )

    readonly_fields = (
        "updated_at",
    )

    ordering = ("-updated_at",)


# ============================================================
# SPONSORED POST
# ============================================================

@admin.register(SponsoredPost)
class SponsoredPostAdmin(admin.ModelAdmin):
    list_display = (
        "advertiser",
        "post",
        "budget",
        "spent",
        "currency",
        "impressions",
        "clicks",
        "status",
        "starts_at",
        "ends_at",
        "created_at",
    )

    search_fields = (
        "advertiser__username",
        "advertiser__phone_number",
        "post__content",
    )

    list_filter = (
        "status",
        "currency",
        "created_at",
    )

    readonly_fields = (
        "spent",
        "impressions",
        "clicks",
        "created_at",
        "updated_at",
    )

    ordering = ("-created_at",)


# ============================================================
# REVENUE RECORD
# ============================================================

@admin.register(RevenueRecord)
class RevenueRecordAdmin(admin.ModelAdmin):
    list_display = (
        "reference",
        "revenue_type",
        "amount",
        "currency",
        "user",
        "transaction",
        "created_at",
    )

    search_fields = (
        "reference",
        "user__username",
        "user__phone_number",
        "transaction__reference",
        "description",
    )

    list_filter = (
        "revenue_type",
        "currency",
        "created_at",
    )

    readonly_fields = (
        "reference",
        "created_at",
    )

    ordering = ("-created_at",)


import csv
 
from django.contrib import admin
from django.http import HttpResponse
 
from .models import WaitlistEntry
 
 
def _csv_safe(value):
    """Stop spreadsheet formula injection in exported cells."""
    value = str(value)
    return "'" + value if value[:1] in ("=", "+", "-", "@") else value
 
 
@admin.register(WaitlistEntry)
class WaitlistEntryAdmin(admin.ModelAdmin):
    list_display = ("contact", "kind", "source", "invited", "created_at")
    list_filter = ("kind", "invited", "source")
    search_fields = ("contact",)
    actions = ["mark_invited", "export_csv"]
 
    @admin.action(description="Mark selected as invited")
    def mark_invited(self, request, queryset):
        queryset.update(invited=True)
 
    @admin.action(description="Export selected to CSV")
    def export_csv(self, request, queryset):
        response = HttpResponse(content_type="text/csv")
        response["Content-Disposition"] = 'attachment; filename="pheral-waitlist.csv"'
        writer = csv.writer(response)
        writer.writerow(["contact", "kind", "source", "invited", "created_at"])
        for e in queryset:
            writer.writerow([_csv_safe(e.contact), e.kind, _csv_safe(e.source), e.invited, e.created_at.isoformat()])
        return response
 