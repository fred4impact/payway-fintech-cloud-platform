"""
DEVELOPMENT/TESTING ONLY - REMOVE BEFORE PRODUCTION DEPLOYMENT

Generates fictional transactions and payment requests between the users created by
`create_test_users`, spread over the last 90 days, so the dashboard charts have data.
Account balances are NOT changed; the dashboard reconstructs history from today's balance.

    python manage.py seed_demo_activity            # adds activity
    python manage.py seed_demo_activity --reset    # removes previously seeded rows first
"""

import random
from datetime import timedelta
from decimal import Decimal

from django.contrib.auth import get_user_model
from django.core.management.base import BaseCommand, CommandError
from django.utils import timezone

from account.models import Account, PaymentRequest, Transaction

User = get_user_model()
SEED_TAG = 'DEMO-SEED'
TEST_EMAILS = ['john.doe@test.com', 'sarah.smith@test.com', 'mike.wilson@test.com']


class Command(BaseCommand):
    help = 'Seed fictional transactions/payment requests for the test users (DEVELOPMENT ONLY)'

    def add_arguments(self, parser):
        parser.add_argument('--reset', action='store_true', help='Delete previously seeded demo rows first')
        parser.add_argument('--per-user', type=int, default=40, help='Approximate transactions per user')

    def handle(self, *args, **opts):
        users = list(User.objects.filter(email__in=TEST_EMAILS))
        if len(users) < 2:
            raise CommandError('Run `python manage.py create_test_users` first.')
        accounts = {a.user_id: a for a in Account.objects.filter(user__in=users)}

        if opts['reset']:
            n_tx, _ = Transaction.objects.filter(reference=SEED_TAG).delete()
            n_req, _ = PaymentRequest.objects.filter(reference=SEED_TAG).delete()
            self.stdout.write(f'Removed {n_tx} transactions and {n_req} payment requests.')

        rng = random.Random(42)
        now = timezone.now()
        created_tx = created_req = 0

        for user in users:
            others = [u for u in users if u.id != user.id]
            for _ in range(opts['per_user']):
                when = now - timedelta(days=rng.uniform(0, 89), hours=rng.uniform(0, 12))
                kind = rng.choices(['in', 'out', 'card_funding', 'withdrawal'], weights=[4, 4, 2, 1])[0]
                other = rng.choice(others)
                amount = Decimal(str(round(rng.uniform(12, 480), 2)))
                if kind == 'in':
                    sender, receiver, ttype = other, user, 'transfer'
                elif kind == 'out':
                    sender, receiver, ttype = user, other, rng.choice(['transfer', 'transfer', 'payment_request'])
                else:
                    sender = receiver = user
                    ttype = kind
                    amount = Decimal(str(round(rng.uniform(50, 900), 2)))
                tx = Transaction.objects.create(
                    transaction_type=ttype, amount=amount, currency='USD',
                    sender=sender, receiver=receiver,
                    sender_account=accounts[sender.id], receiver_account=accounts[receiver.id],
                    status=rng.choices(['completed', 'pending', 'failed'], weights=[18, 1, 1])[0],
                    description='Demo activity', reference=SEED_TAG,
                )
                Transaction.objects.filter(pk=tx.pk).update(created_at=when, completed_at=when)
                created_tx += 1

            for _ in range(rng.randint(5, 9)):
                when = now - timedelta(days=rng.uniform(0, 60))
                other = rng.choice(others)
                status = rng.choices(['paid', 'pending', 'expired', 'cancelled'], weights=[5, 3, 1, 1])[0]
                req = PaymentRequest.objects.create(
                    amount=Decimal(str(round(rng.uniform(15, 250), 2))), currency='USD',
                    description='Demo request', requester=user, recipient=other,
                    requester_account=accounts[user.id], recipient_account=accounts[other.id],
                    status=status, reference=SEED_TAG, expires_at=now + timedelta(days=7),
                    is_expired=status == 'expired',
                )
                PaymentRequest.objects.filter(pk=req.pk).update(created_at=when)
                created_req += 1

        self.stdout.write(self.style.SUCCESS(
            f'Seeded {created_tx} transactions and {created_req} payment requests for {len(users)} test users.'))
