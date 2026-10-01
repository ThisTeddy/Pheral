from django.urls import path

from . import views

urlpatterns = [
    # Public / landing
    path("", views.landing, name="landing"),

    # Authentication
    path("register/", views.register, name="register"),
    path("login/", views.login_view, name="login_view"),
    path("logout/", views.logout_view, name="logout"),
    path("verify-otp/", views.verify_otp, name="verify_otp"),
    path("resend-otp/", views.resend_otp, name="resend_otp"),
    path("forgot-password/", views.forgot_password, name="forgot_password"),
    path("verify-password-reset-otp/", views.verify_password_reset_otp, name="verify_password_reset_otp"),
    path("reset-password/", views.reset_password, name="reset_password"),
    path("resend-password-reset-otp/", views.resend_password_reset_otp, name="resend_password_reset_otp"),
    path("api/check-username/", views.check_username, name="check_username"),
    path("api/lookup-account/", views.lookup_account, name="lookup_account"),

    # Profile (literal "edit/" must come before the <username> wildcard)
    path("profile/", views.profile, name="profile"),
    path("profile/edit/", views.profile_edit, name="profile_edit"),
    path("profile/<str:username>/", views.profile, name="profile_user"),

    # Direct chat
    path("chat_list", views.chat_list, name="chat_list"),
    path("chat/", views.chat, name="chat"),
    path("chat/<int:conversation_id>/", views.chat, name="chat"),
    path("chat/user/<str:username>/", views.chat, name="chat_user"),
    path("chat/start/<str:username>/", views.start_chat, name="start_chat"),
    path("chat/bulk-action/", views.bulk_chat_action, name="bulk_chat_action"),
    path("chat/<int:conversation_id>/read/", views.mark_chat_read, name="mark_chat_read"),
    path("chat/<int:conversation_id>/typing/", views.chat_typing, name="chat_typing"),
    path("chat/<int:conversation_id>/toggle/<str:flag>/", views.toggle_chat_flag, name="toggle_chat_flag"),
    path("message/<int:message_id>/delete/", views.delete_message, name="delete_message"),
    path("presence/heartbeat/", views.presence_heartbeat, name="presence_heartbeat"),

    # Groups
    path("groups/", views.group_list, name="group_list"),
    path("groups/create/", views.group_create, name="group_create"),
    path("groups/<int:conversation_id>/", views.group_chat, name="group_chat"),
    path("groups/<int:conversation_id>/add-member/", views.group_add_member, name="group_add_member"),
    path("groups/<int:conversation_id>/profile/", views.group_profile, name="group_profile"),
    path("groups/<int:conversation_id>/contribute/", views.group_contribute, name="group_contribute"),
    path("groups/<int:conversation_id>/withdraw/", views.group_withdraw, name="group_withdraw"),
    path("groups/<int:conversation_id>/admin/<str:username>/", views.group_toggle_admin, name="group_toggle_admin"),
    path("groups/<int:conversation_id>/remove/<str:username>/", views.group_remove_member, name="group_remove_member"),
    path("groups/<int:conversation_id>/leave/", views.group_leave, name="group_leave"),

    # Wallet
    path("wallet/", views.wallet, name="wallet"),
    path("wallet/top-up/", views.top_up, name="top_up"),
    path("wallet/top-up/init/", views.init_top_up, name="init_top_up"),
    path("wallet/top-up/verify/", views.verify_top_up, name="verify_top_up"),
    path("wallet/top-up/callback/", views.top_up_callback, name="top_up_callback"),
    path("wallet/withdraw/", views.withdraw, name="withdraw"),
    path("wallet/withdraw/add-bank-account/", views.add_bank_account, name="add_bank_account"),
    path("wallet/withdraw/<str:reference>/sync/", views.sync_withdrawal_status, name="sync_withdrawal_status"),
    path("wallet/transfer/", views.transfer, name="transfer"),
    path("wallet/bills/<str:reference>/sync/", views.sync_bill_status, name="sync_bill_status"),

    # Cards / virtual account
    path("wallet/cards/", views.cards_and_accounts, name="cards_and_accounts"),
    path("wallet/cards/create/", views.create_virtual_card, name="create_virtual_card"),
    path("wallet/cards/<int:card_id>/toggle/", views.toggle_card_status, name="toggle_card_status"),
    path("wallet/cards/<int:card_id>/fund/", views.fund_virtual_card, name="fund_virtual_card"),
    path("wallet/cards/<int:card_id>/reveal/", views.reveal_card_details, name="reveal_card_details"),
    path("wallet/virtual-account/create/", views.create_virtual_account, name="create_virtual_account"),

    # Payments
    path("pay/<str:username>/", views.pay_user, name="pay_user"),
    path("global-pay/", views.global_pay, name="global_pay"),
    path("currency-converter/", views.currency_converter, name="currency_converter"),
    path("currency-converter/quote/", views.fx_quote, name="fx_quote"),
    path("currency-converter/convert/", views.fx_convert, name="fx_convert"),
    path("receipt/<str:reference>/", views.receipt_detail, name="receipt_detail"),

    # Bank accounts
    path("bank-accounts/", views.bank_accounts, name="bank_accounts"),
    path("bank-accounts/add/", views.add_bank_account, name="add_bank_account_page"),
    path("bank-accounts/resolve/", views.resolve_bank_account, name="resolve_bank_account"),

    # Airtime / data
    path("airtime/", views.airtime_purchase, name="airtime_purchase"),
    path("data/", views.data_purchase, name="data_purchase"),
    path("api/data-plans/<int:network_id>/", views.data_plans_api, name="data_plans_api"),

    # Status
    path("status/", views.status_list, name="status_list"),
    path("status/create/", views.create_status, name="create_status"),
    path("status/<int:status_id>/", views.status_detail, name="status_detail"),
    path("status/<int:status_id>/delete/", views.delete_status, name="delete_status"),

    # Feed
    path("feed/", views.feed, name="feed"),
    path("feed/create/", views.create_post, name="create_post"),
    path("feed/<int:post_id>/like/", views.like_post, name="like_post"),
    path("feed/<int:post_id>/comment/", views.comment_post, name="comment_post"),

    # Hire
    path("hire/", views.hire, name="hire"),
    path("hire/create/", views.create_hire_job, name="create_hire_job"),
    path("hire/job/<int:job_id>/", views.hire_job_detail, name="hire_job_detail"),
    path("hire/job/<int:job_id>/request/<str:username>/", views.send_hire_request, name="send_hire_request"),
    path("hire/job/<int:job_id>/apply/", views.apply_to_hire_job, name="apply_to_hire_job"),
    path("hire/job/<int:job_id>/close/", views.close_hire_job, name="close_hire_job"),
    path("hire/request/<int:request_id>/respond/<str:action>/", views.respond_hire_request, name="respond_hire_request"),
    path("hire/request/<int:request_id>/cancel/", views.cancel_hire_request, name="cancel_hire_request"),
    path("hire/request/<int:request_id>/complete/", views.complete_hire_request, name="complete_hire_request"),

    # Notifications
    path("notifications/", views.notifications, name="notifications"),
    path("notifications/read-all/", views.mark_all_notifications_read, name="mark_all_notifications_read"),
    path("notifications/<int:notification_id>/read/", views.mark_notification_read, name="mark_notification_read"),

    # Search / contacts
    path("search/", views.search, name="search"),
    path("contacts/", views.contacts, name="contacts"),
    path("contacts/add/<str:username>/", views.add_contact, name="add_contact"),
    path("contacts/sync/", views.sync_contacts, name="sync_contacts"),

    # Agent
    path("agent/", views.agent, name="agent"),
    path("agent/command/<int:command_id>/", views.agent_command, name="agent_command"),

    # Push / PWA
    path("push/register/", views.register_push_device, name="register_push_device"),
    path("manifest.json", views.pwa_manifest, name="pwa_manifest"),
    path("service-worker.js", views.service_worker, name="service_worker"),

    # JSON
    path("api/users/", views.user_lookup, name="user_lookup"),
    path("api/messages/<int:message_id>/read/", views.mark_message_read, name="mark_message_read"),

    # Flutterwave webhook (csrf-exempt, signature-checked)
    path("flutterwave/webhook/", views.flutterwave_webhook, name="flutterwave_webhook"),
]