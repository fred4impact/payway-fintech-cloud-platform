from django.db.models.signals import post_save
from django.dispatch import receiver
from django.utils import timezone

from account.models import KYC

from .models import RiskDecision


@receiver(post_save, sender=KYC)
def close_kyc_review(sender, instance, **kwargs):
    """A KYC approved or rejected straight from its admin page leaves the review queue too."""
    if instance.verification_status in ('approved', 'rejected'):
        RiskDecision.objects.filter(kind='kyc', result_ref=str(instance.pk), review_status='pending').update(
            review_status=instance.verification_status, reviewed_at=timezone.now())
