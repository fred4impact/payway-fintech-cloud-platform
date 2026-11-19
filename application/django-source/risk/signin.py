"""Sign-in risk scoring.

Replaces the old hand-written SecurityService.assess_risk_score rules. A sign-in is scored
from the security log: is this device or network new for the user, have there been failed
attempts on the account or from this address, is the request itself odd, and (once the user
has enough history) how unusual the time of day and weekday are for them, using an Isolation
Forest fitted on their own past sign-ins.

Must be called *before* the device is recorded, otherwise every device already looks known.
"""
from dataclasses import dataclass
from datetime import timedelta

from django.conf import settings
from django.db.models import Q
from django.utils import timezone

from userauths.models import SecurityLog, UserDevice

from . import anomaly
from .models import RiskDecision

MODEL_VERSION = 'signin-v1'
SUCCESS_EVENTS = ['login_success']


def thresholds():
    cfg = getattr(settings, 'RISK', {})
    return cfg.get('SIGNIN_CHALLENGE', 40), cfg.get('SIGNIN_BLOCK', 85)


@dataclass
class SigninAssessment:
    score: int
    reasons: list
    features: dict
    device_known: bool = False
    device_trusted: bool = False

    @property
    def unusual(self):
        return self.score >= thresholds()[0]

    @property
    def blocked(self):
        return self.score >= thresholds()[1]


def _login_vector(when):
    local = timezone.localtime(when)
    return [*anomaly.hour_features(local), local.weekday() / 6]


def assess(request, user=None, email=None):
    from userauths.services import SecurityService

    now = timezone.now()
    ip = SecurityService.get_client_ip(request)
    ua = request.META.get('HTTP_USER_AGENT', '')
    reasons, features, score = [], {'ip': ip}, 0

    def add(points, reason):
        nonlocal score
        score += points
        reasons.append(reason)

    # -- the request itself
    if not ua or len(ua) < 10:
        add(20, 'Missing or truncated browser identifier')
    if SecurityService.is_suspicious_ip(ip):
        add(30, 'Request from an invalid or reserved address')
    from_ip_last_min = SecurityLog.objects.filter(ip_address=ip, timestamp__gte=now - timedelta(minutes=1)).count()
    features['events_from_ip_last_minute'] = from_ip_last_min
    if from_ip_last_min > 10:
        add(25, f'{from_ip_last_min} requests from this address in the last minute')

    # -- failed attempts, on this account and from this address
    email = email or (user.email if user else None)
    acct_q = Q(user=user) if user else Q(pk__in=[])
    if email:
        acct_q |= Q(details__email=email)
    failed_acct = SecurityLog.objects.filter(acct_q, event_type__in=['login_failed', '2fa_failed'],
                                             timestamp__gte=now - timedelta(hours=1)).count()
    failed_ip = SecurityLog.objects.filter(ip_address=ip, event_type='login_failed',
                                           timestamp__gte=now - timedelta(hours=1)).count()
    features.update(failed_account_1h=failed_acct, failed_ip_1h=failed_ip)
    if failed_acct:
        add(min(failed_acct * 10, 40), f'{failed_acct} failed attempt(s) on this account in the last hour')
    if failed_ip >= 5:
        add(15, f'{failed_ip} failed sign-ins from this address in the last hour')

    device_known = device_trusted = False
    if user is not None:
        devices = UserDevice.objects.filter(user=user)
        device = devices.filter(device_id=SecurityService.generate_device_id(request, user)).first()
        device_known = device is not None
        device_trusted = bool(device and device.is_trusted and device.is_active)
        has_devices = devices.exists()
        features.update(device_known=device_known, device_trusted=device_trusted)
        if not device_known:
            add(25 if has_devices else 5, 'New device for this account' if has_devices else 'First sign-in on record')
        elif device is not None and not device.is_active:
            add(20, 'Device was removed from the account')

        past = SecurityLog.objects.filter(user=user, event_type__in=SUCCESS_EVENTS,
                                          timestamp__gte=now - timedelta(days=180))
        known_ips = set(past.values_list('ip_address', flat=True)[:500])
        features['known_ip'] = ip in known_ips
        if known_ips and ip not in known_ips:
            add(10, 'New network address for this account')

        locks = user.accountlockout_set.filter(locked_at__gte=now - timedelta(days=7)).count()
        if locks:
            add(10, 'Account was locked in the last 7 days')

        # -- learned: is this an unusual time for this person to sign in?
        history = [_login_vector(t) for t in past.order_by('timestamp').values_list('timestamp', flat=True)[:400]]
        rank = anomaly.unusualness(history, _login_vector(now))
        if rank is not None:
            features['time_anomaly_rank'] = round(rank, 3)
            if rank <= 0.02:
                add(15, 'Unusual time of day or week for this account')
            elif rank <= 0.06:
                add(7, 'Somewhat unusual time for this account')

    score = max(0, min(100, int(round(score))))
    return SigninAssessment(score=score, reasons=reasons, features=features,
                            device_known=device_known, device_trusted=device_trusted)


def record(request, user, assessment, action, extra_reasons=()):
    from userauths.services import SecurityService
    return RiskDecision.objects.create(
        user=user, kind='signin', subject='login', score=assessment.score, action=action,
        reasons=list(assessment.reasons) + list(extra_reasons), features=assessment.features,
        model_version=MODEL_VERSION, ip_address=SecurityService.get_client_ip(request),
    )
