from django.shortcuts import render, redirect, get_object_or_404
from django.contrib.auth import login, logout, authenticate
from django.contrib.auth.decorators import login_required
from django.contrib import messages
from django.http import JsonResponse, HttpResponse
from django.utils import timezone
from django.db.models import Q
from django.core.paginator import Paginator
from django.conf import settings
import logging

from .forms import (
    UserRegisterForm, UserLoginForm, TwoFactorSetupForm, TwoFactorVerifyForm,
    TwoFactorBackupCodeForm, TwoFactorDisableForm, SecuritySettingsForm,
    DeviceTrustForm, SecurityLogFilterForm
)
from .services import SecurityService
from .models import User, TwoFactorAuth, UserDevice, SecurityLog, AccountLockout
from .forms import generate_totp_secret, generate_totp_qr_code, verify_totp_code, generate_backup_codes
from account.models import Notification
from risk import signin as risk_signin
import json

logger = logging.getLogger(__name__)


def register_view(request):
    if request.method == 'POST':
        form = UserRegisterForm(request.POST)
        if form.is_valid():
            try:
                user = form.save()
                login(request, user)
                
                # Log successful registration
                try:
                    SecurityService.log_security_event(
                        user=user,
                        event_type='account_created',
                        request=request,
                        details={'registration_method': 'email'},
                        risk_score=SecurityService.assess_risk_score(request, user)
                    )
                except Exception as e:
                    logger.exception("Security logging failed after registration")
                
                messages.success(request, 'Account created successfully!')
                return redirect('core:dashboard')
            except Exception as e:
                logger.exception("Registration failed")
                messages.error(request, 'Registration failed. Please try again.')
                if settings.DEBUG:
                    raise
        else:
            # Log failed registration attempt
            try:
                SecurityService.log_security_event(
                    user=None,
                    event_type='registration_failed',
                    request=request,
                    details={'errors': dict(form.errors)},
                    risk_score=SecurityService.assess_risk_score(request)
                )
            except Exception as e:
                logger.exception("Security logging failed on registration_failed")
    else:
        form = UserRegisterForm()
    
    return render(request, 'userauths/register.html', {'form': form})


def login_view(request):
    if request.method == 'POST':
        form = UserLoginForm(request, data=request.POST)
        if form.is_valid():
            email = form.cleaned_data.get('email')
            password = form.cleaned_data.get('password')
            
            # Check if user exists and is not locked
            try:
                from .models import User
                user = User.objects.get(email=email)
                
                # Check if account is locked
                if user.is_account_locked():
                    try:
                        SecurityService.log_security_event(
                            user=user,
                            event_type='login_blocked',
                            request=request,
                            details={'reason': 'Account locked'},
                            risk_score=90
                        )
                    except Exception as e:
                        print(f"Security logging failed: {e}")
                    messages.error(request, 'Account is temporarily locked due to multiple failed login attempts. Please try again later.')
                    return render(request, 'userauths/login.html', {'form': form})
                
            except User.DoesNotExist:
                user = None
            
            # Attempt authentication
            user = authenticate(username=email, password=password)
            
            if user is not None:
                # Check if account is locked after authentication
                if user.is_account_locked():
                    try:
                        SecurityService.log_security_event(
                            user=user,
                            event_type='login_blocked',
                            request=request,
                            details={'reason': 'Account locked'},
                            risk_score=90
                        )
                    except Exception as e:
                        print(f"Security logging failed: {e}")
                    messages.error(request, 'Account is temporarily locked due to multiple failed login attempts. Please try again later.')
                    return render(request, 'userauths/login.html', {'form': form})
                
                # Score the sign-in before the device is recorded (a new device must still look new).
                signin = risk_signin.assess(request, user, email)
                twofa = TwoFactorAuth.objects.filter(user=user, is_enabled=True).first()

                if twofa is not None:
                    skip = (settings.RISK.get('SKIP_2FA_ON_TRUSTED_DEVICE', False)
                            and signin.device_trusted and not signin.unusual)
                    if skip:
                        risk_signin.record(request, user, signin, 'allow', ['Trusted device: 2FA code skipped'])
                        return _complete_login(request, user, signin.score)

                    risk_signin.record(request, user, signin, 'challenge', ['2FA code required'])
                    request.session['2fa_user_id'] = user.id
                    request.session['2fa_method'] = twofa.method
                    try:
                        SecurityService.log_security_event(
                            user=user, event_type='2fa_required', request=request,
                            details={'method': twofa.method}, risk_score=signin.score)
                    except Exception:
                        logger.exception('Security logging failed')
                    return redirect('userauths:two_factor_verify')

                if signin.blocked:
                    # No second factor to fall back on, and the sign-in looks like an attack.
                    risk_signin.record(request, user, signin, 'block')
                    try:
                        SecurityService.log_security_event(
                            user=user, event_type='login_blocked', request=request,
                            details={'reason': 'High sign-in risk', 'signals': signin.reasons}, risk_score=signin.score)
                    except Exception:
                        logger.exception('Security logging failed')
                    Notification.objects.create(
                        user=user, notification_type='security', title='Sign-in blocked',
                        message='We blocked a sign-in to your account because it looked unusual. If it was you, '
                                'try again later from a device you have used before. If it was not, change your password.')
                    messages.error(request, "We couldn't sign you in because this attempt looks unusual. "
                                            "Try again later from a device you've used before, or contact support.")
                    return render(request, 'userauths/login.html', {'form': form})

                if signin.unusual:
                    risk_signin.record(request, user, signin, 'allow', ['Flagged to the user'])
                    try:
                        SecurityService.log_security_event(
                            user=user, event_type='suspicious_activity', request=request,
                            details={'reason': 'Unusual sign-in', 'signals': signin.reasons}, risk_score=signin.score)
                    except Exception:
                        logger.exception('Security logging failed')
                    Notification.objects.create(
                        user=user, notification_type='security', title='Unusual sign-in',
                        message='Your account was just signed in to from a device or place we have not seen before. '
                                'If this was not you, change your password and turn on two-factor authentication.')
                else:
                    risk_signin.record(request, user, signin, 'allow')
                return _complete_login(request, user, signin.score)
            else:
                # Failed login
                try:
                    SecurityService.handle_failed_login(request, email)
                except Exception as e:
                    # Log the error but don't fail login
                    print(f"Security service failed: {e}")
                messages.error(request, 'Invalid email or password.')
        else:
            # Form validation failed
            try:
                SecurityService.log_security_event(
                    user=None,
                    event_type='login_validation_failed',
                    request=request,
                    details={'errors': form.errors},
                    risk_score=SecurityService.assess_risk_score(request)
                )
            except Exception as e:
                # Log the error but don't fail login
                print(f"Security service failed: {e}")
    else:
        form = UserLoginForm()
    
    return render(request, 'userauths/login.html', {'form': form})


def _complete_login(request, user, risk_score):
    login(request, user)
    try:
        SecurityService.handle_successful_login(request, user, risk_score=risk_score)
    except Exception:
        logger.exception('Security service failed')
    messages.success(request, f'Welcome back, {user.username}!')
    return redirect('core:dashboard')


def two_factor_verify_view(request):
    """Verify Two-Factor Authentication code"""
    user_id = request.session.get('2fa_user_id')
    method = request.session.get('2fa_method')
    
    if not user_id or not method:
        messages.error(request, 'Invalid 2FA session. Please login again.')
        return redirect('userauths:login')
    
    try:
        user = User.objects.get(id=user_id)
        twofa = user.twofactorauth
    except (User.DoesNotExist, TwoFactorAuth.DoesNotExist):
        messages.error(request, 'Invalid 2FA session. Please login again.')
        return redirect('userauths:login')
    
    if request.method == 'POST':
        form = TwoFactorVerifyForm(request.POST)
        if form.is_valid():
            code = form.cleaned_data.get('code')
            
            # Verify the code
            if method == 'totp':
                if verify_totp_code(twofa.secret_key, code):
                    # Successful 2FA verification
                    login(request, user)
                    
                    # Update 2FA last used
                    twofa.last_used = timezone.now()
                    twofa.save()
                    
                    # Log successful 2FA
                    try:
                        SecurityService.log_security_event(
                            user=user,
                            event_type='2fa_used',
                            request=request,
                            details={'method': method},
                            risk_score=SecurityService.assess_risk_score(request, user)
                        )
                    except Exception as e:
                        print(f"Security logging failed: {e}")
                    
                    # Handle successful login
                    try:
                        SecurityService.handle_successful_login(request, user)
                    except Exception as e:
                        print(f"Security service failed: {e}")
                    
                    # Clear 2FA session
                    del request.session['2fa_user_id']
                    del request.session['2fa_method']
                    
                    messages.success(request, f'Welcome back, {user.username}!')
                    return redirect('core:dashboard')
                else:
                    # Failed 2FA verification
                    try:
                        SecurityService.log_security_event(
                            user=user,
                            event_type='2fa_failed',
                            request=request,
                            details={'method': method, 'code_entered': code},
                            risk_score=80
                        )
                    except Exception as e:
                        print(f"Security logging failed: {e}")
                    messages.error(request, 'Invalid 2FA code. Please try again.')
            else:
                # SMS 2FA (placeholder for future implementation)
                messages.error(request, 'SMS 2FA not yet implemented.')
    else:
        form = TwoFactorVerifyForm()
    
    context = {
        'form': form,
        'method': method,
        'pending_user': user,   # not `user`: that would make templates treat them as signed in
    }
    
    return render(request, 'userauths/two_factor_verify.html', context)


def two_factor_backup_code_view(request):
    """Use backup code for 2FA"""
    user_id = request.session.get('2fa_user_id')
    
    if not user_id:
        messages.error(request, 'Invalid 2FA session. Please login again.')
        return redirect('userauths:login')
    
    try:
        user = User.objects.get(id=user_id)
        twofa = user.twofactorauth
    except (User.DoesNotExist, TwoFactorAuth.DoesNotExist):
        messages.error(request, 'Invalid 2FA session. Please login again.')
        return redirect('userauths:login')
    
    if request.method == 'POST':
        form = TwoFactorBackupCodeForm(request.POST)
        if form.is_valid():
            backup_code = form.cleaned_data.get('backup_code')
            
            if twofa.verify_backup_code(backup_code):
                # Successful backup code verification
                login(request, user)
                
                # Log successful backup code usage
                try:
                    SecurityService.log_security_event(
                        user=user,
                        event_type='2fa_backup_used',
                        request=request,
                        details={'backup_code': backup_code},
                        risk_score=SecurityService.assess_risk_score(request, user)
                    )
                except Exception as e:
                    print(f"Security logging failed: {e}")
                
                # Handle successful login
                try:
                    SecurityService.handle_successful_login(request, user)
                except Exception as e:
                    print(f"Security service failed: {e}")
                
                # Clear 2FA session
                del request.session['2fa_user_id']
                del request.session['2fa_method']
                
                messages.success(request, f'Welcome back, {user.username}!')
                return redirect('core:dashboard')
            else:
                # Invalid backup code
                try:
                    SecurityService.log_security_event(
                        user=user,
                        event_type='2fa_backup_failed',
                        request=request,
                        details={'backup_code_entered': backup_code},
                        risk_score=90
                    )
                except Exception as e:
                    print(f"Security logging failed: {e}")
                messages.error(request, 'Invalid backup code. Please try again.')
    else:
        form = TwoFactorBackupCodeForm()
    
    context = {
        'form': form,
        'pending_user': user,
    }
    
    return render(request, 'userauths/two_factor_backup.html', context)


@login_required
def logout_view(request):
    # Log logout event
    try:
        SecurityService.log_security_event(
            user=request.user,
            event_type='logout',
            request=request,
            risk_score=SecurityService.assess_risk_score(request, request.user)
        )
    except Exception as e:
        print(f"Security logging failed: {e}")
    
    logout(request)
    messages.success(request, 'You have been logged out successfully.')
    return redirect('userauths:login')


SIGNIN_EVENTS = ['login_success']
FAILED_EVENTS = ['login_failed', '2fa_failed', '2fa_backup_failed', 'login_blocked']
EVENT_GROUPS = [
    ('Sign-ins', ['login_success', 'logout']),
    ('2FA codes', ['2fa_used', '2fa_required', '2fa_backup_used']),
    ('Failed attempts', FAILED_EVENTS + ['login_validation_failed']),
    ('Devices', ['device_added', 'device_removed', 'device_trusted', 'device_untrusted']),
    ('Settings', ['password_change', '2fa_enabled', '2fa_disabled', '2fa_setup', '2fa_backup_codes_regenerated',
                  'security_settings_changed']),
]
SEG_PALETTE = ['#a6d16b', '#c6e48b', '#f3b47c', '#f1ec7c', '#7cb342', '#f7c99f']


@login_required
def security_dashboard(request):
    """Security overview: score, sign-in activity, protection, recent events."""
    from collections import Counter
    from datetime import datetime, time, timedelta
    from django.db.models import Q
    from core.dash import last_days, segments, split_bars, trend
    from risk.models import RiskDecision

    user = request.user
    now = timezone.now()
    try:
        summary = SecurityService.get_user_security_summary(user)
    except Exception:
        logger.exception('Security summary failed')
        summary = {}
    score = int(summary.get('security_score', 0) or 0)

    mine = SecurityLog.objects.filter(Q(user=user) | Q(user__isnull=True, details__email=user.email))
    days = last_days(7)
    since = timezone.make_aware(datetime.combine(days[0], time.min))
    ok_by_day, bad_by_day = Counter(), Counter()
    for ts, ev in mine.filter(timestamp__gte=since, event_type__in=SIGNIN_EVENTS + FAILED_EVENTS) \
            .values_list('timestamp', 'event_type'):
        (ok_by_day if ev in SIGNIN_EVENTS else bad_by_day)[timezone.localtime(ts).date()] += 1
    chart = split_bars(days, ok_by_day, bad_by_day, 'Sign-ins', 'Failed')

    def count(events, start, end=None):
        qs = mine.filter(event_type__in=events, timestamp__gte=start)
        return qs.filter(timestamp__lt=end).count() if end else qs.count()

    week, fortnight, month = now - timedelta(days=7), now - timedelta(days=14), now - timedelta(days=30)
    signins_7, signins_prev = count(SIGNIN_EVENTS, week), count(SIGNIN_EVENTS, fortnight, week)
    failed_7, failed_prev = count(FAILED_EVENTS, week), count(FAILED_EVENTS, fortnight, week)
    checked = RiskDecision.objects.filter(user=user, kind='payment', action__in=['challenge', 'hold'])
    checked_30 = checked.filter(created_at__gte=month).count()
    checked_prev = checked.filter(created_at__gte=now - timedelta(days=60), created_at__lt=month).count()

    twofa = TwoFactorAuth.objects.filter(user=user).first()
    twofa_on = bool(twofa and twofa.is_enabled)
    devices = UserDevice.objects.filter(user=user, is_active=True).order_by('-last_used')
    device_count, trusted_count = devices.count(), devices.filter(is_trusted=True).count()
    kyc_status = getattr(getattr(user, 'kyc', None), 'verification_status', '')

    # event mix, last 30 days
    month_events = Counter(mine.filter(timestamp__gte=month).values_list('event_type', flat=True))
    grouped = [(label, sum(month_events.pop(e, 0) for e in evs)) for label, evs in EVENT_GROUPS]
    grouped.append(('Other', sum(month_events.values())))
    mix, mix_total = segments([g for g in grouped if g[1]], SEG_PALETTE)

    checks = [
        {'label': 'Password set', 'done': user.has_usable_password()},
        {'label': 'Two-factor authentication', 'done': twofa_on},
        {'label': 'Identity verified', 'done': kyc_status == 'approved'},
        {'label': 'Every device trusted', 'done': device_count > 0 and trusted_count == device_count},
    ]
    checks_pct = round(sum(c['done'] for c in checks) / len(checks) * 100)
    filled = sum(bool(x) for x in (user.email, user.phone_number, user.date_of_birth, user.first_name))
    goals = {
        'essentials': [
            {'label': 'Two-factor', 'icon': 'fa-key', 'pct': 100 if twofa_on else 0,
             'value': 'On' if twofa_on else 'Off', 'url': 'userauths:two_factor_disable' if twofa_on else 'userauths:two_factor_setup',
             'note': 'Codes required at sign-in' if twofa_on else 'Turn on to protect sign-ins and payments'},
            {'label': 'Trusted devices', 'icon': 'fa-laptop', 'url': 'userauths:device_management',
             'pct': round(trusted_count / device_count * 100) if device_count else 0,
             'value': f'{trusted_count} / {device_count}',
             'note': 'All devices reviewed' if device_count and trusted_count == device_count else 'Review devices you don\'t recognise'},
        ],
        'recommended': [
            {'label': 'Identity check', 'icon': 'fa-id-card', 'url': 'account:kyc',
             'pct': {'approved': 100, 'pending': 50}.get(kyc_status, 0),
             'value': (kyc_status or 'not started').capitalize(),
             'note': 'Unlocks higher limits' if kyc_status != 'approved' else 'Verified'},
            {'label': 'Profile details', 'icon': 'fa-user', 'url': 'account:account_info',
             'pct': round(filled / 4 * 100), 'value': f'{filled} / 4',
             'note': 'Used to recover your account'},
        ],
    }

    if not twofa_on:
        tip = {'title': 'Add a second lock to your account',
               'text': 'Two-factor codes stop someone who has your password. Unusual payments can then be confirmed with a code instead of your password.',
               'url': 'userauths:two_factor_setup', 'cta': 'Turn on 2FA'}
    elif trusted_count < device_count:
        tip = {'title': 'Review your devices',
               'text': f'{device_count - trusted_count} signed-in device(s) aren\'t marked as trusted. Remove any you don\'t recognise.',
               'url': 'userauths:device_management', 'cta': 'Review devices'}
    elif kyc_status != 'approved':
        tip = {'title': 'Finish verifying your identity',
               'text': 'A verified identity makes account recovery faster and lifts your limits.',
               'url': 'account:kyc', 'cta': 'Verify now'}
    else:
        tip = {'title': 'You\'re well protected',
               'text': 'Skim your security log once a month and sign out of devices you no longer use.',
               'url': 'userauths:security_logs', 'cta': 'Open security log'}

    history = []
    for log in mine.order_by('-timestamp')[:8]:
        t = log.event_type
        bad = t in FAILED_EVENTS or t in ('suspicious_activity', 'account_locked')
        tone = 'bad' if bad and log.risk_score >= 70 else 'warn' if bad or log.risk_score >= 40 else \
            'ok' if t in SIGNIN_EVENTS + ['2fa_used', '2fa_enabled', 'device_trusted'] else 'info'
        icon = ('fa-triangle-exclamation' if bad else 'fa-right-to-bracket' if 'login' in t else 'fa-key' if '2fa' in t
                else 'fa-laptop' if 'device' in t else 'fa-lock' if 'password' in t else 'fa-shield-halved')
        history.append({'log': log, 'tone': tone, 'icon': icon,
                        'status': 'High risk' if log.risk_score >= 70 else 'Review' if log.risk_score >= 40 else 'Normal'})

    context = {
        'score': score,
        'score_label': 'Excellent' if score >= 80 else 'Good' if score >= 50 else 'At risk',
        'chart': chart,
        'stats': [
            {'label': 'Sign-ins', 'value': signins_7, 'trend': trend(signins_7, signins_prev, 'up'), 'suffix': 'from last week'},
            {'label': 'Failed attempts', 'value': failed_7, 'trend': trend(failed_7, failed_prev, 'down'), 'suffix': 'from last week'},
            {'label': 'Payments checked', 'value': checked_30, 'trend': trend(checked_30, checked_prev, 'down'), 'suffix': 'from last month'},
        ],
        'twofa': twofa, 'twofa_on': twofa_on,
        'devices': devices[:6], 'device_count': device_count, 'trusted_count': trusted_count,
        'mix': mix, 'mix_total': mix_total,
        'checks': checks, 'checks_pct': checks_pct, 'missing': [c['label'] for c in checks if not c['done']], 'checks_done': sum(c['done'] for c in checks),
        'goals': goals, 'tip': tip, 'history': history,
        'last_login': user.last_login,
    }
    return render(request, 'userauths/security_dashboard.html', context)


@login_required
def two_factor_setup(request):
    """Setup Two-Factor Authentication"""
    try:
        twofa = request.user.twofactorauth
        if twofa.is_enabled:
            messages.info(request, 'Two-Factor Authentication is already enabled.')
            return redirect('userauths:security_dashboard')
    except:
        pass
    
    if request.method == 'POST':
        form = TwoFactorSetupForm(request.POST)
        if form.is_valid():
            method = form.cleaned_data.get('method')
            phone_number = form.cleaned_data.get('phone_number')
            
            # Create or update 2FA settings
            twofa, created = TwoFactorAuth.objects.get_or_create(
                user=request.user,
                defaults={
                    'method': method,
                    'phone_number': phone_number if method == 'sms' else '',
                    'secret_key': generate_totp_secret() if method == 'totp' else '',
                    'backup_codes': generate_backup_codes()
                }
            )
            
            if not created:
                twofa.method = method
                twofa.phone_number = phone_number if method == 'sms' else ''
                if method == 'totp':
                    twofa.secret_key = generate_totp_secret()
                twofa.backup_codes = generate_backup_codes()
                twofa.save()
            
            # Log 2FA setup
            SecurityService.log_security_event(
                user=request.user,
                event_type='2fa_setup',
                request=request,
                details={'method': method},
                risk_score=SecurityService.assess_risk_score(request, request.user)
            )
            
            messages.success(request, 'Two-Factor Authentication setup completed successfully!')
            return redirect('userauths:two_factor_configure')
    else:
        form = TwoFactorSetupForm()
    
    context = {
        'form': form
    }
    
    return render(request, 'userauths/two_factor_setup.html', context)


@login_required
def two_factor_configure(request):
    """Configure Two-Factor Authentication (show QR code, etc.)"""
    try:
        twofa = request.user.twofactorauth
    except:
        messages.error(request, 'Two-Factor Authentication not configured.')
        return redirect('userauths:two_factor_setup')
    
    if twofa.method == 'totp':
        # Generate QR code for TOTP
        qr_code, provisioning_uri = generate_totp_qr_code(
            twofa.secret_key, 
            request.user.email
        )
        
        context = {
            'twofa': twofa,
            'qr_code': qr_code,
            'provisioning_uri': provisioning_uri,
            'backup_codes': twofa.backup_codes
        }
        
        return render(request, 'userauths/two_factor_configure.html', context)
    else:
        # SMS/Email delivery isn't implemented (it would need a third-party provider).
        messages.info(request, 'Please set up an authenticator app instead.')
        return redirect('userauths:two_factor_setup')


@login_required
def two_factor_enable(request):
    """Turn 2FA on only after the user proves their authenticator produces valid codes."""
    twofa = TwoFactorAuth.objects.filter(user=request.user).first()
    if twofa is None or twofa.method != 'totp' or not twofa.secret_key:
        messages.error(request, 'Set up an authenticator app first.')
        return redirect('userauths:two_factor_setup')
    if twofa.is_enabled:
        messages.info(request, 'Two-Factor Authentication is already enabled.')
        return redirect('userauths:security_dashboard')
    if request.method != 'POST':
        return redirect('userauths:two_factor_configure')

    code = ''.join(ch for ch in request.POST.get('code', '') if ch.isdigit())
    if len(code) != 6 or not verify_totp_code(twofa.secret_key, code):
        messages.error(request, "That code didn't match. Check your app's time is correct and try the newest code.")
        return redirect('userauths:two_factor_configure')

    twofa.is_enabled = True
    twofa.last_used = timezone.now()
    twofa.save()
    SecurityService.log_security_event(
        user=request.user,
        event_type='2fa_enabled',
        request=request,
        details={'method': twofa.method},
        risk_score=SecurityService.assess_risk_score(request, request.user)
    )
    messages.success(request, 'Two-factor authentication is on. You\'ll be asked for a code at each sign-in.')
    return redirect('userauths:security_dashboard')


@login_required
def two_factor_disable(request):
    """Disable Two-Factor Authentication"""
    if request.method == 'POST':
        form = TwoFactorDisableForm(request.user, request.POST)
        if form.is_valid():
            try:
                twofa = request.user.twofactorauth
                twofa.is_enabled = False
                twofa.save()
                
                # Log 2FA disabled
                SecurityService.log_security_event(
                    user=request.user,
                    event_type='2fa_disabled',
                    request=request,
                    details={'method': twofa.method},
                    risk_score=SecurityService.assess_risk_score(request, request.user)
                )
                
                messages.success(request, 'Two-Factor Authentication has been disabled.')
                
            except:
                messages.error(request, 'Two-Factor Authentication not found.')
            
            return redirect('userauths:security_dashboard')
    else:
        form = TwoFactorDisableForm(request.user)
    
    context = {
        'form': form
    }
    
    return render(request, 'userauths/two_factor_disable.html', context)


@login_required
def device_management(request):
    """Manage user devices and sessions"""
    if request.method == 'POST':
        form = DeviceTrustForm(request.POST)
        if form.is_valid():
            device_id = form.cleaned_data.get('device_id')
            action = form.cleaned_data.get('action')
            
            try:
                device = UserDevice.objects.get(device_id=device_id, user=request.user)
                
                if action == 'trust':
                    device.is_trusted = True
                    device.save()
                    
                    SecurityService.log_security_event(
                        user=request.user,
                        event_type='device_trusted',
                        request=request,
                        details={'device_id': device_id, 'device_name': device.device_name},
                        risk_score=SecurityService.assess_risk_score(request, request.user)
                    )
                    
                    messages.success(request, f'Device "{device.device_name}" marked as trusted.')
                    
                elif action == 'untrust':
                    device.is_trusted = False
                    device.save()
                    
                    SecurityService.log_security_event(
                        user=request.user,
                        event_type='device_untrusted',
                        request=request,
                        details={'device_id': device_id, 'device_name': device.device_name},
                        risk_score=SecurityService.assess_risk_score(request, request.user)
                    )
                    
                    messages.success(request, f'Device "{device.device_name}" marked as untrusted.')
                    
                elif action == 'remove':
                    device.is_active = False
                    device.save()
                    
                    SecurityService.log_security_event(
                        user=request.user,
                        event_type='device_removed',
                        request=request,
                        details={'device_id': device_id, 'device_name': device.device_name},
                        risk_score=SecurityService.assess_risk_score(request, request.user)
                    )
                    
                    messages.success(request, f'Device "{device.device_name}" removed.')
                    
            except UserDevice.DoesNotExist:
                messages.error(request, 'Device not found.')
    
    # Get user devices
    devices = UserDevice.objects.filter(user=request.user, is_active=True).order_by('-last_used')
    
    context = {
        'devices': devices,
        'user': request.user
    }
    
    return render(request, 'userauths/device_management.html', context)


@login_required
def security_logs(request):
    """View security logs for the user"""
    # Get filter parameters
    event_type = request.GET.get('event_type')
    risk_level = request.GET.get('risk_level')
    date_from = request.GET.get('date_from')
    date_to = request.GET.get('date_to')
    ip_address = request.GET.get('ip_address')
    
    # Build query
    logs = SecurityLog.objects.filter(user=request.user)
    
    if event_type:
        logs = logs.filter(event_type=event_type)
    
    if risk_level:
        if risk_level == 'low':
            logs = logs.filter(risk_score__lt=30)
        elif risk_level == 'medium':
            logs = logs.filter(risk_score__gte=30, risk_score__lt=70)
        elif risk_level == 'high':
            logs = logs.filter(risk_score__gte=70)
    
    if date_from:
        logs = logs.filter(timestamp__date__gte=date_from)
    
    if date_to:
        logs = logs.filter(timestamp__date__lte=date_to)
    
    if ip_address:
        logs = logs.filter(ip_address__icontains=ip_address)
    
    # Order by timestamp
    logs = logs.order_by('-timestamp')
    
    # Pagination
    paginator = Paginator(logs, 20)
    page_number = request.GET.get('page')
    page_obj = paginator.get_page(page_number)
    
    context = {
        'logs': page_obj,
        'current_page': page_obj.number,
        'total_pages': paginator.num_pages,
        'page_range': paginator.get_elided_page_range(page_obj.number, on_each_side=2, on_ends=1),
        'user': request.user,
        'filter_form': SecurityLogFilterForm(request.GET)
    }
    
    return render(request, 'userauths/security_logs.html', context)


@login_required
def security_settings(request):
    """Update security settings"""
    if request.method == 'POST':
        form = SecuritySettingsForm(request.POST, instance=request.user)
        if form.is_valid():
            current_password = form.cleaned_data.get('current_password')
            new_password = form.cleaned_data.get('new_password')
            security_level = form.cleaned_data.get('security_level')
            
            # Update security level
            if security_level != request.user.security_level:
                request.user.security_level = security_level
                
                SecurityService.log_security_event(
                    user=request.user,
                    event_type='security_settings_changed',
                    request=request,
                    details={'security_level': security_level},
                    risk_score=SecurityService.assess_risk_score(request, request.user)
                )
            
            # Change password if provided
            if new_password:
                request.user.set_password(new_password)
                request.user.save()  # was missing: the new password (and level) were never persisted
                
                SecurityService.log_security_event(
                    user=request.user,
                    event_type='password_change',
                    request=request,
                    risk_score=SecurityService.assess_risk_score(request, request.user)
                )
                
                messages.success(request, 'Password changed successfully. Please login again.')
                logout(request)
                return redirect('userauths:login')
            
            request.user.save()
            messages.success(request, 'Security settings updated successfully.')
            return redirect('userauths:security_dashboard')
    else:
        form = SecuritySettingsForm(instance=request.user)
    
    context = {
        'form': form
    }
    
    return render(request, 'userauths/security_settings.html', context)


@login_required
def regenerate_backup_codes(request):
    """Regenerate backup codes for 2FA"""
    try:
        twofa = request.user.twofactorauth
        if not twofa.is_enabled:
            messages.error(request, 'Two-Factor Authentication is not enabled.')
            return redirect('userauths:security_dashboard')
        
        # Generate new backup codes
        new_codes = twofa.generate_backup_codes()
        
        # Log backup codes regeneration
        SecurityService.log_security_event(
            user=request.user,
            event_type='2fa_backup_codes_regenerated',
            request=request,
            risk_score=SecurityService.assess_risk_score(request, request.user)
        )
        
        messages.success(request, 'Backup codes have been regenerated successfully.')
        
    except:
        messages.error(request, 'Two-Factor Authentication not configured.')
    
    return redirect('userauths:two_factor_configure')


# API endpoints for AJAX requests
@login_required
def get_security_summary_api(request):
    """API endpoint to get security summary"""
    from .services import SecurityService
    
    summary = SecurityService.get_user_security_summary(request.user)
    
    return JsonResponse({
        'security_score': summary['security_score'],
        'failed_login_attempts': summary['failed_login_attempts'],
        'is_locked': summary['is_locked'],
        'active_devices_count': len(summary['active_devices']),
        'recent_events_count': len(summary['recent_events'])
    })


@login_required
def get_recent_security_events_api(request):
    """API endpoint to get recent security events"""
    events = SecurityLog.objects.filter(
        user=request.user
    ).order_by('-timestamp')[:5]
    
    events_data = []
    for event in events:
        events_data.append({
            'event_type': event.get_event_type_display(),
            'timestamp': event.timestamp.strftime('%Y-%m-%d %H:%M'),
            'risk_score': event.risk_score,
            'ip_address': event.ip_address,
            'location': event.location
        })
    
    return JsonResponse({'events': events_data})
