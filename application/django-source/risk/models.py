from django.conf import settings
from django.db import models


class RiskDecision(models.Model):
    """One automated risk decision: a scored payment, sign-in or KYC submission.

    Every decision is kept, including the ones that let things through, so staff can see
    why the system acted and tune the thresholds. Held payments and KYC submissions wait
    here (review_status='pending') until a member of staff approves or rejects them.
    """

    KIND_CHOICES = [
        ('payment', 'Payment'),
        ('signin', 'Sign-in'),
        ('kyc', 'KYC'),
    ]
    ACTION_CHOICES = [
        ('allow', 'Allowed'),
        ('challenge', 'Asked to confirm'),
        ('hold', 'Held for review'),
        ('block', 'Blocked'),
        ('review', 'Sent for review'),
    ]
    REVIEW_CHOICES = [
        ('none', 'No review needed'),
        ('pending', 'Awaiting review'),
        ('approved', 'Approved'),
        ('rejected', 'Rejected'),
        ('failed', 'Approved, but could not complete'),
    ]

    user = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.CASCADE,
                             related_name='risk_decisions', null=True, blank=True)
    kind = models.CharField(max_length=10, choices=KIND_CHOICES)
    # What was scored: transfer, settlement, card_fund, card_withdraw, international, login, kyc
    subject = models.CharField(max_length=20)
    score = models.PositiveSmallIntegerField(default=0, help_text='Risk score from 0 (normal) to 100')
    action = models.CharField(max_length=10, choices=ACTION_CHOICES)
    reasons = models.JSONField(default=list, blank=True, help_text='Why the score is what it is')
    features = models.JSONField(default=dict, blank=True, help_text='Inputs the score was computed from')
    # For held payments: everything needed to carry the payment out once approved.
    payload = models.JSONField(default=dict, blank=True)
    model_version = models.CharField(max_length=30, blank=True)
    amount_usd = models.DecimalField(max_digits=15, decimal_places=2, null=True, blank=True)
    ip_address = models.GenericIPAddressField(null=True, blank=True)

    review_status = models.CharField(max_length=10, choices=REVIEW_CHOICES, default='none')
    reviewed_by = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.SET_NULL,
                                    related_name='risk_reviews', null=True, blank=True)
    reviewed_at = models.DateTimeField(null=True, blank=True)
    review_note = models.TextField(blank=True)
    # Transaction / transfer ID created when a held payment is approved, or the KYC id.
    result_ref = models.CharField(max_length=40, blank=True)

    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ['-created_at']
        indexes = [
            models.Index(fields=['kind', 'review_status', 'created_at']),
            models.Index(fields=['user', 'kind', 'created_at']),
        ]

    def __str__(self):
        return f'{self.get_kind_display()} {self.subject} · {self.score} · {self.get_action_display()}'

    @property
    def score_band(self):
        if self.score >= 75:
            return 'high'
        if self.score >= 40:
            return 'medium'
        return 'low'
