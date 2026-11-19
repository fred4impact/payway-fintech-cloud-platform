"""Carrying out payments.

Each function moves the money for one payment flow inside a single database transaction,
re-checking balances and limits under row locks. The views call them once a payment has
passed its risk check, and the risk review queue calls them when staff approve a payment
that was held, so a held payment runs through exactly the same code as a normal one.

They raise ValueError with a user-facing message when the payment can't go ahead.
"""
from decimal import Decimal

from django.db import transaction
from django.utils import timezone
from datetime import timedelta

from .models import Account, Bank, CreditCard, Currency, InternationalTransfer, Notification, PaymentRequest, Transaction

TRANSFER_FEE_RATE = Decimal('0.01')

INTERNATIONAL_FEES = {
    'USD': ('0.02', '5.00', '50.00'),
    'EUR': ('0.025', '5.00', '60.00'),
    'GBP': ('0.03', '5.00', '75.00'),
    'JPY': ('0.025', '500.00', '5000.00'),
    'CAD': ('0.025', '6.00', '60.00'),
    'AUD': ('0.025', '6.00', '65.00'),
    'CHF': ('0.025', '5.00', '55.00'),
    'SGD': ('0.025', '6.00', '65.00'),
    'HKD': ('0.025', '40.00', '400.00'),
    'NZD': ('0.025', '7.00', '70.00'),
}


def _name(user):
    return user.get_full_name() or user.email


def transfer_fee(amount):
    return (amount * TRANSFER_FEE_RATE).quantize(Decimal('0.01'))


def international_fee(amount, currency_code):
    """Fee in the transfer's own currency: a percentage, clamped to a per-currency min/max."""
    pct, lo, hi = (Decimal(v) for v in INTERNATIONAL_FEES.get(currency_code, INTERNATIONAL_FEES['USD']))
    return max(lo, min(Decimal(amount) * pct, hi)).quantize(Decimal('0.01'))


def international_quote(amount, currency):
    """(fee, amount_usd, fee_usd, debit_usd) for sending `amount` of `currency`."""
    rate = currency.exchange_rate_to_usd          # units of `currency` per 1 USD (EUR 0.85, JPY 110)
    fee = international_fee(amount, currency.code)
    amount_usd = (amount / rate).quantize(Decimal('0.01'))
    fee_usd = (fee / rate).quantize(Decimal('0.01'))
    return fee, amount_usd, fee_usd, amount_usd + fee_usd


def execute_transfer(user, receiver_account_id, amount, description='', reference=''):
    amount = Decimal(amount)
    fee = transfer_fee(amount)
    total = amount + fee
    sender_pk = Account.objects.only('pk').get(user=user).pk
    with transaction.atomic():
        # Lock both rows in a stable order so concurrent transfers can't overspend.
        locked = {a.pk: a for a in Account.objects.select_for_update()
                  .filter(pk__in=[sender_pk, receiver_account_id]).order_by('pk')}
        sender, receiver = locked[sender_pk], locked.get(int(receiver_account_id))
        if receiver is None or receiver.pk == sender.pk or not receiver.is_active:
            raise ValueError('The recipient account is not available.')
        if sender.account_balance < total:
            raise ValueError(f'Insufficient balance. You need ${total:,.2f} including the ${fee:,.2f} fee.')

        tx = Transaction.objects.create(
            sender=user, receiver=receiver.user,
            sender_account=sender, receiver_account=receiver,
            amount=amount, transaction_fee=fee, total_amount=total,
            description=description, reference=reference,
            status='completed', completed_at=timezone.now(), is_verified=True,
        )
        sender.account_balance -= total
        sender.save(update_fields=['account_balance'])
        receiver.account_balance += amount
        receiver.save(update_fields=['account_balance'])

        Notification.objects.create(
            user=user, notification_type='transaction', title='Money Sent Successfully',
            message=f'You sent ${amount:,.2f} to {_name(receiver.user)}', transaction=tx, is_email_sent=True)
        Notification.objects.create(
            user=receiver.user, notification_type='transaction', title='Money Received',
            message=f'You received ${amount:,.2f} from {_name(user)}', transaction=tx, is_email_sent=True)
    return tx


def execute_settlement(user, payment_request_id):
    with transaction.atomic():
        # Re-read the request and lock both accounts so it can't be paid twice concurrently.
        pr = PaymentRequest.objects.select_for_update().get(pk=payment_request_id, recipient=user)
        if pr.status != 'pending' or pr.is_expired_property():
            raise ValueError('This payment request can no longer be paid.')
        ids = sorted([pr.recipient_account_id, pr.requester_account_id])
        locked = {a.pk: a for a in Account.objects.select_for_update().filter(pk__in=ids).order_by('pk')}
        payer, payee = locked[pr.recipient_account_id], locked[pr.requester_account_id]
        if payer.account_balance < pr.amount:
            raise ValueError('Insufficient balance to settle this request.')

        tx = Transaction.objects.create(
            sender=user, receiver=pr.requester, sender_account=payer, receiver_account=payee,
            amount=pr.amount, transaction_type='payment_request', description=pr.description,
            reference=pr.request_id, status='completed', completed_at=timezone.now(), is_verified=True,
        )
        payer.account_balance -= pr.amount
        payer.save(update_fields=['account_balance'])
        payee.account_balance += pr.amount
        payee.save(update_fields=['account_balance'])
        pr.status = 'paid'
        pr.paid_at = timezone.now()
        pr.save(update_fields=['status', 'paid_at', 'updated_at'])

        Notification.objects.create(
            user=user, notification_type='transaction', title='Payment Request Settled',
            message=f'You paid ${pr.amount:,.2f} to {_name(pr.requester)}', transaction=tx, is_email_sent=True)
        Notification.objects.create(
            user=pr.requester, notification_type='transaction', title='Payment Request Received',
            message=f'You received ${pr.amount:,.2f} from {_name(user)}', transaction=tx, is_email_sent=True)
    return tx


def execute_card_move(user, card_id, amount, description, direction):
    """Top up the account from a card ('fund') or withdraw to it ('withdraw')."""
    amount = Decimal(amount)
    is_fund = direction == 'fund'
    with transaction.atomic():
        account = Account.objects.select_for_update().get(user=user)
        card = CreditCard.objects.select_for_update().get(pk=card_id, user=user)
        if is_fund and not card.can_fund(amount):
            raise ValueError('This card has reached its daily or monthly funding limit.')
        if not is_fund and (not card.can_withdraw(amount) or account.account_balance < amount):
            raise ValueError('Insufficient balance for this withdrawal.')

        label = 'Card funding' if is_fund else 'Card withdrawal'
        tx = Transaction.objects.create(
            transaction_type='card_funding' if is_fund else 'withdrawal',
            amount=amount, currency='USD',
            sender=user, receiver=user, sender_account=account, receiver_account=account,
            status='completed',
            description=f'{label}: {description}' if description else label,
            reference=card.funding_reference if is_fund else f'CARD_WITHDRAW_{card.last_four_digits}',
            transaction_fee=Decimal('0.00'), total_amount=amount,
            is_verified=True, completed_at=timezone.now(),
        )
        account.account_balance += amount if is_fund else -amount
        account.save(update_fields=['account_balance'])
        if is_fund:
            card.total_funded += amount
        else:
            card.total_withdrawn += amount
        card.last_used = timezone.now()
        card.save()
        Notification.objects.create(
            user=user, notification_type='transaction',
            title='Account Funded' if is_fund else 'Withdrawal Completed',
            message=(f'Your account has been funded with ${amount:,.2f} from your {card.card_brand} card ending in {card.last_four_digits}.'
                     if is_fund else
                     f'${amount:,.2f} has been withdrawn to your {card.card_brand} card ending in {card.last_four_digits}.'),
            transaction=tx)
    return tx


def execute_international(user, data):
    """`data` holds the transfer form's cleaned values, with currency as a code and the bank's SWIFT code."""
    currency = Currency.objects.get(code=data['currency'], is_active=True)
    bank = Bank.objects.get(swift_code=data['swift_code'], is_active=True)
    amount = Decimal(data['amount'])
    fee, amount_usd, fee_usd, debit_usd = international_quote(amount, currency)
    with transaction.atomic():
        account = Account.objects.select_for_update().get(user=user)
        if account.account_balance < debit_usd:
            raise ValueError(f'Insufficient balance. This transfer needs ${debit_usd:,.2f} including fees.')
        transfer = InternationalTransfer.objects.create(
            sender=user, sender_account=account,
            amount=amount, currency=currency, exchange_rate=currency.exchange_rate_to_usd,
            amount_in_usd=amount_usd, transfer_fee=fee, total_amount=amount + fee,
            recipient_name=data['recipient_name'], recipient_bank=bank,
            recipient_account_number=data['recipient_account_number'], recipient_swift_code=bank.swift_code,
            recipient_country=data['recipient_country'], recipient_city=data['recipient_city'],
            description=data.get('description', ''),
            estimated_delivery=timezone.now() + timedelta(days=3),
        )
        account.account_balance -= debit_usd
        account.save(update_fields=['account_balance'])
        Notification.objects.create(
            user=user, notification_type='transaction', title='International Transfer Initiated',
            message=f'Your transfer of {currency.symbol}{amount:,.2f} to {transfer.recipient_name} has been initiated. '
                    f'Transfer ID: {transfer.transfer_id}')
    return transfer


EXECUTORS = {
    'transfer': lambda user, p: execute_transfer(user, p['receiver_account_id'], p['amount'],
                                                 p.get('description', ''), p.get('reference', '')),
    'settlement': lambda user, p: execute_settlement(user, p['payment_request_id']),
    'card_fund': lambda user, p: execute_card_move(user, p['card_id'], p['amount'], p.get('description', ''), 'fund'),
    'card_withdraw': lambda user, p: execute_card_move(user, p['card_id'], p['amount'], p.get('description', ''), 'withdraw'),
    'international': lambda user, p: execute_international(user, p),
}


def execute(flow, user, payload):
    """Run a payment from its stored payload (used when staff approve a held payment)."""
    return EXECUTORS[flow](user, payload)


def reference_of(result):
    return getattr(result, 'transaction_id', None) or getattr(result, 'transfer_id', '')
