from django.contrib import admin
from django.urls import path

from . import views


urlpatterns = [

    # ============================================================
    # PUBLIC / LANDING
    # ============================================================

    path(
        "",
        views.landing,
        name="landing",
    ),


    # ============================================================
    # AUTHENTICATION
    # ============================================================

    path(
        "register/",
        views.register,
        name="register",
    ),

    path(
        "login/",
        views.login_view,
        name="login_view",
    ),

    path(
        "logout/",
        views.logout_view,
        name="logout",
    ),

    path(
        "verify-otp/",
        views.verify_otp,
        name="verify_otp",
    ),
    path(
        "resend-otp/",
        views.resend_otp,
        name="resend_otp",
    ),

    path(
        "forgot-password/",
        views.forgot_password,
        name="forgot_password",
    ),

    path(
        "verify-password-reset-otp/",
        views.verify_password_reset_otp,
        name="verify_password_reset_otp",
    ),

    path(
        "reset-password/",
        views.reset_password,
        name="reset_password",
    ),
    path(
        "resend-password-reset-otp/",
        views.resend_password_reset_otp,
        name="resend_password_reset_otp",
    ),

    # ============================================================
    # MAIN APP
    # ============================================================


    # ============================================================
    # PROFILE
    # (order matters here: literal "edit/" MUST come before the
    # <str:username> wildcard, or "/profile/edit/" gets swallowed
    # by profile_user with username="edit" and 404s.)
    # ============================================================

    path(
        "profile/",
        views.profile,
        name="profile",
    ),

    path(
        "profile/edit/",
        views.profile_edit,
        name="profile_edit",
    ),

    path(
        "profile/<str:username>/",
        views.profile,
        name="profile_user",
    ),


    # ============================================================
    # DIRECT CHAT
    # ============================================================

    path(
        "chat/",
        views.chat,
        name="chat",
    ),

    path(
        "chat/<int:conversation_id>/",
        views.chat,
        name="chat",
    ),

    path(
        "chat/user/<str:username>/",
        views.chat,
        name="chat_user",
    ),

    path(
        "chat/start/<str:username>/",
        views.start_chat,
        name="start_chat",
    ),

    path(
        "chat/<int:conversation_id>/read/",
        views.mark_chat_read,
        name="mark_chat_read",
    ),

    path(
        "chat/<int:conversation_id>/typing/",
        views.chat_typing,
        name="chat_typing",
    ),


    # ============================================================
    # GROUPS
    # ============================================================

    path(
        "groups/",
        views.group_list,
        name="group_list",
    ),

    path(
        "groups/create/",
        views.group_create,
        name="group_create",
    ),

    path(
        "groups/<int:conversation_id>/",
        views.group_chat,
        name="group_chat",
    ),

    path(
        "groups/<int:conversation_id>/add-member/",
        views.group_add_member,
        name="group_add_member",
    ),

    path(
        "groups/<int:conversation_id>/profile/",
        views.group_profile,
        name="group_profile",
    ),


    # ============================================================
    # WALLET
    # ============================================================

    path(
        "wallet/",
        views.wallet,
        name="wallet",
    ),

    path(
        "wallet/top-up/",
        views.top_up,
        name="top_up",
    ),

    path(
        "wallet/top-up/callback/",
        views.top_up_callback,
        name="top_up_callback",
    ),

    path(
        "wallet/withdraw/",
        views.withdraw,
        name="withdraw",
    ),


    # ============================================================
    # PAYMENTS
    # ============================================================

    path(
        "pay/<str:username>/",
        views.pay_user,
        name="pay_user",
    ),

    path(
        "global-pay/",
        views.global_pay,
        name="global_pay",
    ),

    path(
        "currency-converter/",
        views.currency_converter,
        name="currency_converter",
    ),

    path(
        "receipt/<str:reference>/",
        views.receipt_detail,
        name="receipt_detail",
    ),


    # ============================================================
    # BANK ACCOUNTS
    # ============================================================

    path(
        "bank-accounts/",
        views.bank_accounts,
        name="bank_accounts",
    ),

    path(
        "bank-accounts/add/",
        views.add_bank_account,
        name="add_bank_account",
    ),

    path(
        "bank-accounts/resolve/",
        views.resolve_bank_account,
        name="resolve_bank_account",
    ),


    # ============================================================
    # STATUS
    # ============================================================

    path(
        "status/",
        views.status_list,
        name="status_list",
    ),

    path(
        "status/create/",
        views.create_status,
        name="create_status",
    ),

    path(
        "status/<int:status_id>/",
        views.status_detail,
        name="status_detail",
    ),

    path(
        "status/<int:status_id>/delete/",
        views.delete_status,
        name="delete_status",
    ),


    # ============================================================
    # FEED
    # ============================================================

    path(
        "feed/",
        views.feed,
        name="feed",
    ),

    path(
        "feed/create/",
        views.create_post,
        name="create_post",
    ),

    path(
        "feed/<int:post_id>/like/",
        views.like_post,
        name="like_post",
    ),

    path(
        "feed/<int:post_id>/comment/",
        views.comment_post,
        name="comment_post",
    ),


    # ============================================================
    # HIRE
    # ============================================================

    path(
        "hire/",
        views.hire,
        name="hire",
    ),

    path(
        "hire/create/",
        views.create_hire_job,
        name="create_hire_job",
    ),

    path(
        "hire/job/<int:job_id>/",
        views.hire_job_detail,
        name="hire_job_detail",
    ),

    path(
        "hire/job/<int:job_id>/request/<str:username>/",
        views.send_hire_request,
        name="send_hire_request",
    ),


    # ============================================================
    # NOTIFICATIONS
    # ============================================================

    path(
        "notifications/",
        views.notifications,
        name="notifications",
    ),

    path(
        "notifications/<int:notification_id>/read/",
        views.mark_notification_read,
        name="mark_notification_read",
    ),

    path(
        "notifications/read-all/",
        views.mark_all_notifications_read,
        name="mark_all_notifications_read",
    ),


    # ============================================================
    # SEARCH / CONTACTS
    # ============================================================

    path(
        "search/",
        views.search,
        name="search",
    ),

    path(
        "contacts/",
        views.contacts,
        name="contacts",
    ),

    path(
        "contacts/add/<str:username>/",
        views.add_contact,
        name="add_contact",
    ),

    path(
        "contacts/sync/",
        views.sync_contacts,
        name="sync_contacts",
    ),

    path(
        "chat_list",
        views.chat_list,
        name="chat_list",
    ),

    path(
        "message/<int:message_id>/delete/",
        views.delete_message,
        name="delete_message",
    ),

    path(
        "presence/heartbeat/",
        views.presence_heartbeat,
        name="presence_heartbeat",
    ),


    # ============================================================
    # AGENT MODE
    # ============================================================

    path(
        "agent/",
        views.agent,
        name="agent",
    ),

    path(
        "agent/command/<int:command_id>/",
        views.agent_command,
        name="agent_command",
    ),


    # ============================================================
    # PUSH / PWA
    # ============================================================

    path(
        "push/register/",
        views.register_push_device,
        name="register_push_device",
    ),

    path(
        "manifest.json",
        views.pwa_manifest,
        name="pwa_manifest",
    ),

    path(
        "service-worker.js",
        views.service_worker,
        name="service_worker",
    ),


    # ============================================================
    # JSON / LOOKUP
    # ============================================================

    path(
        "api/users/",
        views.user_lookup,
        name="user_lookup",
    ),

    path(
        "api/messages/<int:message_id>/read/",
        views.mark_message_read,
        name="mark_message_read",
    ),


    # ============================================================
    # PAYSTACK WEBHOOK
    # ============================================================

    path(
    "flutterwave/webhook/",
    views.flutterwave_webhook,
    name="flutterwave_webhook",
    ),

    # ============================================================
    # DJANGO ADMIN
    # ============================================================

    path(
        "admin/",
        admin.site.urls,
    ),
]