from django.contrib import admin, messages
from django.utils.html import format_html, format_html_join

from . import review
from .models import RiskDecision


@admin.register(RiskDecision)
class RiskDecisionAdmin(admin.ModelAdmin):
    """Every automated decision. Filter 'Awaiting review' for the work queue."""
    list_display = ['id', 'created_at', 'user', 'kind', 'subject', 'score_badge', 'action', 'amount_usd',
                    'review_status', 'result_ref']
    list_filter = ['review_status', 'kind', 'action', 'subject', 'created_at']
    search_fields = ['user__email', 'result_ref', 'ip_address']
    date_hierarchy = 'created_at'
    ordering = ['-created_at']
    actions = ['approve_selected', 'reject_selected']
    readonly_fields = ['user', 'kind', 'subject', 'score', 'action', 'reasons_list', 'features', 'payload',
                       'model_version', 'amount_usd', 'ip_address', 'review_status', 'reviewed_by', 'reviewed_at',
                       'result_ref', 'created_at']
    fieldsets = (
        ('Decision', {'fields': ('user', 'kind', 'subject', 'amount_usd', 'score', 'action', 'reasons_list',
                                 'model_version', 'ip_address', 'created_at')}),
        ('Review', {'fields': ('review_status', 'reviewed_by', 'reviewed_at', 'review_note', 'result_ref')}),
        ('Inputs', {'fields': ('features', 'payload'), 'classes': ('collapse',)}),
    )

    def has_add_permission(self, request):
        return False

    @admin.display(description='Score', ordering='score')
    def score_badge(self, obj):
        colour = {'high': '#dc2626', 'medium': '#d97706', 'low': '#047857'}[obj.score_band]
        return format_html('<b style="color:{}">{}</b>', colour, obj.score)

    @admin.display(description='Why')
    def reasons_list(self, obj):
        if not obj.reasons:
            return 'Nothing unusual'
        return format_html('<ul style="margin:0;padding-left:1.1em">{}</ul>',
                           format_html_join('', '<li>{}</li>', ((r,) for r in obj.reasons)))

    def _review(self, request, queryset, fn):
        for decision in queryset.filter(review_status='pending'):
            ok, msg = fn(decision, request.user)
            self.message_user(request, msg, messages.SUCCESS if ok else messages.WARNING)
        skipped = queryset.exclude(review_status='pending').count()
        if skipped:
            self.message_user(request, f'{skipped} selected item(s) were not awaiting review and were skipped.',
                              messages.INFO)

    @admin.action(description='Approve (carry out held payments / approve KYC)')
    def approve_selected(self, request, queryset):
        self._review(request, queryset, review.approve)

    @admin.action(description='Reject (decline held payments / reject KYC)')
    def reject_selected(self, request, queryset):
        self._review(request, queryset, review.reject)
