from django.contrib import admin
from .models import Account, KYC, Transaction, PaymentRequest, Notification, CreditCard


@admin.register(Account)
class AccountAdmin(admin.ModelAdmin):
    list_display = ['account_number', 'user', 'account_type', 'account_balance', 'is_active', 'created_at']
    list_filter = ['account_type', 'is_active', 'created_at']
    search_fields = ['account_number', 'user__email', 'user__first_name', 'user__last_name']
    readonly_fields = ['account_number', 'created_at', 'updated_at']
    ordering = ['-created_at']
    
    fieldsets = (
        ('Account Information', {
            'fields': ('user', 'account_number', 'account_type', 'account_balance', 'is_active')
        }),
        ('Timestamps', {
            'fields': ('created_at', 'updated_at'),
            'classes': ('collapse',)
        }),
    )


@admin.register(KYC)
class KYCAdmin(admin.ModelAdmin):
    list_display = ['user', 'full_name', 'verification_status', 'ai_verification_status', 'verification_confidence', 'document_verified', 'face_verified', 'ai_flagged', 'submitted_at']
    list_filter = ['verification_status', 'document_verified', 'face_verified', 'ai_verification_status', 'ai_flagged', 'submitted_at']
    search_fields = ['user__email', 'full_name', 'nationality', 'city', 'country']
    readonly_fields = ['submitted_at', 'verified_at', 'updated_at', 'ai_checked_at', 'ai_summary', 'ai_report']
    ordering = ['-submitted_at']
    actions = ['rerun_ai_checks']

    @admin.display(description='Findings')
    def ai_summary(self, obj):
        from django.utils.html import format_html, format_html_join
        reasons = (obj.ai_report or {}).get('reasons') or []
        if not reasons:
            return 'Not checked yet' if not obj.ai_checked_at else 'No issues found'
        return format_html('<ul style="margin:0;padding-left:1.1em">{}</ul>',
                           format_html_join('', '<li>{}</li>', ((r,) for r in reasons)))

    @admin.action(description='Re-run automated document and face checks')
    def rerun_ai_checks(self, request, queryset):
        from risk.kyc import run_checks
        for kyc in queryset:
            run_checks(kyc)
        self.message_user(request, f'Checks re-run for {queryset.count()} submission(s).')
    
    fieldsets = (
        ('User Information', {
            'fields': ('user', 'full_name', 'date_of_birth', 'nationality')
        }),
        ('Address Information', {
            'fields': ('address', 'city', 'state', 'country', 'postal_code')
        }),
        ('Documents', {
            'fields': ('id_document', 'proof_of_address', 'selfie_photo')
        }),
        ('Verification Status', {
            'fields': ('verification_status', 'document_verified', 'face_verified', 'verification_confidence')
        }),
        ('Automated checks', {
            'description': 'Filled in by the automated document and face checks. A person still makes the final decision.',
            'fields': ('ai_verification_status', 'ai_flagged', 'risk_score', 'ai_checked_at', 'ai_summary', 'ai_report'),
        }),
        ('Timestamps', {
            'fields': ('submitted_at', 'verified_at', 'updated_at'),
            'classes': ('collapse',)
        }),
    )


@admin.register(Transaction)
class TransactionAdmin(admin.ModelAdmin):
    list_display = ['transaction_id', 'transaction_type', 'amount', 'currency', 'sender', 'receiver', 'status', 'created_at']
    list_filter = ['transaction_type', 'status', 'currency', 'created_at']
    search_fields = ['transaction_id', 'sender__email', 'receiver__email', 'description', 'reference']
    readonly_fields = ['transaction_id', 'created_at', 'updated_at', 'completed_at']
    ordering = ['-created_at']
    
    fieldsets = (
        ('Transaction Details', {
            'fields': ('transaction_id', 'transaction_type', 'amount', 'currency', 'description', 'reference')
        }),
        ('Users', {
            'fields': ('sender', 'receiver', 'sender_account', 'receiver_account')
        }),
        ('Status & Fees', {
            'fields': ('status', 'transaction_fee', 'total_amount')
        }),
        ('Security', {
            'fields': ('is_verified', 'verification_code'),
            'classes': ('collapse',)
        }),
        ('Timestamps', {
            'fields': ('created_at', 'updated_at', 'completed_at'),
            'classes': ('collapse',)
        }),
    )


@admin.register(PaymentRequest)
class PaymentRequestAdmin(admin.ModelAdmin):
    list_display = ['request_id', 'amount', 'currency', 'requester', 'recipient', 'status', 'created_at']
    list_filter = ['status', 'currency', 'created_at']
    search_fields = ['request_id', 'requester__email', 'recipient__email', 'description']
    readonly_fields = ['request_id', 'created_at', 'updated_at', 'paid_at']
    ordering = ['-created_at']
    
    fieldsets = (
        ('Request Details', {
            'fields': ('request_id', 'amount', 'currency', 'description', 'reference')
        }),
        ('Users', {
            'fields': ('requester', 'recipient', 'requester_account', 'recipient_account')
        }),
        ('Status & Expiration', {
            'fields': ('status', 'expires_at', 'is_expired')
        }),
        ('Timestamps', {
            'fields': ('created_at', 'updated_at', 'paid_at'),
            'classes': ('collapse',)
        }),
    )


@admin.register(Notification)
class NotificationAdmin(admin.ModelAdmin):
    list_display = ['user', 'notification_type', 'title', 'status', 'created_at']
    list_filter = ['notification_type', 'status', 'is_email_sent', 'is_sms_sent', 'created_at']
    search_fields = ['user__email', 'title', 'message']
    readonly_fields = ['created_at', 'read_at']
    ordering = ['-created_at']
    
    fieldsets = (
        ('Notification Details', {
            'fields': ('user', 'notification_type', 'title', 'message')
        }),
        ('Status', {
            'fields': ('status', 'is_email_sent', 'is_sms_sent')
        }),
        ('Related Objects', {
            'fields': ('transaction', 'payment_request'),
            'classes': ('collapse',)
        }),
        ('Timestamps', {
            'fields': ('created_at', 'read_at'),
            'classes': ('collapse',)
        }),
    )


@admin.register(CreditCard)
class CreditCardAdmin(admin.ModelAdmin):
    list_display = ['user', 'card_brand', 'last_four_digits', 'card_holder_name', 'status', 'is_default', 'created_at']
    list_filter = ['card_type', 'status', 'is_default', 'is_verified', 'created_at']
    search_fields = ['user__email', 'card_holder_name', 'last_four_digits']
    readonly_fields = ['created_at', 'updated_at', 'verified_at', 'last_used', 'masked_number', 'last_four_digits']
    ordering = ['-created_at']
    
    fieldsets = (
        ('Card Information', {
            'fields': ('user', 'card_holder_name', 'card_type', 'card_brand', 'masked_number', 'last_four_digits')
        }),
        ('Card Details', {
            'fields': ('expiry_month', 'expiry_year', 'card_number_encrypted', 'cvv_encrypted'),
            'classes': ('collapse',)
        }),
        ('Status & Settings', {
            'fields': ('status', 'is_default', 'is_verified')
        }),
        ('Security & Limits', {
            'fields': ('daily_limit', 'monthly_limit', 'failed_attempts', 'locked_until'),
            'classes': ('collapse',)
        }),
        ('Usage Tracking', {
            'fields': ('total_funded', 'total_withdrawn', 'last_used'),
            'classes': ('collapse',)
        }),
        ('Timestamps', {
            'fields': ('created_at', 'updated_at', 'verified_at'),
            'classes': ('collapse',)
        }),
    )
    
    def get_queryset(self, request):
        return super().get_queryset(request).select_related('user')
