import shutil
import tempfile
from datetime import date, timedelta
from decimal import Decimal
from unittest import mock

import pyotp
from django.conf import settings
from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import RequestFactory, TestCase, override_settings
from django.urls import reverse
from django.utils import timezone

from account.models import (KYC, Account, Bank, CreditCard, Currency, InternationalTransfer, Notification,
                            PaymentRequest, Transaction)
from userauths.models import SecurityLog, TwoFactorAuth, User, UserDevice
from userauths.services import SecurityService

from . import insights, kyc as kyc_checks, review
from .models import RiskDecision
from .payments import score_payment

UA = 'Mozilla/5.0 (Macintosh; Intel Mac OS X 14_0) AppleWebKit/537.36 Chrome/126.0 Safari/537.36'
PASSWORD = 'Str0ng-pass-123!'


FAST_HASHER = ['django.contrib.auth.hashers.MD5PasswordHasher']   # tests only: the real hasher takes ~0.6s
PLAIN_STATIC = {**settings.STORAGES, 'staticfiles': {'BACKEND': 'django.contrib.staticfiles.storage.StaticFilesStorage'}}


def risk_settings(**overrides):
    return override_settings(RISK={**settings.RISK, 'KYC_ASYNC': False, **overrides}, STORAGES=PLAIN_STATIC,
                             PASSWORD_HASHERS=FAST_HASHER)


def make_user(email, balance='5000.00', age_days=400):
    user = User.objects.create_user(username=email.split('@')[0], email=email, password=PASSWORD,
                                    first_name=email.split('@')[0].title(), last_name='Tester')
    User.objects.filter(pk=user.pk).update(date_joined=timezone.now() - timedelta(days=age_days))
    user.refresh_from_db()
    Account.objects.filter(user=user).update(account_balance=Decimal(balance))
    return user


def known_device(user, trusted=False, ua=UA, ip='127.0.0.1'):
    """Register the test client's device for `user`, first seen a month ago."""
    req = RequestFactory().get('/', HTTP_USER_AGENT=ua, REMOTE_ADDR=ip)
    device = UserDevice.objects.create(
        user=user, device_id=SecurityService.generate_device_id(req, user), device_name='Mac - Chrome',
        ip_address=ip, user_agent=ua, is_trusted=trusted)
    UserDevice.objects.filter(pk=device.pk).update(first_used=timezone.now() - timedelta(days=30))
    return device


def payment_history(sender, receiver, n=20, amount='50.00'):
    """`n` completed transfers spread over past weeks, at varied daytime hours."""
    base = timezone.now() - timedelta(days=3)
    for i in range(n):
        tx = Transaction.objects.create(
            sender=sender, receiver=receiver, sender_account=sender.account, receiver_account=receiver.account,
            amount=Decimal(amount), status='completed', description='Lunch')
        when = (base - timedelta(days=i * 2)).replace(hour=9 + i % 9, minute=0)
        Transaction.objects.filter(pk=tx.pk).update(created_at=when)


@override_settings(STORAGES=PLAIN_STATIC, PASSWORD_HASHERS=FAST_HASHER)
class RiskTestCase(TestCase):
    def setUp(self):
        self.alice = make_user('alice@example.com')
        self.bob = make_user('bob@example.com', balance='100.00')
        self.carol = make_user('carol@example.com', balance='100.00')
        self.client.defaults.update(HTTP_USER_AGENT=UA)

    def login(self, user):
        self.client.force_login(user)

    def transfer(self, to, amount, **extra):
        data = {'receiver_account': to.account.account_number, 'amount': str(amount),
                'description': 'Test', 'reference': '', 'stage': 'confirm', **extra}
        return self.client.post(reverse('account:money_transfer'), data)


# ------------------------------------------------------------------ payments

@risk_settings()
class PaymentScoringTests(RiskTestCase):
    def setUp(self):
        super().setUp()
        payment_history(self.alice, self.bob)
        known_device(self.alice)
        self.login(self.alice)

    def test_usual_payment_goes_through_and_is_logged(self):
        resp = self.transfer(self.bob, '50.00')
        tx = Transaction.objects.filter(sender=self.alice, amount=Decimal('50.00')).latest('created_at')
        self.assertRedirects(resp, reverse('account:transaction_detail', args=[tx.transaction_id]))
        decision = RiskDecision.objects.get(kind='payment')
        self.assertEqual(decision.action, 'allow')
        self.assertLess(decision.score, 40)
        self.assertEqual(decision.result_ref, tx.transaction_id)
        self.assertEqual(decision.features['new_recipient'], False)

    def test_large_payment_to_new_recipient_asks_for_password(self):
        resp = self.transfer(self.carol, '600.00')
        self.assertEqual(resp.status_code, 200)
        self.assertContains(resp, "Confirm it")
        self.assertContains(resp, 'Your PayWay password')
        self.assertFalse(Transaction.objects.filter(receiver=self.carol).exists())

        resp = self.transfer(self.carol, '600.00', risk_code='wrong-password')
        self.assertContains(resp, 'That password is not right')
        self.assertFalse(Transaction.objects.filter(receiver=self.carol).exists())
        self.assertTrue(SecurityLog.objects.filter(user=self.alice, event_type='2fa_failed').exists())

        resp = self.transfer(self.carol, '600.00', risk_code=PASSWORD)
        self.assertEqual(resp.status_code, 302)
        self.assertTrue(Transaction.objects.filter(receiver=self.carol, amount=Decimal('600.00')).exists())
        decision = RiskDecision.objects.filter(kind='payment').latest('created_at')
        self.assertEqual(decision.action, 'challenge')
        self.assertIn('Confirmed by the user (password)', decision.reasons)
        self.assertTrue(any('First payment to this recipient' in r for r in decision.reasons))

    def test_challenge_uses_totp_when_2fa_is_on(self):
        secret = pyotp.random_base32()
        TwoFactorAuth.objects.create(user=self.alice, method='totp', is_enabled=True, secret_key=secret)
        resp = self.transfer(self.carol, '600.00')
        self.assertContains(resp, 'authenticator app')
        resp = self.transfer(self.carol, '600.00', risk_code=pyotp.TOTP(secret).now())
        self.assertEqual(resp.status_code, 302)
        self.assertTrue(Transaction.objects.filter(receiver=self.carol).exists())

    def test_three_failed_confirmations_hold_the_payment(self):
        for _ in range(2):
            self.transfer(self.carol, '600.00', risk_code='nope')
        resp = self.transfer(self.carol, '600.00', risk_code='nope')
        decision = RiskDecision.objects.get(review_status='pending')
        self.assertRedirects(resp, reverse('risk:payment_held', args=[decision.pk]))
        self.assertIn('Confirmation failed 3 times', decision.reasons)
        self.assertFalse(Transaction.objects.filter(receiver=self.carol).exists())

    def test_review_page_still_renders_without_scoring(self):
        resp = self.client.post(reverse('account:money_transfer'), {
            'receiver_account': self.carol.account.account_number, 'amount': '600.00', 'stage': 'details'})
        self.assertTemplateUsed(resp, 'account/transfer_review.html')
        self.assertNotContains(resp, 'Confirm it')
        self.assertFalse(RiskDecision.objects.exists())


@risk_settings()
class HeldPaymentTests(RiskTestCase):
    """A brand-new account emptying its balance to a stranger, after failed sign-ins."""

    def setUp(self):
        super().setUp()
        self.mallory = make_user('mallory@example.com', balance='5000.00', age_days=1)
        for _ in range(3):
            SecurityLog.objects.create(user=None, event_type='login_failed', ip_address='10.0.0.9', user_agent=UA,
                                       details={'email': self.mallory.email})
        self.staff = User.objects.create_user(username='ops', email='ops@example.com', password=PASSWORD, is_staff=True)
        self.login(self.mallory)

    def hold_transfer(self):
        resp = self.transfer(self.bob, '4900.00')
        decision = RiskDecision.objects.get(kind='payment', review_status='pending')
        self.assertRedirects(resp, reverse('risk:payment_held', args=[decision.pk]))
        return decision

    def test_risky_payment_is_held_and_nothing_moves(self):
        decision = self.hold_transfer()
        self.assertGreaterEqual(decision.score, 75)
        self.assertEqual(decision.action, 'hold')
        self.assertEqual(Account.objects.get(user=self.mallory).account_balance, Decimal('5000.00'))
        self.assertFalse(Transaction.objects.filter(sender=self.mallory).exists())
        self.assertTrue(Notification.objects.filter(user=self.mallory, title='Payment held for review').exists())
        page = self.client.get(reverse('risk:payment_held', args=[decision.pk]))
        self.assertContains(page, "checking this payment")
        for reason in decision.reasons:          # staff-only detail is not shown to the customer
            self.assertNotContains(page, reason)

    def test_other_users_cannot_see_a_held_payment(self):
        decision = self.hold_transfer()
        self.login(self.alice)
        self.assertEqual(self.client.get(reverse('risk:payment_held', args=[decision.pk])).status_code, 404)

    def test_approving_carries_out_the_payment(self):
        decision = self.hold_transfer()
        ok, _ = review.approve(decision, self.staff)
        self.assertTrue(ok)
        decision.refresh_from_db()
        self.assertEqual(decision.review_status, 'approved')
        tx = Transaction.objects.get(transaction_id=decision.result_ref)
        self.assertEqual(tx.amount, Decimal('4900.00'))
        self.assertEqual(Account.objects.get(user=self.mallory).account_balance, Decimal('51.00'))  # 4900 + 1% fee
        self.assertEqual(Account.objects.get(user=self.bob).account_balance, Decimal('5000.00'))
        self.assertFalse(review.approve(decision, self.staff)[0], 'approving twice must not pay twice')

    def test_rejecting_moves_nothing(self):
        decision = self.hold_transfer()
        self.assertTrue(review.reject(decision, self.staff)[0])
        decision.refresh_from_db()
        self.assertEqual(decision.review_status, 'rejected')
        self.assertEqual(Account.objects.get(user=self.mallory).account_balance, Decimal('5000.00'))
        self.assertTrue(Notification.objects.filter(user=self.mallory, title='Transfer declined').exists())

    def test_approval_fails_cleanly_if_the_balance_is_gone(self):
        decision = self.hold_transfer()
        Account.objects.filter(user=self.mallory).update(account_balance=Decimal('10.00'))
        ok, msg = review.approve(decision, self.staff)
        self.assertFalse(ok)
        decision.refresh_from_db()
        self.assertEqual(decision.review_status, 'failed')
        self.assertFalse(Transaction.objects.filter(sender=self.mallory).exists())

    def test_admin_action_approves(self):
        decision = self.hold_transfer()
        self.client.force_login(User.objects.create_superuser('root', 'root@example.com', PASSWORD))
        resp = self.client.post(reverse('admin:risk_riskdecision_changelist'),
                                {'action': 'approve_selected', '_selected_action': [decision.pk]}, follow=True)
        self.assertEqual(resp.status_code, 200)
        decision.refresh_from_db()
        self.assertEqual(decision.review_status, 'approved')
        detail = self.client.get(reverse('admin:risk_riskdecision_change', args=[decision.pk]))
        self.assertContains(detail, 'Account opened less than a week ago')


@risk_settings(PAYMENT_HOLD=30, PAYMENT_CHALLENGE=20)
class OtherPaymentFlowTests(RiskTestCase):
    """Each flow's held payload must be complete enough to carry the payment out later."""

    def setUp(self):
        super().setUp()
        self.staff = User.objects.create_user(username='ops', email='ops@example.com', password=PASSWORD, is_staff=True)
        self.login(self.alice)

    def approve_pending(self):
        decision = RiskDecision.objects.get(review_status='pending')
        ok, msg = review.approve(decision, self.staff)
        self.assertTrue(ok, msg)
        decision.refresh_from_db()
        return decision

    def test_international_transfer_held_then_approved(self):
        eur = Currency.objects.create(code='EUR', name='Euro', symbol='€', exchange_rate_to_usd=Decimal('0.85'))
        Bank.objects.create(swift_code='DEUTDEFF', bank_name='Deutsche Bank', country='Germany', city='Frankfurt')
        resp = self.client.post(reverse('account:create_international_transfer'), {
            'amount': '3000.00', 'currency': eur.pk, 'recipient_name': 'Hans Muller',
            'recipient_account_number': 'DE89370400440532013000', 'recipient_country': 'Germany',
            'recipient_city': 'Berlin', 'description': 'Invoice 7', 'swift_code': 'DEUTDEFF', 'stage': 'confirm'})
        self.assertEqual(resp.status_code, 302)
        self.assertFalse(InternationalTransfer.objects.exists())
        decision = self.approve_pending()
        transfer = InternationalTransfer.objects.get(transfer_id=decision.result_ref)
        self.assertEqual((transfer.amount, transfer.recipient_bank_id, transfer.description),
                         (Decimal('3000.00'), 'DEUTDEFF', 'Invoice 7'))
        # €3000 + €60 fee at 0.85 EUR/USD
        self.assertEqual(Account.objects.get(user=self.alice).account_balance,
                         Decimal('5000.00') - Decimal('3529.41') - Decimal('70.59'))

    def test_payment_request_settlement_held_then_approved(self):
        pr = PaymentRequest.objects.create(requester=self.carol, recipient=self.alice,
                                           requester_account=self.carol.account, recipient_account=self.alice.account,
                                           amount=Decimal('4800.00'), expires_at=timezone.now() + timedelta(days=3))
        self.client.post(reverse('account:settle_payment_request', args=[pr.request_id]))
        pr.refresh_from_db()
        self.assertEqual(pr.status, 'pending')
        self.approve_pending()
        pr.refresh_from_db()
        self.assertEqual(pr.status, 'paid')
        self.assertEqual(Account.objects.get(user=self.carol).account_balance, Decimal('4900.00'))

    def test_card_top_up_from_a_new_card(self):
        card = CreditCard(user=self.alice, card_holder_name='Alice Tester', expiry_month=12,
                          expiry_year=timezone.now().year + 2, card_type='visa', card_brand='Visa',
                          last_four_digits='1111', masked_number='**** **** **** 1111', daily_limit=Decimal('5000'),
                          monthly_limit=Decimal('9000'))
        card.encrypt_card_data('4111111111111111', '123')
        card.save()
        for _ in range(3):
            SecurityLog.objects.create(user=self.alice, event_type='2fa_failed', ip_address='127.0.0.1', user_agent=UA)
        a = score_payment(self.alice, 'card_fund', Decimal('900.00'), card=card)
        self.assertIn('Card was added in the last 24 hours', a.reasons)
        self.client.post(reverse('account:card_funding'), {'card': card.pk, 'amount': '900.00', 'stage': 'confirm'})
        self.approve_pending()
        self.assertEqual(Account.objects.get(user=self.alice).account_balance, Decimal('5900.00'))


# ------------------------------------------------------------------ sign-in

@risk_settings()
class SigninRiskTests(RiskTestCase):
    def post_login(self, user, ua=UA):
        return self.client.post(reverse('userauths:login'), {'email': user.email, 'password': PASSWORD},
                                HTTP_USER_AGENT=ua)

    def failed_attempts(self, user, n):
        for _ in range(n):
            SecurityLog.objects.create(user=None, event_type='login_failed', ip_address='127.0.0.1',
                                       user_agent=UA, details={'email': user.email})

    def test_normal_sign_in_on_known_device(self):
        known_device(self.alice)
        resp = self.post_login(self.alice)
        self.assertRedirects(resp, reverse('core:dashboard'), fetch_redirect_response=False)
        decision = RiskDecision.objects.get(kind='signin')
        self.assertEqual(decision.action, 'allow')
        self.assertFalse(Notification.objects.filter(user=self.alice, title='Unusual sign-in').exists())

    def test_new_device_after_failed_attempts_is_flagged_but_allowed(self):
        known_device(self.alice, ua='Some other browser 1.0 (Windows NT 10.0)')
        self.failed_attempts(self.alice, 3)
        resp = self.post_login(self.alice)
        self.assertEqual(resp.status_code, 302)
        decision = RiskDecision.objects.get(kind='signin')
        self.assertIn('New device for this account', decision.reasons)
        self.assertGreaterEqual(decision.score, 40)
        self.assertTrue(Notification.objects.filter(user=self.alice, title='Unusual sign-in').exists())
        self.assertTrue(SecurityLog.objects.filter(user=self.alice, event_type='suspicious_activity').exists())

    def test_very_risky_sign_in_without_2fa_is_blocked(self):
        known_device(self.alice, ua='Some other browser 1.0 (Windows NT 10.0)')
        self.failed_attempts(self.alice, 5)
        resp = self.post_login(self.alice, ua='')
        self.assertEqual(resp.status_code, 200)
        self.assertNotIn('_auth_user_id', self.client.session)
        self.assertEqual(RiskDecision.objects.get(kind='signin').action, 'block')
        self.assertTrue(Notification.objects.filter(user=self.alice, title='Sign-in blocked').exists())

    def test_trusted_device_skips_the_2fa_code(self):
        TwoFactorAuth.objects.create(user=self.alice, method='totp', is_enabled=True, secret_key=pyotp.random_base32())
        known_device(self.alice, trusted=True)
        resp = self.post_login(self.alice)
        self.assertRedirects(resp, reverse('core:dashboard'), fetch_redirect_response=False)
        self.assertIn('Trusted device: 2FA code skipped', RiskDecision.objects.get(kind='signin').reasons)

    def test_untrusted_device_still_needs_the_2fa_code(self):
        TwoFactorAuth.objects.create(user=self.alice, method='totp', is_enabled=True, secret_key=pyotp.random_base32())
        known_device(self.alice, trusted=False)
        resp = self.post_login(self.alice)
        self.assertRedirects(resp, reverse('userauths:two_factor_verify'), fetch_redirect_response=False)
        self.assertNotIn('_auth_user_id', self.client.session)

    def test_unusual_sign_in_on_trusted_device_still_needs_the_code(self):
        TwoFactorAuth.objects.create(user=self.alice, method='totp', is_enabled=True, secret_key=pyotp.random_base32())
        known_device(self.alice, trusted=True)
        self.failed_attempts(self.alice, 4)
        resp = self.post_login(self.alice)
        self.assertRedirects(resp, reverse('userauths:two_factor_verify'), fetch_redirect_response=False)

    def test_successful_login_log_records_pre_login_score(self):
        known_device(self.alice, ua='Some other browser 1.0 (Windows NT 10.0)')
        self.post_login(self.alice)
        log = SecurityLog.objects.filter(user=self.alice, event_type='login_success').latest('timestamp')
        self.assertGreaterEqual(log.risk_score, 25)   # new device counted before it was recorded


# ------------------------------------------------------------------ KYC

def _png(colour=(200, 200, 200)):
    import cv2
    import numpy as np
    img = np.full((400, 600, 3), colour, dtype=np.uint8)
    cv2.putText(img, 'PASSPORT', (40, 80), cv2.FONT_HERSHEY_SIMPLEX, 1.5, (0, 0, 0), 3)
    return cv2.imencode('.png', img)[1].tobytes()


class KycHelperTests(TestCase):
    def test_name_match_tolerates_ocr_slips(self):
        text = 'REPUBLIC OF EXAMPLE\nSurname: 0KAFOR\nGiven names: CHIDI JAMES\n'
        self.assertEqual(kyc_checks.name_match('Chidi James Okafor', text), 1.0)
        self.assertLess(kyc_checks.name_match('Maria Gonzalez', text), 0.34)

    def test_dob_formats(self):
        dob = date(1990, 3, 7)
        for text in ['DOB 07/03/1990', 'Date of birth: 07 MAR 1990', '1990-03-07', 'P<GBRSMITH<<JOHN 900307']:
            self.assertTrue(kyc_checks.dob_found(dob, text), text)
        self.assertFalse(kyc_checks.dob_found(dob, 'DOB 08/03/1991'))

    def test_document_type(self):
        self.assertEqual(kyc_checks.classify_document('UNITED KINGDOM PASSPORT'), 'passport')
        self.assertEqual(kyc_checks.classify_document("DRIVING LICENCE"), 'driving_licence')
        self.assertEqual(kyc_checks.classify_document('P<GBRSMITH<<JOHN'), 'passport')
        self.assertEqual(kyc_checks.classify_document('receipt'), 'unknown')


@risk_settings()
class KycCheckTests(RiskTestCase):
    def setUp(self):
        super().setUp()
        self.media = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.media, ignore_errors=True)
        self.override = override_settings(MEDIA_ROOT=self.media)
        self.override.enable()
        self.addCleanup(self.override.disable)
        self.staff = User.objects.create_user(username='ops', email='ops@example.com', password=PASSWORD, is_staff=True)

    def submit(self):
        self.login(self.alice)
        files = {name: SimpleUploadedFile(f'{name}.png', _png(), content_type='image/png')
                 for name in ('id_document', 'proof_of_address', 'selfie_photo')}
        with self.captureOnCommitCallbacks(execute=True):
            return self.client.post(reverse('account:kyc'), {
            'full_name': 'Alice Tester', 'date_of_birth': '1990-03-07', 'nationality': 'British',
            'address': '1 High St', 'city': 'London', 'state': 'London', 'country': 'UK', 'postal_code': 'N1',
            **files})

    def test_submission_runs_checks_and_queues_for_a_person(self):
        with mock.patch('risk.kyc.tesseract_available', return_value=False):
            resp = self.submit()
        self.assertRedirects(resp, reverse('account:kyc_detail'))
        kyc = KYC.objects.get(user=self.alice)
        self.assertIsNotNone(kyc.ai_checked_at)
        self.assertEqual(kyc.verification_status, 'pending')          # never decided automatically
        self.assertFalse(kyc.ai_report['document']['available'])
        self.assertFalse(kyc.face_verified)
        decision = RiskDecision.objects.get(kind='kyc')
        self.assertEqual((decision.review_status, decision.result_ref), ('pending', str(kyc.pk)))

    def test_document_check_with_ocr(self):
        ocr_text = 'UNITED KINGDOM PASSPORT\nSurname TESTER\nGiven names ALICE\nDate of birth 07 MAR 1990'
        with mock.patch('risk.kyc.tesseract_available', return_value=True), \
                mock.patch('pytesseract.image_to_string', return_value=ocr_text), \
                mock.patch('risk.kyc.BLUR_THRESHOLD', 0):
            self.submit()
        kyc = KYC.objects.get(user=self.alice)
        doc = kyc.ai_report['document']
        self.assertEqual((doc['name_match'], doc['dob_found'], doc['document_type']), (1.0, True, 'passport'))
        self.assertTrue(kyc.document_verified)
        self.assertGreater(kyc.verification_confidence, 0)
        self.assertIn(kyc.ai_verification_status, ('review', 'passed'))

    def test_rerun_replaces_the_queue_item_and_approval_verifies_user(self):
        with mock.patch('risk.kyc.tesseract_available', return_value=False):
            self.submit()
            kyc = KYC.objects.get(user=self.alice)
            kyc_checks.run_checks(kyc)
        self.assertEqual(RiskDecision.objects.filter(kind='kyc', review_status='pending').count(), 1)
        ok, _ = review.approve(RiskDecision.objects.get(kind='kyc'), self.staff)
        self.assertTrue(ok)
        kyc.refresh_from_db()
        self.alice.refresh_from_db()
        self.assertEqual(kyc.verification_status, 'approved')
        self.assertTrue(self.alice.is_verified)

    def test_approving_in_kyc_admin_clears_the_queue(self):
        with mock.patch('risk.kyc.tesseract_available', return_value=False):
            self.submit()
        kyc = KYC.objects.get(user=self.alice)
        kyc.verification_status = 'rejected'
        kyc.save()
        self.assertEqual(RiskDecision.objects.get(kind='kyc').review_status, 'rejected')

    def test_unreadable_upload_does_not_crash(self):
        with mock.patch('risk.kyc.tesseract_available', return_value=False):
            self.submit()
        kyc = KYC.objects.get(user=self.alice)
        kyc.id_document.save('broken.png', SimpleUploadedFile('broken.png', b'not an image'), save=True)
        kyc_checks.run_checks(kyc)
        kyc.refresh_from_db()
        self.assertEqual(kyc.ai_verification_status, 'unavailable')


# ------------------------------------------------------------------ insights

class InsightTests(TestCase):
    def test_categorise(self):
        cases = {
            ('Rent for March', 'transfer'): 'Rent & housing',
            ('Uber to airport', 'transfer'): 'Transport',
            ('pizza night', 'transfer'): 'Dining',
            ('Netflix + electricity', 'transfer'): 'Bills & utilities',
            ('happy birthday sis', 'transfer'): 'Family & friends',
            ('', 'transfer'): 'Transfers to people',
            ('anything', 'withdrawal'): 'Card withdrawals',
            ('Invoice 7', 'international'): 'International',
        }
        for (desc, kind), expected in cases.items():
            self.assertEqual(insights.categorise(desc, kind), expected, desc)

    def test_forecast_declining_balance(self):
        f = insights.forecast_balance([1000 - 20 * d for d in range(30)], horizon=14)
        self.assertAlmostEqual(f['slope_per_day'], -20, places=1)
        self.assertEqual(f['values'][0], 420 - 20)     # trend continues from the last actual balance (420)
        self.assertEqual(f['runs_out_in_days'], 21)
        self.assertIn('run out in about 21 days', insights.forecast_flag(f)['text'])

    def test_forecast_needs_movement(self):
        self.assertIsNone(insights.forecast_balance([500] * 30))
        self.assertIsNone(insights.forecast_balance([1, 2, 3]))

    def test_spending_flags(self):
        flags = insights.spending_flags(
            {'Dining': 400, 'Travel': 900}, {'Dining': 150, 'Groceries': 300}, 30,
            history_amounts=[40, 50, 45, 60, 55, 50, 48, 52],
            current_payments=[(timezone.now(), 900.0, 'Sky Airlines')])
        text = ' '.join(f['text'] for f in flags)
        self.assertIn('Dining spending is up 167%', text)
        self.assertIn('New this period: $900 on travel', text)
        self.assertIn('Groceries spending is down 100%', text)
        self.assertIn('Unusually large payment: $900.00 to Sky Airlines', text)


class DashboardInsightTests(RiskTestCase):
    def test_dashboard_shows_categories_forecast_and_flags(self):
        payment_history(self.alice, self.bob, n=12)
        for i, desc in enumerate(['Rent', 'Rent', 'pizza']):
            Transaction.objects.create(sender=self.alice, receiver=self.bob, sender_account=self.alice.account,
                                       receiver_account=self.bob.account, amount=Decimal('800.00'),
                                       status='completed', description=desc)
        self.login(self.alice)
        resp = self.client.get(reverse('core:dashboard'))
        self.assertEqual(resp.status_code, 200)
        labels = [m['label'] for m in resp.context['mix']]
        self.assertIn('Rent & housing', labels)
        self.assertIn('Dining', labels)
        self.assertIsNotNone(resp.context['chart_data']['forecast'])
        self.assertEqual(len(resp.context['chart_data']['forecast']['values']), 14)
        self.assertContains(resp, 'Insights')


# ------------------------------------------------------------------ redesigned pages

class SecurityAndSettingsPageTests(RiskTestCase):
    def test_security_page_score_reflects_protections(self):
        known_device(self.alice)
        self.login(self.alice)
        resp = self.client.get(reverse('userauths:security_dashboard'))
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.context['score'], 40)            # password only, no 2FA / KYC / trusted devices
        self.assertContains(resp, 'Add a second lock to your account')
        self.assertContains(resp, 'pwx-gauge-svg')

        TwoFactorAuth.objects.create(user=self.alice, method='totp', is_enabled=True, secret_key=pyotp.random_base32())
        UserDevice.objects.filter(user=self.alice).update(is_trusted=True)
        resp = self.client.get(reverse('userauths:security_dashboard'))
        self.assertEqual(resp.context['score'], 85)
        self.assertEqual(resp.context['checks_done'], 3)

    def test_settings_page_shows_money_in_out_and_people(self):
        payment_history(self.alice, self.bob, n=6)
        Transaction.objects.create(sender=self.alice, receiver=self.bob, sender_account=self.alice.account,
                                   receiver_account=self.bob.account, amount=Decimal('120.00'),
                                   status='completed', description='Rent share')
        self.login(self.alice)
        resp = self.client.get(reverse('account:account_info'))
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.context['stats'][1]['value'], Decimal('420.00'))   # 6 x $50 + $120, last 30 days
        self.assertEqual(resp.context['people'][0]['name'], 'Bob')
        self.assertIn('Rent & housing', [s['label'] for s in resp.context['spend']])
        self.assertContains(resp, f'?to={self.bob.account.account_number}')
