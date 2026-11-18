from django.shortcuts import render, redirect, get_object_or_404
from django.contrib.auth.decorators import login_required
from django.contrib import messages
from django.db import transaction, models
from django.http import JsonResponse, HttpResponse
from django.core.paginator import Paginator
from django.utils import timezone
from datetime import datetime, timedelta
from decimal import Decimal
import csv
from .forms import KYCForm, AccountSearchForm, MoneyTransferForm, PaymentRequestForm, TransactionFilterForm, CreditCardForm, CardFundingForm, CardWithdrawalForm, InternationalTransferForm, SwiftCodeSearchForm, CurrencyConverterForm
from .models import KYC, Account, Transaction, PaymentRequest, Notification, CreditCard, Currency, Bank, InternationalTransfer
from userauths.models import User


@login_required
def kyc_view(request):
    """Handle KYC form submission and display"""
    try:
        kyc = KYC.objects.get(user=request.user)
        # If KYC already exists, show the details
        return render(request, 'account/kyc_detail.html', _kyc_context(kyc))
    except KYC.DoesNotExist:
        # If KYC doesn't exist, show the form
        if request.method == 'POST':
            form = KYCForm(request.POST, request.FILES)
            if form.is_valid():
                kyc = form.save(commit=False)
                kyc.user = request.user
                kyc.save()
                messages.success(request, 'KYC submitted successfully! We will review your documents.')
                return redirect('account:kyc_detail')
            else:
                messages.error(request, 'Please correct the errors below.')
        else:
            form = KYCForm()
        
        return render(request, 'account/kyc.html', {'form': form})


@login_required
def kyc_detail_view(request):
    """Display KYC details"""
    kyc = KYC.objects.filter(user=request.user).first()
    if kyc is None:
        messages.warning(request, 'Please complete your KYC first.')
        return redirect('account:kyc')
    return render(request, 'account/kyc_detail.html', _kyc_context(kyc))


def _kyc_context(kyc):
    """Review checklist + progress figures for the verification page."""
    docs = [
        {'label': 'Government ID', 'file': kyc.id_document, 'icon': 'fa-id-card', 'tone': 'brand'},
        {'label': 'Proof of address', 'file': kyc.proof_of_address, 'icon': 'fa-file-lines', 'tone': 'blue'},
        {'label': 'Selfie with ID', 'file': kyc.selfie_photo, 'icon': 'fa-camera', 'tone': 'violet'},
    ]
    status = kyc.verification_status
    checks = [
        {'label': 'Details submitted', 'state': 'done'},
        {'label': 'Documents uploaded', 'state': 'done' if all(d['file'] for d in docs) else 'pending'},
        {'label': 'Document check', 'state': 'done' if kyc.document_verified else ('failed' if status == 'rejected' else 'progress')},
        {'label': 'Face match', 'state': 'done' if kyc.face_verified else ('failed' if status == 'rejected' else 'progress')},
        {'label': 'Final review', 'state': {'approved': 'done', 'rejected': 'failed'}.get(status, 'pending')},
    ]
    n = len(checks)
    done = sum(c['state'] == 'done' for c in checks)
    progress = sum(c['state'] == 'progress' for c in checks)
    return {
        'kyc': kyc,
        'docs': docs,
        'docs_uploaded': sum(1 for d in docs if d['file']),
        'checks': checks,
        'done_value': f"{round(done / n * 100)}%",
        'done_pct': round(done / n * 100),
        'progress_pct': round(progress / n * 100),
        'pending_pct': 100 - round(done / n * 100) - round(progress / n * 100),
        'confidence': round(kyc.verification_confidence or 0),
        'risk_pct': round((kyc.risk_score or 0) * 100) if (kyc.risk_score or 0) <= 1 else round(kyc.risk_score),
    }


@login_required
def account_info_view(request):
    """Profile + account overview (spec 12)."""
    account = Account.objects.filter(user=request.user).first()
    if account is None:
        messages.error(request, 'Account not found.')
        return redirect('core:dashboard')
    user = request.user
    kyc = KYC.objects.filter(user=user).first()
    completed = Transaction.objects.filter(status='completed')
    sent = completed.filter(sender=user).exclude(receiver=user)
    received = completed.filter(receiver=user).exclude(sender=user)
    sum_of = lambda qs: qs.aggregate(s=models.Sum('amount'))['s'] or Decimal('0.00')

    recent = []
    for tx in (Transaction.objects.filter(models.Q(sender=user) | models.Q(receiver=user))
               .select_related('sender', 'receiver').order_by('-created_at')[:6]):
        self_tx = tx.sender_id == tx.receiver_id
        incoming = (tx.transaction_type in ('card_funding', 'deposit')) if self_tx else tx.receiver_id == user.id
        other = tx.sender if tx.receiver_id == user.id else tx.receiver
        name = ('Card top-up' if tx.transaction_type == 'card_funding' else tx.get_transaction_type_display()) if self_tx \
            else (other.get_full_name() or other.username)
        recent.append({'tx': tx, 'incoming': incoming, 'name': name})

    twofa = getattr(user, 'twofactorauth', None) if hasattr(user, 'twofactorauth') else None
    checks = [
        {'label': 'Email address', 'done': bool(user.email)},
        {'label': 'Phone number', 'done': bool(user.phone_number)},
        {'label': 'Date of birth', 'done': bool(user.date_of_birth)},
        {'label': 'Identity verified', 'done': bool(kyc and kyc.verification_status == 'approved')},
        {'label': 'Two-factor on', 'done': bool(twofa and twofa.is_enabled)},
    ]
    done = sum(c['done'] for c in checks)
    pct = round(done / len(checks) * 100)

    context = {
        'account': account,
        'kyc': kyc,
        'sent_count': sent.count(), 'sent_total': sum_of(sent),
        'received_count': received.count(), 'received_total': sum_of(received),
        'cards_count': user.credit_cards.filter(status='active').count(),
        'pending_requests': PaymentRequest.objects.filter(recipient=user, status='pending', is_expired=False).count(),
        'recent': recent,
        'twofa': twofa,
        'checks': checks,
        'profile_pct': pct,
        'profile_pending': 100 - pct,
        'profile_value': f'{pct}%',
    }
    return render(request, 'account/account_info.html', context)


# Money Transfer Views
@login_required
def search_account_view(request):
    """Search for users by account number or email"""
    form = AccountSearchForm(request.GET or None)
    search_results = []
    
    if form.is_valid():
        query = form.cleaned_data['search_query']
        
        # Search by account number
        try:
            account = Account.objects.get(account_number=query, is_active=True)
            if account.user != request.user:
                search_results.append({
                    'account': account,
                    'user': account.user,
                    'match_type': 'account_number'
                })
        except Account.DoesNotExist:
            pass
        
        # Search by email
        users = User.objects.filter(email__icontains=query)
        for user in users:
            if user != request.user:
                try:
                    account = Account.objects.get(user=user, is_active=True)
                    search_results.append({
                        'account': account,
                        'user': user,
                        'match_type': 'email'
                    })
                except Account.DoesNotExist:
                    pass
    
    context = {
        'form': form,
        'search_results': search_results,
    }
    return render(request, 'account/search_account.html', context)


TRANSFER_FEE_RATE = Decimal('0.01')


def _flow_stage(request):
    """'details' (first POST), 'confirm' (review accepted) or 'edit' (back from review)."""
    return request.POST.get('stage', 'details') if request.method == 'POST' else None


@login_required
def money_transfer_view(request):
    """Send money: details -> review -> confirm (spec 13-16)."""
    sender_account = Account.objects.filter(user=request.user).first()
    if sender_account is None:
        messages.error(request, 'Your account was not found.')
        return redirect('core:dashboard')

    stage = _flow_stage(request)
    initial = {'receiver_account': request.GET.get('to', '')}
    form = MoneyTransferForm(request.POST or None, initial=initial)
    context = {'form': form, 'account': sender_account, 'fee_rate': TRANSFER_FEE_RATE}

    if stage and stage != 'edit' and form.is_valid():
        receiver_account = form.cleaned_data['receiver_account']
        amount = form.cleaned_data['amount']
        fee = (amount * TRANSFER_FEE_RATE).quantize(Decimal('0.01'))
        total = amount + fee

        if receiver_account == sender_account:
            form.add_error('receiver_account', 'You cannot transfer money to your own account.')
        elif sender_account.account_balance < total:
            form.add_error('amount', f'Insufficient balance. You need ${total:,.2f} including the ${fee:,.2f} fee.')

        if not form.errors and stage != 'confirm':
            context.update({'receiver_account': receiver_account, 'amount': amount, 'fee': fee, 'total': total,
                            'balance_after': sender_account.account_balance - total})
            return render(request, 'account/transfer_review.html', context)

        if not form.errors and stage == 'confirm':
            try:
                with transaction.atomic():
                    # Lock both rows in a stable order so concurrent transfers can't overspend.
                    locked = {a.pk: a for a in Account.objects.select_for_update()
                              .filter(pk__in=[sender_account.pk, receiver_account.pk]).order_by('pk')}
                    sender, receiver = locked[sender_account.pk], locked[receiver_account.pk]
                    if sender.account_balance < total:
                        raise ValueError(f'Insufficient balance. You need ${total:,.2f} including the ${fee:,.2f} fee.')

                    transfer = Transaction.objects.create(
                        sender=request.user, receiver=receiver.user,
                        sender_account=sender, receiver_account=receiver,
                        amount=amount, transaction_fee=fee, total_amount=total,
                        description=form.cleaned_data['description'],
                        reference=form.cleaned_data['reference'],
                        status='completed', completed_at=timezone.now(),
                    )
                    sender.account_balance -= total
                    sender.save(update_fields=['account_balance'])
                    receiver.account_balance += amount
                    receiver.save(update_fields=['account_balance'])

                    receiver_name = receiver.user.get_full_name() or receiver.user.email
                    Notification.objects.create(
                        user=request.user, notification_type='transaction', title='Money Sent Successfully',
                        message=f'You sent ${amount:,.2f} to {receiver_name}', transaction=transfer, is_email_sent=True)
                    Notification.objects.create(
                        user=receiver.user, notification_type='transaction', title='Money Received',
                        message=f'You received ${amount:,.2f} from {request.user.get_full_name() or request.user.email}',
                        transaction=transfer, is_email_sent=True)

                messages.success(request, f'Successfully sent ${amount:,.2f} to {receiver_name}')
                return redirect('account:transaction_detail', transaction_id=transfer.transaction_id)
            except ValueError as e:
                form.add_error('amount', str(e))
            except Exception as e:
                messages.error(request, f'Transfer failed: {e}')

    return render(request, 'account/money_transfer.html', context)


# Payment Request Views
@login_required
def create_payment_request_view(request):
    """Request money: details -> review -> send (spec 19-24)."""
    requester_account = Account.objects.filter(user=request.user).first()
    if requester_account is None:
        messages.error(request, 'Your account was not found.')
        return redirect('core:dashboard')

    stage = _flow_stage(request)
    form = PaymentRequestForm(request.POST or None, initial={'recipient_account': request.GET.get('from', '')})
    context = {
        'form': form,
        'account': requester_account,
        'pending_count': PaymentRequest.objects.filter(requester=request.user, status='pending').count(),
        'paid_count': PaymentRequest.objects.filter(requester=request.user, status='paid').count(),
        'expired_count': PaymentRequest.objects.filter(requester=request.user, status='expired').count(),
        'total_requested': PaymentRequest.objects.filter(requester=request.user)
                           .aggregate(s=models.Sum('amount'))['s'] or 0,
    }

    if stage and stage != 'edit' and form.is_valid():
        recipient_account = form.cleaned_data['recipient_account']
        amount = form.cleaned_data['amount']
        expires_in_days = int(form.cleaned_data['expires_in_days'])
        if recipient_account == requester_account:
            form.add_error('recipient_account', 'You cannot request money from your own account.')

        if not form.errors and stage != 'confirm':
            context.update({'recipient_account': recipient_account, 'amount': amount,
                            'expires_at': timezone.now() + timedelta(days=expires_in_days),
                            'expires_in_days': expires_in_days})
            return render(request, 'account/request_review.html', context)

        if not form.errors and stage == 'confirm':
            payment_request = PaymentRequest.objects.create(
                requester=request.user, recipient=recipient_account.user,
                requester_account=requester_account, recipient_account=recipient_account,
                amount=amount, description=form.cleaned_data['description'],
                expires_at=timezone.now() + timedelta(days=expires_in_days),
            )
            Notification.objects.create(
                user=recipient_account.user, notification_type='payment_request', title='Payment Request Received',
                message=f'{request.user.get_full_name() or request.user.email} is requesting ${amount:,.2f} from you',
                payment_request=payment_request, is_email_sent=True)
            messages.success(request, f'Payment request sent to {recipient_account.user.get_full_name() or recipient_account.user.email}')
            return redirect('account:payment_request_detail', request_id=payment_request.request_id)

    return render(request, 'account/create_payment_request.html', context)


@login_required
def payment_requests_view(request):
    """View all payment requests (sent and received)"""
    sent_requests = PaymentRequest.objects.filter(requester=request.user).order_by('-created_at')
    received_requests = PaymentRequest.objects.filter(recipient=request.user).order_by('-created_at')
    
    # Check expiration for all requests
    for payment_request in list(sent_requests) + list(received_requests):
        payment_request.check_expiration()
    
    context = {
        'sent_requests': sent_requests,
        'received_requests': received_requests,
    }
    return render(request, 'account/payment_requests.html', context)


@login_required
def settle_payment_request_view(request, request_id):
    """Settle a payment request"""
    payment_request = get_object_or_404(PaymentRequest, request_id=request_id, recipient=request.user)
    
    # Check expiration
    payment_request.check_expiration()
    
    if payment_request.status != 'pending':
        messages.error(request, 'This payment request cannot be settled.')
        return redirect('account:payment_requests')
    
    if payment_request.is_expired:
        messages.error(request, 'This payment request has expired.')
        return redirect('account:payment_requests')
    
    if request.method == 'POST':
        try:
            with transaction.atomic():
                # Re-read the request and lock both accounts so it can't be paid twice concurrently.
                payment_request = PaymentRequest.objects.select_for_update().get(pk=payment_request.pk)
                if payment_request.status != 'pending':
                    messages.error(request, 'This payment request has already been settled.')
                    return redirect('account:payment_requests')
                ids = sorted([payment_request.recipient_account_id, payment_request.requester_account_id])
                locked = {a.pk: a for a in Account.objects.select_for_update().filter(pk__in=ids).order_by('pk')}
                recipient_account = locked[payment_request.recipient_account_id]
                requester_account = locked[payment_request.requester_account_id]

                if recipient_account.account_balance < payment_request.amount:
                    messages.error(request, 'Insufficient balance to settle this request.')
                    return redirect('account:settle_payment_request', request_id=payment_request.request_id)

                # Create transaction
                transfer = Transaction.objects.create(
                    sender=request.user,
                    receiver=payment_request.requester,
                    sender_account=recipient_account,
                    receiver_account=requester_account,
                    amount=payment_request.amount,
                    transaction_type='payment_request',
                    description=payment_request.description,
                    status='completed'
                )
                
                # Update account balances
                recipient_account.account_balance -= payment_request.amount
                recipient_account.save()
                
                requester_account.account_balance += payment_request.amount
                requester_account.save()
                
                # Update payment request status
                payment_request.status = 'paid'
                payment_request.paid_at = timezone.now()
                payment_request.save()
                
                # Create notifications
                Notification.objects.create(
                    user=request.user,
                    notification_type='transaction',
                    title='Payment Request Settled',
                    message=f'You paid ${payment_request.amount:.2f} to {payment_request.requester.get_full_name() or payment_request.requester.email}',
                    transaction=transfer,
                    is_email_sent=True
                )
                
                Notification.objects.create(
                    user=payment_request.requester,
                    notification_type='transaction',
                    title='Payment Request Received',
                    message=f'You received ${payment_request.amount:.2f} from {request.user.get_full_name() or request.user.email}',
                    transaction=transfer,
                    is_email_sent=True
                )
                
                messages.success(request, f'Successfully paid ${payment_request.amount:.2f} to {payment_request.requester.get_full_name() or payment_request.requester.email}')
                return redirect('account:transaction_detail', transaction_id=transfer.transaction_id)
                
        except Exception as e:
            messages.error(request, f'Settlement failed: {str(e)}')
            return redirect('account:payment_requests')
    
    account = Account.objects.get(user=request.user)
    context = {
        'payment_request': payment_request,
        'account': account,
        'balance_after': account.account_balance - payment_request.amount,
        'can_pay': account.account_balance >= payment_request.amount,
    }
    return render(request, 'account/settle_payment_request.html', context)


# Transaction Management Views
@login_required
def transactions_view(request):
    """View all transactions with filtering and pagination"""
    form = TransactionFilterForm(request.GET or None)
    transactions = Transaction.objects.filter(
        models.Q(sender=request.user) | models.Q(receiver=request.user)
    ).order_by('-created_at')
    
    if form.is_valid():
        # Apply filters
        if form.cleaned_data.get('transaction_type'):
            transactions = transactions.filter(transaction_type=form.cleaned_data['transaction_type'])
        
        if form.cleaned_data.get('status'):
            transactions = transactions.filter(status=form.cleaned_data['status'])
        
        if form.cleaned_data.get('date_from'):
            transactions = transactions.filter(created_at__date__gte=form.cleaned_data['date_from'])
        
        if form.cleaned_data.get('date_to'):
            transactions = transactions.filter(created_at__date__lte=form.cleaned_data['date_to'])
        
        if form.cleaned_data.get('min_amount'):
            transactions = transactions.filter(amount__gte=form.cleaned_data['min_amount'])
        
        if form.cleaned_data.get('max_amount'):
            transactions = transactions.filter(amount__lte=form.cleaned_data['max_amount'])
        
        # Handle CSV export
        if 'export' in request.GET:
            return export_transactions_csv(request, transactions)
    
    # Pagination
    paginator = Paginator(transactions, 20)
    page_number = request.GET.get('page')
    page_obj = paginator.get_page(page_number)
    
    context = {
        'form': form,
        'page_obj': page_obj,
        'transactions': page_obj,
    }
    return render(request, 'account/transactions.html', context)


@login_required
def transaction_detail_view(request, transaction_id):
    """View detailed transaction information"""
    transaction_obj = get_object_or_404(
        Transaction.objects.filter(
            models.Q(sender=request.user) | models.Q(receiver=request.user)
        ),
        transaction_id=transaction_id
    )
    
    context = {
        'transaction': transaction_obj,
    }
    return render(request, 'account/transaction_detail.html', context)


@login_required
def payment_request_detail_view(request, request_id):
    """View detailed payment request information"""
    payment_request = get_object_or_404(
        PaymentRequest.objects.filter(
            models.Q(requester=request.user) | models.Q(recipient=request.user)
        ),
        request_id=request_id
    )
    
    # Check expiration
    payment_request.check_expiration()
    
    context = {
        'payment_request': payment_request,
    }
    return render(request, 'account/payment_request_detail.html', context)


# Notification Views
@login_required
def notifications_view(request):
    """View all notifications"""
    notifications = Notification.objects.filter(user=request.user).order_by('-created_at')
    
    # Mark notifications as read
    unread_notifications = notifications.filter(status='unread')
    unread_notifications.update(status='read', read_at=timezone.now())
    
    # Pagination
    paginator = Paginator(notifications, 20)
    page_number = request.GET.get('page')
    page_obj = paginator.get_page(page_number)
    
    context = {
        'notifications': page_obj,
    }
    return render(request, 'account/notifications.html', context)


@login_required
def mark_notification_read_view(request, notification_id):
    """Mark a notification as read"""
    notification = get_object_or_404(Notification, id=notification_id, user=request.user)
    notification.status = 'read'
    notification.read_at = timezone.now()
    notification.save()
    
    if request.headers.get('X-Requested-With') == 'XMLHttpRequest':
        return JsonResponse({'status': 'success'})
    
    return redirect('account:notifications')


@login_required
def notification_count_view(request):
    """Get notification count for real-time updates"""
    if request.headers.get('X-Requested-With') == 'XMLHttpRequest':
        unread_count = Notification.objects.filter(
            user=request.user, 
            status='unread'
        ).count()
        
        # Get latest notification for toast
        latest_notification = None
        if unread_count > 0:
            latest = Notification.objects.filter(
                user=request.user,
                status='unread'
            ).order_by('-created_at').first()
            
            if latest:
                latest_notification = {
                    'title': latest.title,
                    'message': latest.message,
                    'type': latest.notification_type
                }
        
        return JsonResponse({
            'count': unread_count,
            'latest_notification': latest_notification
        })
    
    return JsonResponse({'error': 'Invalid request'}, status=400)


# Credit Card Views
@login_required
def credit_cards_view(request):
    """View and manage credit cards"""
    cards = CreditCard.objects.filter(user=request.user).order_by('-created_at')
    
    context = {
        'cards': cards,
    }
    return render(request, 'account/credit_cards.html', context)


@login_required
def add_credit_card_view(request):
    """Add a new credit card"""
    if request.method == 'POST':
        form = CreditCardForm(request.POST)
        if form.is_valid():
            card = form.save(commit=False)
            card.user = request.user
            card.save()
            messages.success(request, f'{card.card_brand} card ending in {card.last_four_digits} added successfully!')
            return redirect('account:card_detail', card_id=card.id)
    else:
        form = CreditCardForm()

    return render(request, 'account/add_credit_card.html', {'form': form})


@login_required
def edit_credit_card_view(request, card_id):
    """Edit credit card details"""
    card = get_object_or_404(CreditCard, id=card_id, user=request.user)

    if request.method == 'POST':
        form = CreditCardForm(request.POST, instance=card)
        if form.is_valid():
            card = form.save()
            messages.success(request, f'{card.card_brand} card updated successfully!')
            return redirect('account:card_detail', card_id=card.id)
    else:
        form = CreditCardForm(instance=card)

    return render(request, 'account/edit_credit_card.html', {'form': form, 'card': card})


@login_required
def card_detail_view(request, card_id):
    """Single card: details, limits, usage and its funding/withdrawal history"""
    card = get_object_or_404(CreditCard, id=card_id, user=request.user)
    now = timezone.now()
    day_start = now.replace(hour=0, minute=0, second=0, microsecond=0)
    month_start = day_start.replace(day=1)
    funded_today = card.funded_since(day_start)
    funded_month = card.funded_since(month_start)
    history = (Transaction.objects
               .filter(sender=request.user,
                       reference__in=[card.funding_reference, f'CARD_WITHDRAW_{card.last_four_digits}'])
               .order_by('-created_at')[:10])
    context = {
        'card': card,
        'funded_today': funded_today,
        'funded_month': funded_month,
        'daily_remaining': max(card.daily_limit - funded_today, Decimal('0.00')),
        'monthly_remaining': max(card.monthly_limit - funded_month, Decimal('0.00')),
        'daily_pct': min(int(funded_today / card.daily_limit * 100), 100) if card.daily_limit else 0,
        'monthly_pct': min(int(funded_month / card.monthly_limit * 100), 100) if card.monthly_limit else 0,
        'history': history,
        'is_expired': not card.validate_expiry_date(card.expiry_month, card.expiry_year),
    }
    return render(request, 'account/card_detail.html', context)


@login_required
def delete_credit_card_view(request, card_id):
    """Delete a credit card"""
    card = get_object_or_404(CreditCard, id=card_id, user=request.user)
    
    if request.method == 'POST':
        card.delete()
        messages.success(request, f'{card.card_brand} card deleted successfully!')
        return redirect('account:credit_cards')
    
    context = {
        'card': card,
    }
    return render(request, 'account/delete_credit_card.html', context)


def _card_move_view(request, direction):
    """Shared flow for card top-ups ('fund') and withdrawals ('withdraw'): details -> review -> confirm."""
    is_fund = direction == 'fund'
    FormClass = CardFundingForm if is_fund else CardWithdrawalForm
    template = 'account/card_funding.html' if is_fund else 'account/card_withdrawal.html'
    url_name = 'account:card_funding' if is_fund else 'account:card_withdrawal'
    account = Account.objects.get(user=request.user)
    stage = _flow_stage(request)
    form = FormClass(request.user, request.POST or None, initial={'card': request.GET.get('card')})
    cards = list(request.user.credit_cards.filter(status='active'))
    context = {'form': form, 'account': account, 'cards': cards, 'direction': direction, 'url_name': url_name}

    if stage and stage != 'edit' and form.is_valid():
        card = form.cleaned_data['card']
        amount = form.cleaned_data['amount']
        description = form.cleaned_data.get('description', '')
        balance_after = account.account_balance + amount if is_fund else account.account_balance - amount

        if stage != 'confirm':
            context.update({'card': card, 'amount': amount, 'balance_after': balance_after})
            return render(request, 'account/card_move_review.html', context)

        try:
            with transaction.atomic():
                account = Account.objects.select_for_update().get(pk=account.pk)
                card = CreditCard.objects.select_for_update().get(pk=card.pk, user=request.user)
                if is_fund and not card.can_fund(amount):
                    raise ValueError('This card has reached its daily or monthly funding limit.')
                if not is_fund and (not card.can_withdraw(amount) or account.account_balance < amount):
                    raise ValueError('Insufficient balance for this withdrawal.')

                tx = Transaction.objects.create(
                    transaction_type='card_funding' if is_fund else 'withdrawal',
                    amount=amount, currency='USD',
                    sender=request.user, receiver=request.user,
                    sender_account=account, receiver_account=account,
                    status='completed',
                    description=(f'Card funding: {description}' if is_fund else f'Card withdrawal: {description}') if description
                    else ('Card funding' if is_fund else 'Card withdrawal'),
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
                    user=request.user, notification_type='transaction',
                    title='Account Funded' if is_fund else 'Withdrawal Completed',
                    message=(f'Your account has been funded with ${amount:,.2f} from your {card.card_brand} card ending in {card.last_four_digits}.'
                             if is_fund else
                             f'${amount:,.2f} has been withdrawn to your {card.card_brand} card ending in {card.last_four_digits}.'),
                    transaction=tx)
            messages.success(request, f'Account funded with ${amount:,.2f}!' if is_fund else f'Withdrawal of ${amount:,.2f} completed!')
            return redirect('account:transaction_detail', transaction_id=tx.transaction_id)
        except ValueError as e:
            form.add_error('amount', str(e))
        except Exception as e:
            messages.error(request, f"{'Funding' if is_fund else 'Withdrawal'} failed: {e}")

    return render(request, template, context)


@login_required
def card_funding_view(request):
    """Top up the PayWay account from a linked card (spec 33)."""
    return _card_move_view(request, 'fund')


@login_required
def card_withdrawal_view(request):
    """Withdraw from the PayWay account to a linked card (spec 34)."""
    return _card_move_view(request, 'withdraw')


# International Transfer Views
@login_required
def international_transfers_view(request):
    """View for listing international transfers"""
    transfers = InternationalTransfer.objects.filter(sender=request.user).order_by('-created_at')
    
    # Filter by status if provided
    status_filter = request.GET.get('status')
    if status_filter:
        transfers = transfers.filter(status=status_filter)
    
    context = {
        'transfers': transfers,
        'status_choices': InternationalTransfer.TRANSFER_STATUS,
        'current_status': status_filter,
    }
    return render(request, 'account/international_transfers.html', context)


@login_required
def create_international_transfer_view(request):
    """International transfer: recipient -> amount -> review -> confirm."""
    account = Account.objects.get(user=request.user)
    stage = _flow_stage(request)
    form = InternationalTransferForm(request.POST or None)
    context = {'form': form, 'account': account, 'currencies': Currency.objects.filter(is_active=True)}

    if stage and stage != 'edit' and form.is_valid():
        currency = form.cleaned_data['currency']
        amount = form.cleaned_data['amount']
        rate = currency.exchange_rate_to_usd          # units of `currency` per 1 USD (EUR 0.85, JPY 110)
        fee = form.calculate_transfer_fee(amount, currency.code)
        amount_usd = (amount / rate).quantize(Decimal('0.01'))
        fee_usd = (fee / rate).quantize(Decimal('0.01'))
        debit_usd = amount_usd + fee_usd

        if account.account_balance < debit_usd:
            form.add_error('amount', f'Insufficient balance. This transfer needs ${debit_usd:,.2f} including fees.')

        if not form.errors and stage != 'confirm':
            context.update({'bank': form.bank, 'currency': currency, 'amount': amount, 'fee': fee, 'rate': rate,
                            'usd_per_unit': (Decimal('1') / rate),
                            'amount_usd': amount_usd, 'fee_usd': fee_usd, 'debit_usd': debit_usd,
                            'balance_after': account.account_balance - debit_usd,
                            'eta': timezone.now() + timedelta(days=3)})
            return render(request, 'account/international_review.html', context)

        if not form.errors and stage == 'confirm':
            try:
                with transaction.atomic():
                    locked = Account.objects.select_for_update().get(pk=account.pk)
                    if locked.account_balance < debit_usd:
                        raise ValueError(f'Insufficient balance. This transfer needs ${debit_usd:,.2f} including fees.')
                    transfer = form.save(commit=False)
                    transfer.sender = request.user
                    transfer.sender_account = locked
                    transfer.recipient_bank = form.bank
                    transfer.recipient_swift_code = form.bank.swift_code
                    transfer.transfer_fee = fee
                    transfer.exchange_rate = rate
                    transfer.amount_in_usd = amount_usd
                    transfer.total_amount = amount + fee
                    transfer.estimated_delivery = timezone.now() + timedelta(days=3)
                    transfer.save()

                    locked.account_balance -= debit_usd
                    locked.save(update_fields=['account_balance'])
                    Notification.objects.create(
                        user=request.user, notification_type='transaction', title='International Transfer Initiated',
                        message=f'Your transfer of {currency.symbol}{amount:,.2f} to {transfer.recipient_name} has been initiated. '
                                f'Transfer ID: {transfer.transfer_id}')
                messages.success(request, f'International transfer initiated! Transfer ID: {transfer.transfer_id}')
                return redirect('account:international_transfer_detail', transfer_id=transfer.transfer_id)
            except ValueError as e:
                form.add_error('amount', str(e))
            except Exception as e:
                messages.error(request, f'Error creating transfer: {e}')

    return render(request, 'account/create_international_transfer.html', context)


@login_required
def international_transfer_detail_view(request, transfer_id):
    """View for international transfer details"""
    try:
        transfer = InternationalTransfer.objects.get(
            transfer_id=transfer_id, 
            sender=request.user
        )
    except InternationalTransfer.DoesNotExist:
        messages.error(request, 'Transfer not found.')
        return redirect('account:international_transfers')
    
    context = {
        'transfer': transfer,
    }
    return render(request, 'account/international_transfer_detail.html', context)


@login_required
def swift_code_search_view(request):
    """View for searching SWIFT codes"""
    form = SwiftCodeSearchForm(request.GET)
    banks = Bank.objects.filter(is_active=True)
    
    if form.is_valid():
        search = form.cleaned_data.get('search', '').strip()
        country = form.cleaned_data.get('country', '').strip()
        
        if search:
            banks = banks.filter(
                models.Q(bank_name__icontains=search) |
                models.Q(swift_code__icontains=search) |
                models.Q(country__icontains=search) |
                models.Q(city__icontains=search)
            )
        
        if country:
            banks = banks.filter(country=country)
    
    # Paginate results
    paginator = Paginator(banks, 20)
    page_number = request.GET.get('page')
    page_obj = paginator.get_page(page_number)
    
    context = {
        'form': form,
        'page_obj': page_obj,
        'total_banks': banks.count(),
    }
    return render(request, 'account/swift_code_search.html', context)


@login_required
def currency_converter_view(request):
    """View for currency conversion"""
    form = CurrencyConverterForm(request.GET)
    conversion_result = None
    
    if form.is_valid():
        try:
            conversion_result = form.convert_currency()
        except Exception as e:
            messages.error(request, f'Error converting currency: {str(e)}')
    
    currencies = list(Currency.objects.filter(is_active=True).order_by('code'))
    # Value of 1 USD in each currency, normalised to a bar height (log scale keeps JPY/HKD readable)
    import math
    if currencies:
        logs = {c.code: math.log10(float(c.exchange_rate_to_usd) * 10 + 1) for c in currencies}
        top = max(logs.values()) or 1
    bars = [{'c': c, 'height': max(18, round(logs[c.code] / top * 100)),
             'usd_value': (Decimal('1') / c.exchange_rate_to_usd)} for c in currencies] if currencies else []

    result = conversion_result
    if result:
        result['inverse_rate'] = Decimal('1') / result['exchange_rate'] if result['exchange_rate'] else None
        sel = {result['from_currency'].code, result['to_currency'].code}
        for b in bars:
            b['selected'] = b['c'].code in sel
    pairs = [('USD', 'EUR'), ('GBP', 'USD'), ('EUR', 'GBP'), ('USD', 'JPY'), ('SGD', 'GBP'), ('AUD', 'USD')]
    codes = {c.code for c in currencies}
    context = {
        'form': form,
        'conversion_result': result,
        'currencies': currencies,
        'bars': bars,
        'pairs': [p for p in pairs if p[0] in codes and p[1] in codes],
        'amount_value': request.GET.get('amount') or '1000',
    }
    return render(request, 'account/currency_converter.html', context)


@login_required
def transfer_fee_calculator_view(request):
    """View for calculating transfer fees"""
    if request.method == 'POST':
        amount = request.POST.get('amount')
        currency_code = request.POST.get('currency')
        
        if amount and currency_code:
            try:
                amount = Decimal(amount)
                currency = Currency.objects.get(code=currency_code)
                
                # Calculate fee using the same logic as the form
                fee_structure = {
                    'USD': {'percentage': 0.02, 'min_fee': 5.00, 'max_fee': 50.00},
                    'EUR': {'percentage': 0.025, 'min_fee': 5.00, 'max_fee': 60.00},
                    'GBP': {'percentage': 0.03, 'min_fee': 5.00, 'max_fee': 75.00},
                    'JPY': {'percentage': 0.025, 'min_fee': 500.00, 'max_fee': 5000.00},
                    'CAD': {'percentage': 0.025, 'min_fee': 6.00, 'max_fee': 60.00},
                    'AUD': {'percentage': 0.025, 'min_fee': 6.00, 'max_fee': 65.00},
                    'CHF': {'percentage': 0.025, 'min_fee': 5.00, 'max_fee': 55.00},
                    'SGD': {'percentage': 0.025, 'min_fee': 6.00, 'max_fee': 65.00},
                    'HKD': {'percentage': 0.025, 'min_fee': 40.00, 'max_fee': 400.00},
                    'NZD': {'percentage': 0.025, 'min_fee': 7.00, 'max_fee': 70.00},
                }
                
                structure = fee_structure.get(currency_code, fee_structure['USD'])
                percentage_fee = amount * Decimal(str(structure['percentage']))
                fee = max(structure['min_fee'], min(percentage_fee, structure['max_fee']))
                total_amount = amount + fee
                
                return JsonResponse({
                    'success': True,
                    'amount': float(amount),
                    'fee': float(fee),
                    'total_amount': float(total_amount),
                    'currency': currency_code,
                    'currency_symbol': currency.symbol,
                })
                
            except (ValueError, Currency.DoesNotExist):
                return JsonResponse({
                    'success': False,
                    'error': 'Invalid amount or currency'
                })
    
    context = {
        'currencies': Currency.objects.filter(is_active=True),
    }
    return render(request, 'account/transfer_fee_calculator.html', context)


# Utility Views
def export_transactions_csv(request, transactions):
    """Export transactions to CSV"""
    response = HttpResponse(content_type='text/csv')
    response['Content-Disposition'] = 'attachment; filename="transactions.csv"'
    
    writer = csv.writer(response)
    writer.writerow([
        'Transaction ID', 'Date', 'Type', 'Amount', 'Currency', 
        'Sender', 'Receiver', 'Status', 'Description', 'Reference'
    ])
    
    for transaction in transactions:
        writer.writerow([
            transaction.transaction_id,
            transaction.created_at.strftime('%Y-%m-%d %H:%M:%S'),
            transaction.get_transaction_type_display(),
            transaction.amount,
            transaction.currency,
            transaction.sender.email,
            transaction.receiver.email,
            transaction.get_status_display(),
            transaction.description,
            transaction.reference,
        ])
    
    return response
