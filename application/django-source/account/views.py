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
from risk.payments import gate_payment, link_result
from risk.kyc import enqueue as enqueue_kyc_checks
from .payments import (TRANSFER_FEE_RATE, transfer_fee, international_quote, execute_transfer,
                       execute_settlement, execute_card_move, execute_international)


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
                transaction.on_commit(lambda: enqueue_kyc_checks(kyc.pk))
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

    # ---- money in / out, last 7 days (chart) and 30 days vs the 30 before (stats)
    from collections import Counter
    from core.dash import last_days, segments, split_bars, trend
    from risk.insights import COLOURS, categorise
    now = timezone.now()
    in_q = models.Q(receiver=user) & (~models.Q(sender=user) | models.Q(transaction_type__in=['card_funding', 'deposit']))
    out_q = models.Q(sender=user) & (~models.Q(receiver=user) | models.Q(transaction_type='withdrawal'))
    days = last_days(7)
    week_txs = completed.filter(created_at__date__gte=days[0])
    money_in, money_out = Counter(), Counter()
    for tx in week_txs.filter(in_q):
        money_in[timezone.localtime(tx.created_at).date()] += float(tx.amount)
    for tx in week_txs.filter(out_q):
        money_out[timezone.localtime(tx.created_at).date()] += float(tx.amount)
    chart = split_bars(days, money_in, money_out, 'Money in', 'Money out', fmt='${:,.0f}')

    month, prev_month = now - timedelta(days=30), now - timedelta(days=60)
    in_30, in_prev = sum_of(completed.filter(in_q, created_at__gte=month)), sum_of(completed.filter(in_q, created_at__gte=prev_month, created_at__lt=month))
    out_30, out_prev = sum_of(completed.filter(out_q, created_at__gte=month)), sum_of(completed.filter(out_q, created_at__gte=prev_month, created_at__lt=month))
    net_30, net_prev = in_30 - out_30, in_prev - out_prev
    saved_pct = max(0, min(100, round(float(net_30) / float(in_30) * 100))) if in_30 else 0

    # ---- spending by category, last 30 days
    by_cat = Counter()
    for tx in completed.filter(out_q, created_at__gte=month):
        by_cat[categorise(tx.description, tx.transaction_type)] += float(tx.amount)
    spend, spend_total = segments(by_cat.most_common(6), ['#f3b47c', '#f7c99f', '#f1ec7c', '#c6e48b', '#a6d16b', '#7cb342'])

    # ---- people you pay most (quick payment)
    people = []
    for row in (sent.values('receiver_account__account_number', 'receiver__first_name', 'receiver__username')
                .annotate(n=models.Count('id'), last=models.Max('created_at')).order_by('-n', '-last')[:6]):
        people.append({'name': row['receiver__first_name'] or row['receiver__username'],
                       'account': row['receiver_account__account_number']})

    # ---- card funding limits used today (goal-tracker style)
    day_start = timezone.localtime(now).replace(hour=0, minute=0, second=0, microsecond=0)
    card_limits = []
    for card in user.credit_cards.filter(status='active')[:4]:
        used = card.funded_since(day_start)
        pct = round(float(used) / float(card.daily_limit) * 100) if card.daily_limit else 0
        card_limits.append({'card': card, 'used': used, 'pct': min(pct, 100),
                            'left': max(card.daily_limit - used, Decimal('0.00'))})

    missing = [c['label'] for c in checks if not c['done']]
    stats = [
        {'label': 'Money in', 'value': in_30, 'trend': trend(in_30, in_prev, 'up')},
        {'label': 'Money out', 'value': out_30, 'trend': trend(out_30, out_prev, 'down')},
        {'label': 'Net saved', 'value': net_30, 'trend': trend(net_30, net_prev, 'up') if net_prev > 0 else None},
    ]
    for st in stats:
        st['abs'] = abs(st['value'])
    if not (kyc and kyc.verification_status == 'approved'):
        tip = {'title': 'Verify your identity', 'cta': 'Start verification', 'url': 'account:kyc',
               'text': 'Verification takes about three minutes and lifts your sending limits.'}
    elif not (twofa and twofa.is_enabled):
        tip = {'title': 'Protect payments with 2FA', 'cta': 'Turn on 2FA', 'url': 'userauths:two_factor_setup',
               'text': 'Unusual payments are confirmed with a code from your phone instead of your password.'}
    elif not card_limits:
        tip = {'title': 'Link a card for instant top-ups', 'cta': 'Add a card', 'url': 'account:add_credit_card',
               'text': 'Move money in from your bank card in seconds, within limits you control.'}
    else:
        tip = {'title': 'See where your money goes', 'cta': 'Open overview', 'url': 'core:dashboard',
               'text': 'Your overview groups spending by category and forecasts your balance for the next two weeks.'}

    context = {
        'account': account,
        'kyc': kyc,
        'chart': chart,
        'stats': stats,
        'saved_pct': saved_pct, 'spend': spend, 'spend_total': spend_total,
        'people': people, 'card_limits': card_limits, 'missing': missing, 'tip': tip,
        'checks_done': done,
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
        fee = transfer_fee(amount)
        total = amount + fee

        if receiver_account == sender_account:
            form.add_error('receiver_account', 'You cannot transfer money to your own account.')
        elif sender_account.account_balance < total:
            form.add_error('amount', f'Insufficient balance. You need ${total:,.2f} including the ${fee:,.2f} fee.')

        if not form.errors:
            context.update({'receiver_account': receiver_account, 'amount': amount, 'fee': fee, 'total': total,
                            'balance_after': sender_account.account_balance - total})
            if stage != 'confirm':
                return render(request, 'account/transfer_review.html', context)

            description, reference = form.cleaned_data['description'], form.cleaned_data['reference']
            gate = gate_payment(request, 'transfer', total, counterparty=f'acct:{receiver_account.pk}', payload={
                'receiver_account_id': receiver_account.pk, 'amount': str(amount),
                'description': description, 'reference': reference})
            if gate.action == 'hold':
                return redirect('risk:payment_held', decision_id=gate.decision.pk)
            if gate.action == 'challenge':
                context['risk'] = gate
                return render(request, 'account/transfer_review.html', context)
            try:
                tx = execute_transfer(request.user, receiver_account.pk, amount, description, reference)
                link_result(gate, tx)
                messages.success(request, f'Successfully sent ${amount:,.2f} to '
                                          f'{receiver_account.user.get_full_name() or receiver_account.user.email}')
                return redirect('account:transaction_detail', transaction_id=tx.transaction_id)
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
    
    risk = None
    if request.method == 'POST':
        risk = gate_payment(request, 'settlement', payment_request.amount,
                            counterparty=f'acct:{payment_request.requester_account_id}',
                            payload={'payment_request_id': payment_request.pk})
        if risk.action == 'hold':
            return redirect('risk:payment_held', decision_id=risk.decision.pk)
        if risk.allowed:
            try:
                tx = execute_settlement(request.user, payment_request.pk)
                link_result(risk, tx)
                messages.success(request, f'Successfully paid ${payment_request.amount:,.2f} to '
                                          f'{payment_request.requester.get_full_name() or payment_request.requester.email}')
                return redirect('account:transaction_detail', transaction_id=tx.transaction_id)
            except ValueError as e:
                messages.error(request, str(e))
                return redirect('account:payment_requests')
            except Exception as e:
                messages.error(request, f'Settlement failed: {e}')
                return redirect('account:payment_requests')

    account = Account.objects.get(user=request.user)
    context = {
        'payment_request': payment_request,
        'account': account,
        'balance_after': account.account_balance - payment_request.amount,
        'can_pay': account.account_balance >= payment_request.amount,
        'risk': risk,
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

        context.update({'card': card, 'amount': amount, 'balance_after': balance_after})
        if stage != 'confirm':
            return render(request, 'account/card_move_review.html', context)

        flow = 'card_fund' if is_fund else 'card_withdraw'
        gate = gate_payment(request, flow, amount, card=card, payload={
            'card_id': card.pk, 'amount': str(amount), 'description': description})
        if gate.action == 'hold':
            return redirect('risk:payment_held', decision_id=gate.decision.pk)
        if gate.action == 'challenge':
            context['risk'] = gate
            return render(request, 'account/card_move_review.html', context)
        try:
            tx = execute_card_move(request.user, card.pk, amount, description, direction)
            link_result(gate, tx)
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
        fee, amount_usd, fee_usd, debit_usd = international_quote(amount, currency)

        if account.account_balance < debit_usd:
            form.add_error('amount', f'Insufficient balance. This transfer needs ${debit_usd:,.2f} including fees.')

        if not form.errors:
            context.update({'bank': form.bank, 'currency': currency, 'amount': amount, 'fee': fee, 'rate': rate,
                            'usd_per_unit': (Decimal('1') / rate),
                            'amount_usd': amount_usd, 'fee_usd': fee_usd, 'debit_usd': debit_usd,
                            'balance_after': account.account_balance - debit_usd,
                            'eta': timezone.now() + timedelta(days=3)})
            if stage != 'confirm':
                return render(request, 'account/international_review.html', context)

            cd = form.cleaned_data
            payload = {'currency': currency.code, 'amount': str(amount), 'swift_code': form.bank.swift_code,
                       **{k: cd.get(k) or '' for k in ('recipient_name', 'recipient_account_number',
                                                       'recipient_country', 'recipient_city', 'description')}}
            gate = gate_payment(request, 'international', debit_usd, payload=payload,
                                counterparty=f"iban:{form.bank.swift_code}:{payload['recipient_account_number']}")
            if gate.action == 'hold':
                return redirect('risk:payment_held', decision_id=gate.decision.pk)
            if gate.action == 'challenge':
                context['risk'] = gate
                return render(request, 'account/international_review.html', context)
            try:
                transfer = execute_international(request.user, payload)
                link_result(gate, transfer)
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
