"""Fraud scoring for outgoing payments (and card top-ups).

A payment is scored at its confirm step from two sources:

* rules a reviewer can read: size against the user's usual payments, a new recipient,
  how fast money is leaving, a new device or card, a brand-new account;
* an Isolation Forest fitted on the user's own past payments (amount, time of day,
  day of week), which catches combinations the rules don't describe.

The score decides what happens: allow, ask the user to confirm (2FA code, or password
when 2FA is off), or hold the payment for staff review. Every outcome is logged.
"""
import math
import statistics
from dataclasses import dataclass, field
from datetime import timedelta
from decimal import Decimal

from django.conf import settings
from django.db.models import Q, Sum
from django.utils import timezone

from account.models import CreditCard, InternationalTransfer, Notification, Transaction
from userauths.models import SecurityLog, UserDevice

from . import anomaly
from .models import RiskDecision

MODEL_VERSION = 'payments-v1'
HISTORY_DAYS = 180
OUTGOING_TYPES = ['transfer', 'payment_request', 'withdrawal']
FLOW_LABELS = {
    'transfer': 'Transfer', 'settlement': 'Payment request', 'card_fund': 'Card top-up',
    'card_withdraw': 'Card withdrawal', 'international': 'International transfer',
}


def thresholds():
    cfg = getattr(settings, 'RISK', {})
    return cfg.get('PAYMENT_CHALLENGE', 40), cfg.get('PAYMENT_HOLD', 75)


@dataclass
class Assessment:
    score: int
    reasons: list
    features: dict
    action: str = 'allow'


@dataclass
class Gate:
    """What the view should do with a payment at its confirm step."""
    action: str                         # 'allow' | 'challenge' | 'hold'
    assessment: Assessment
    decision: RiskDecision = None
    method: str = ''                    # for a challenge: 'totp' or 'password'
    error: str = ''                     # for a challenge: why the last code was refused
    reasons: list = field(default_factory=list)

    @property
    def allowed(self):
        return self.action == 'allow'


def _history(user, since):
    """(when, amount_usd, counterparty) for the user's completed outgoing payments."""
    rows = [(t.created_at, float(t.amount), f'acct:{t.receiver_account_id}')
            for t in Transaction.objects.filter(sender=user, status='completed', created_at__gte=since,
                                                transaction_type__in=OUTGOING_TYPES)
            .exclude(transaction_type='transfer', receiver=user)
            .only('created_at', 'amount', 'receiver_account_id')]
    rows += [(t.created_at, float(t.amount_in_usd), f'iban:{t.recipient_swift_code}:{t.recipient_account_number}')
             for t in InternationalTransfer.objects.filter(sender=user, created_at__gte=since)
             .exclude(status__in=['failed', 'cancelled'])
             .only('created_at', 'amount_in_usd', 'recipient_swift_code', 'recipient_account_number')]
    rows.sort(key=lambda r: r[0])
    return rows


def _event_vector(when, amount):
    local = timezone.localtime(when)
    return [math.log1p(amount), *anomaly.hour_features(local), local.weekday() / 6]


def score_payment(user, flow, amount_usd, counterparty='', request=None, card=None):
    """Score one payment. `amount_usd` is the total leaving (or, for top-ups, entering) the account."""
    now = timezone.now()
    amount = float(amount_usd)
    reasons, features, score = [], {'flow': flow, 'amount_usd': round(amount, 2)}, 0

    def add(points, reason):
        nonlocal score
        score += points
        reasons.append(reason)

    history = _history(user, now - timedelta(days=HISTORY_DAYS))
    amounts = [r[1] for r in history]
    features['history_count'] = len(history)

    # -- size compared with the user's usual payments
    if len(amounts) >= 5:
        median = statistics.median(amounts)
        ratio = amount / median if median else 0
        features['amount_vs_median'] = round(ratio, 2)
        if ratio >= 10:
            add(30, f'About {ratio:.0f}x the usual payment size (median ${median:,.0f})')
        elif ratio >= 4:
            add(18, f'About {ratio:.1f}x the usual payment size (median ${median:,.0f})')
        elif ratio >= 2.5:
            add(8, f'Larger than usual ({ratio:.1f}x the median of ${median:,.0f})')
    elif flow != 'card_fund' and amount >= 1000:
        add(15, f'${amount:,.0f} with little payment history to compare against')

    # -- share of the balance
    if flow != 'card_fund':
        balance = float(user.account.account_balance)
        share = amount / balance if balance > 0 else 1
        features['share_of_balance'] = round(share, 2)
        if share >= 0.9:
            add(15, f'Uses {share:.0%} of the available balance')
        elif share >= 0.6:
            add(7, f'Uses {share:.0%} of the available balance')

    # -- first payment to this recipient
    if counterparty and flow not in ('card_fund', 'card_withdraw'):
        known = {r[2] for r in history}
        features['new_recipient'] = counterparty not in known
        if counterparty not in known:
            add(12 if history else 6, 'First payment to this recipient')

    # -- velocity: how many payments and how much money in the last hour / day
    last_hour = sum(1 for r in history if r[0] >= now - timedelta(hours=1))
    last_day_total = sum(r[1] for r in history if r[0] >= now - timedelta(days=1))
    features.update(payments_last_hour=last_hour, sent_last_24h=round(last_day_total, 2))
    if last_hour >= 5:
        add(20, f'{last_hour} payments in the last hour')
    elif last_hour >= 3:
        add(10, f'{last_hour} payments in the last hour')
    daily_totals = {}
    for when, amt, _ in history:
        day = timezone.localtime(when).date()
        daily_totals[day] = daily_totals.get(day, 0) + amt
    usual_day = statistics.median(daily_totals.values()) if len(daily_totals) >= 5 else 0
    if flow != 'card_fund' and last_day_total + amount > max(2000, 5 * usual_day):
        add(12, f'${last_day_total + amount:,.0f} sent in 24 hours, well above a usual day')

    # -- the device and the account
    if request is not None:
        from userauths.services import SecurityService
        device_id = SecurityService.generate_device_id(request, user)
        device = UserDevice.objects.filter(user=user, device_id=device_id).only('first_used', 'is_trusted').first()
        features['device_trusted'] = bool(device and device.is_trusted)
        if device is None or device.first_used >= now - timedelta(hours=24):
            features['new_device'] = True
            add(10, 'Signed in on a new device today')
    account_age_days = (now - user.date_joined).days
    features['account_age_days'] = account_age_days
    if account_age_days < 7:
        add(8, 'Account opened less than a week ago')
    failed_recent = SecurityLog.objects.filter(
        Q(user=user) | Q(details__email=user.email),
        event_type__in=['login_failed', '2fa_failed', '2fa_backup_failed'],
        timestamp__gte=now - timedelta(hours=24)).count()
    features['failed_security_24h'] = failed_recent
    if failed_recent >= 3:
        add(10, f'{failed_recent} failed sign-in or code attempts in the last 24 hours')

    # -- a card added very recently (stolen-card top-ups are usually immediate)
    if card is not None and isinstance(card, CreditCard):
        if card.created_at >= now - timedelta(hours=24):
            features['card_age_hours'] = round((now - card.created_at).total_seconds() / 3600, 1)
            add(15 if flow == 'card_fund' else 8, 'Card was added in the last 24 hours')

    # -- the learned part: how unusual is this against the user's own pattern?
    rank = anomaly.unusualness([_event_vector(r[0], r[1]) for r in history], _event_vector(now, amount))
    if rank is not None:
        features['anomaly_rank'] = round(rank, 3)
        if rank <= 0.01:
            add(20, 'Unlike any of this account\'s previous payments (amount and timing)')
        elif rank <= 0.05:
            add(10, 'Unusual for this account (amount and timing)')

    score = max(0, min(100, int(round(score))))
    challenge_at, hold_at = thresholds()
    action = 'hold' if score >= hold_at else 'challenge' if score >= challenge_at else 'allow'
    return Assessment(score=score, reasons=reasons, features=features, action=action)


def _log(request, user, flow, assessment, action, amount_usd, payload=None, review='none', extra_reasons=()):
    from userauths.services import SecurityService
    return RiskDecision.objects.create(
        user=user, kind='payment', subject=flow, score=assessment.score, action=action,
        reasons=list(assessment.reasons) + list(extra_reasons), features=assessment.features,
        payload=payload or {}, model_version=MODEL_VERSION, amount_usd=Decimal(str(amount_usd)).quantize(Decimal('0.01')),
        ip_address=SecurityService.get_client_ip(request) if request else None, review_status=review,
    )


def _failed_step_ups(user):
    return SecurityLog.objects.filter(user=user, event_type='2fa_failed', details__context='payment',
                                      timestamp__gte=timezone.now() - timedelta(minutes=15)).count()


def _verify_step_up(user, code):
    """Check a step-up code: the TOTP code when 2FA is on, otherwise the account password."""
    twofa = getattr(user, 'twofactorauth', None) if hasattr(user, 'twofactorauth') else None
    if twofa is not None and twofa.is_enabled and twofa.secret_key:
        from userauths.forms import verify_totp_code
        return 'totp', verify_totp_code(twofa.secret_key, code.replace(' ', ''))
    return 'password', user.check_password(code)


def _step_up_method(user):
    twofa = getattr(user, 'twofactorauth', None) if hasattr(user, 'twofactorauth') else None
    return 'totp' if twofa is not None and twofa.is_enabled and twofa.secret_key else 'password'


def hold(request, user, flow, assessment, amount_usd, payload, extra_reasons=()):
    decision = _log(request, user, flow, assessment, 'hold', amount_usd, payload=payload, review='pending',
                    extra_reasons=extra_reasons)
    Notification.objects.create(
        user=user, notification_type='security', title='Payment held for review',
        message=f'Your {FLOW_LABELS.get(flow, "payment").lower()} of ${Decimal(str(amount_usd)):,.2f} is being checked by our team. '
                f'No money has moved yet. We will let you know as soon as it is reviewed.')
    return decision


def gate_payment(request, flow, amount_usd, payload, counterparty='', card=None):
    """Score a payment at its confirm step and decide what the view should do.

    `payload` must contain everything account.payments.execute() needs for this flow,
    so a held payment can be carried out later exactly as submitted.
    """
    user = request.user
    a = score_payment(user, flow, amount_usd, counterparty=counterparty, request=request, card=card)

    if a.action == 'hold':
        return Gate('hold', a, decision=hold(request, user, flow, a, amount_usd, payload))

    if a.action == 'challenge':
        method = _step_up_method(user)
        code = (request.POST.get('risk_code') or '').strip()
        if not code:
            return Gate('challenge', a, method=method)

        from userauths.services import SecurityService
        method, ok = _verify_step_up(user, code)
        if not ok:
            SecurityService.log_security_event(user, '2fa_failed', request,
                                               details={'context': 'payment', 'flow': flow, 'method': method},
                                               risk_score=a.score)
            if _failed_step_ups(user) >= 3:
                return Gate('hold', a, decision=hold(request, user, flow, a, amount_usd, payload,
                                                     extra_reasons=['Confirmation failed 3 times']))
            msg = 'That code is not right. Try the current code from your authenticator app.' \
                if method == 'totp' else 'That password is not right.'
            return Gate('challenge', a, method=method, error=msg)

        SecurityService.log_security_event(user, '2fa_used', request,
                                           details={'context': 'payment', 'flow': flow, 'method': method},
                                           risk_score=a.score)
        return Gate('allow', a, decision=_log(request, user, flow, a, 'challenge', amount_usd,
                                              extra_reasons=[f'Confirmed by the user ({"2FA code" if method == "totp" else "password"})']))

    return Gate('allow', a, decision=_log(request, user, flow, a, 'allow', amount_usd))


def link_result(gate, result):
    """Record which transaction an allowed payment produced."""
    from account.payments import reference_of
    if gate and gate.decision is not None:
        gate.decision.result_ref = reference_of(result)
        gate.decision.save(update_fields=['result_ref'])
