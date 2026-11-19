"""Staff decisions on items the risk engine queued: held payments and KYC submissions."""
import logging

from django.db import transaction
from django.utils import timezone

from account.models import KYC, Notification
from account.payments import execute, reference_of

from .models import RiskDecision
from .payments import FLOW_LABELS

logger = logging.getLogger(__name__)


def _close(decision, staff, status, note=''):
    decision.review_status = status
    decision.reviewed_by = staff
    decision.reviewed_at = timezone.now()
    if note:
        decision.review_note = note
    decision.save(update_fields=['review_status', 'reviewed_by', 'reviewed_at', 'review_note', 'result_ref'])


def approve(decision, staff, note=''):
    """Approve a pending item. Returns (ok, message)."""
    with transaction.atomic():
        decision = RiskDecision.objects.select_for_update().get(pk=decision.pk)
        if decision.review_status != 'pending':
            return False, f'#{decision.pk} is not awaiting review'

        if decision.kind == 'payment':
            label = FLOW_LABELS.get(decision.subject, 'Payment')
            try:
                with transaction.atomic():
                    result = execute(decision.subject, decision.user, decision.payload)
            except Exception as e:  # balance changed, request already paid, card limit...
                decision.result_ref = ''
                _close(decision, staff, 'failed', note or str(e))
                Notification.objects.create(
                    user=decision.user, notification_type='security', title=f'{label} not completed',
                    message=f'Your held {label.lower()} of ${decision.amount_usd:,.2f} was approved but could not be '
                            f'completed: {e} No money has moved.')
                return False, f'#{decision.pk} approved but could not be completed: {e}'
            decision.result_ref = reference_of(result)
            _close(decision, staff, 'approved', note)
            Notification.objects.create(
                user=decision.user, notification_type='transaction', title=f'{label} approved',
                message=f'Your {label.lower()} of ${decision.amount_usd:,.2f} was reviewed and has gone through '
                        f'(reference {decision.result_ref}).')
            return True, f'#{decision.pk} approved and completed ({decision.result_ref})'

        if decision.kind == 'kyc':
            kyc = KYC.objects.filter(pk=decision.result_ref).first()
            if kyc is None:
                return False, f'#{decision.pk}: KYC record no longer exists'
            kyc.verification_status = 'approved'
            kyc.verified_at = timezone.now()
            kyc.save(update_fields=['verification_status', 'verified_at', 'updated_at'])
            kyc.user.is_verified = True
            kyc.user.save(update_fields=['is_verified'])
            _close(decision, staff, 'approved', note)
            Notification.objects.create(user=decision.user, notification_type='kyc', title='Identity verified',
                                        message='Your identity documents have been approved.')
            return True, f'#{decision.pk}: KYC approved'

    return False, f'#{decision.pk} cannot be approved'


def reject(decision, staff, note=''):
    with transaction.atomic():
        decision = RiskDecision.objects.select_for_update().get(pk=decision.pk)
        if decision.review_status != 'pending':
            return False, f'#{decision.pk} is not awaiting review'
        _close(decision, staff, 'rejected', note)
        if decision.kind == 'payment':
            label = FLOW_LABELS.get(decision.subject, 'Payment')
            Notification.objects.create(
                user=decision.user, notification_type='security', title=f'{label} declined',
                message=f'Your held {label.lower()} of ${decision.amount_usd:,.2f} was reviewed and declined. '
                        f'No money left your account. Contact support if you think this is a mistake.')
        elif decision.kind == 'kyc':
            KYC.objects.filter(pk=decision.result_ref).update(verification_status='rejected', updated_at=timezone.now())
            Notification.objects.create(
                user=decision.user, notification_type='kyc', title='Identity verification unsuccessful',
                message='We could not verify your identity from the documents provided. Please contact support.')
        return True, f'#{decision.pk} rejected'
