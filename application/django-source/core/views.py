import csv
import json
from collections import defaultdict
from datetime import datetime, time, timedelta

from django.db.models import Q, Sum
from django.http import HttpResponse
from django.shortcuts import render
from django.utils import timezone
from django.contrib.auth.decorators import login_required
from django.db import connection
from django.http import JsonResponse
from django.views.decorators.http import require_GET
from account.models import Account, CreditCard, InternationalTransfer, KYC, PaymentRequest, Transaction
from risk.insights import COLOURS as CATEGORY_COLOURS, categorise, forecast_balance, forecast_flag, spending_flags


def _database_check():
    """Return (ok, error_message). Used for readiness / actuator DB component."""
    try:
        connection.ensure_connection()
        return True, None
    except Exception as exc:
        return False, str(exc)


@require_GET
def health_liveness(request):
    """
    Kubernetes liveness probe — process is up; does not touch the database.
    Same idea as Spring Boot Actuator livenessState.
    """
    return JsonResponse({'status': 'UP'})


@require_GET
def health_readiness(request):
    """
    Kubernetes readiness probe — verifies the app can serve traffic (DB reachable).
    Returns 503 when the database is unavailable so the pod is removed from Service endpoints.
    """
    ok, err = _database_check()
    body = {
        'status': 'UP' if ok else 'DOWN',
        'checks': {'database': 'UP' if ok else 'DOWN'},
    }
    if not ok and err:
        body['checks']['database_error'] = err
    return JsonResponse(body, status=200 if ok else 503)


@require_GET
def actuator_health(request):
    """
    Spring Boot Actuator–style aggregated health JSON for tooling and optional probes.
    """
    db_ok, db_err = _database_check()
    components = {
        'db': {
            'status': 'UP' if db_ok else 'DOWN',
            **({} if db_ok else {'details': {'error': db_err}}),
        },
    }
    overall = 'UP' if db_ok else 'DOWN'
    return JsonResponse(
        {'status': overall, 'components': components},
        status=200 if db_ok else 503,
    )


@require_GET
def api_discovery(request):
    """
    Lightweight index of HTTP-facing routes useful for curl/Postman.
    Most business routes are session/HTML; JSON APIs are called out explicitly.
    """
    base = request.build_absolute_uri('/').rstrip('/')
    return JsonResponse({
        'service': 'payway',
        'documentation': f'{base}/admin/',
        'health': {
            'liveness': f'{base}/health/',
            'readiness': f'{base}/ready/',
            'actuator_style': f'{base}/actuator/health/',
        },
        'json_apis': [
            {
                'path': '/auth/api/security/summary/',
                'methods': ['GET'],
                'auth': 'session',
                'description': 'Security summary for the logged-in user',
            },
            {
                'path': '/auth/api/security/events/',
                'methods': ['GET'],
                'auth': 'session',
                'description': 'Recent security events for the logged-in user',
            },
            {
                'path': '/account/notifications/count/',
                'methods': ['GET'],
                'auth': 'session',
                'headers': {'X-Requested-With': 'XMLHttpRequest'},
                'description': 'Unread notification count and latest unread (AJAX)',
            },
            {
                'path': '/account/notification/<id>/read/',
                'methods': ['GET'],
                'auth': 'session',
                'headers': {'X-Requested-With': 'XMLHttpRequest'},
                'description': 'Mark notification read; JSON if AJAX header',
            },
            {
                'path': '/account/fee-calculator/',
                'methods': ['POST'],
                'auth': 'session',
                'body': 'application/x-www-form-urlencoded: amount, currency',
                'description': 'Transfer fee estimate JSON on successful POST',
            },
        ],
        'note': 'Full human-readable guide: api-doc.md in the project root (not served as a URL). Log in via /auth/login/ (HTML form with CSRF) for a session cookie.',
    })


def home_view(request):
    """Home page view"""
    return render(request, 'core/home.html')


DASHBOARD_RANGES = {'7': 7, '30': 30, '90': 90}


def _pct_change(current, previous):
    current, previous = float(current or 0), float(previous or 0)
    if previous == 0:
        return None if current == 0 else 100.0
    return round((current - previous) / abs(previous) * 100, 1)


@login_required
def dashboard_view(request):
    """Overview dashboard: KPIs, charts and recent activity for the selected period."""
    user = request.user
    account = Account.objects.filter(user=user).first()
    kyc = KYC.objects.filter(user=user).first()

    days = DASHBOARD_RANGES.get(request.GET.get('range'), 30)
    now = timezone.now()
    today = timezone.localdate()
    start_date = today - timedelta(days=days - 1)
    prev_start_date = start_date - timedelta(days=days)
    start = timezone.make_aware(datetime.combine(prev_start_date, time.min))

    inflow_q = Q(receiver=user) & (~Q(sender=user) | Q(transaction_type__in=['card_funding', 'deposit']))
    outflow_q = Q(sender=user) & (~Q(receiver=user) | Q(transaction_type='withdrawal'))
    txs = (Transaction.objects
           .filter(Q(sender=user) | Q(receiver=user), status='completed', created_at__gte=start)
           .select_related('sender', 'receiver'))
    intl = InternationalTransfer.objects.filter(
        sender=user, status__in=['completed', 'processing'], created_at__gte=start)

    if request.GET.get('export') == 'csv':
        return _export_transactions_csv(user, txs.filter(created_at__date__gte=start_date), days)

    # ---- daily series over current + previous period ----
    n = days * 2
    dates = [prev_start_date + timedelta(days=i) for i in range(n)]
    idx = {d: i for i, d in enumerate(dates)}
    inflow = [0.0] * n
    outflow = [0.0] * n
    count = [0] * n
    intl_daily = [0.0] * n
    out_by_cat = defaultdict(float)        # current period, by spending category
    out_by_cat_prev = defaultdict(float)   # previous period
    current_payments = []                  # (when, amount, counterparty) for "unusually large" flags
    inflow_ids = set(txs.filter(inflow_q).values_list('id', flat=True))
    outflow_ids = set(txs.filter(outflow_q).values_list('id', flat=True))

    for tx in txs:
        i = idx.get(timezone.localtime(tx.created_at).date())
        if i is None:
            continue
        amt = float(tx.amount)
        if tx.id in inflow_ids:
            inflow[i] += amt
        if tx.id in outflow_ids:
            outflow[i] += amt
            cat = categorise(tx.description, tx.transaction_type)
            if i >= days:
                out_by_cat[cat] += amt
                if tx.receiver_id != user.id:
                    current_payments.append((timezone.localtime(tx.created_at), amt,
                                             tx.receiver.get_full_name() or tx.receiver.username))
            else:
                out_by_cat_prev[cat] += amt
        count[i] += 1
    for t in intl:
        i = idx.get(timezone.localtime(t.created_at).date())
        if i is None:
            continue
        outflow[i] += float(t.amount_in_usd)
        intl_daily[i] += float(t.amount_in_usd)
        count[i] += 1
        cat = categorise(t.description, 'international')
        if i >= days:
            out_by_cat[cat] += float(t.amount_in_usd)
            current_payments.append((timezone.localtime(t.created_at), float(t.amount_in_usd), t.recipient_name))
        else:
            out_by_cat_prev[cat] += float(t.amount_in_usd)

    cur = slice(days, n)
    prev = slice(0, days)
    balance_now = float(account.account_balance) if account else 0.0
    # walk backwards from today's balance to rebuild end-of-day balances
    balances = [0.0] * n
    running = balance_now
    for i in range(n - 1, -1, -1):
        balances[i] = round(running, 2)
        running -= inflow[i] - outflow[i]
    balance_start = balances[days - 1]

    labels = [d.strftime('%b %d') for d in dates[cur]]
    sum_in, sum_in_prev = sum(inflow[cur]), sum(inflow[prev])
    sum_out, sum_out_prev = sum(outflow[cur]), sum(outflow[prev])
    cnt, cnt_prev = sum(count[cur]), sum(count[prev])

    kpis = [
        {'label': 'Available balance', 'value': balance_now, 'money': True, 'icon': 'fa-wallet', 'tone': 'brand',
         'delta': _pct_change(balance_now, balance_start), 'spark': balances[cur]},
        {'label': 'Money in', 'value': sum_in, 'money': True, 'icon': 'fa-arrow-down', 'tone': 'blue',
         'delta': _pct_change(sum_in, sum_in_prev), 'spark': inflow[cur]},
        {'label': 'Money out', 'value': sum_out, 'money': True, 'icon': 'fa-arrow-up', 'tone': 'amber',
         'delta': _pct_change(sum_out, sum_out_prev), 'spark': outflow[cur], 'invert': True},
        {'label': 'Transactions', 'value': cnt, 'money': False, 'icon': 'fa-receipt', 'tone': 'violet',
         'delta': _pct_change(cnt, cnt_prev), 'spark': count[cur]},
    ]
    for k in kpis:
        k['spark_json'] = json.dumps([round(v, 2) for v in k['spark']])
        k['delta_abs'] = abs(k['delta']) if k['delta'] is not None else None

    # ---- combo chart buckets (6-7 bars regardless of range) ----
    bucket = {7: 1, 30: 5, 90: 15}[days]
    combo = {'labels': [], 'in': [], 'out': [], 'count': []}
    cur_dates = dates[cur]
    for b in range(0, days, bucket):
        sl = slice(days + b, days + min(b + bucket, days))
        first = cur_dates[b]
        combo['labels'].append(first.strftime('%b %d') if bucket > 1 else first.strftime('%a'))
        combo['in'].append(round(sum(inflow[sl]), 2))
        combo['out'].append(round(sum(outflow[sl]), 2))
        combo['count'].append(sum(count[sl]))

    # ---- payment requests pipeline (funnel) ----
    period_start_dt = timezone.make_aware(datetime.combine(start_date, time.min))
    sent_reqs = PaymentRequest.objects.filter(requester=user, created_at__gte=period_start_dt)
    req_total = sent_reqs.count()
    req_paid = sent_reqs.filter(status='paid').count()
    req_pending = sent_reqs.filter(status='pending', is_expired=False).count()
    req_closed = req_total - req_paid - req_pending
    funnel = [
        {'label': 'Requests sent', 'value': req_total, 'color': '#047857'},
        {'label': 'Awaiting payment', 'value': req_pending, 'color': '#10b981'},
        {'label': 'Paid', 'value': req_paid, 'color': '#3b82f6'},
        {'label': 'Expired / cancelled', 'value': req_closed, 'color': '#f59e0b'},
    ]
    for f in funnel:
        f['pct'] = round(f['value'] / req_total * 100, 1) if req_total else 0
        f['width'] = max(f['pct'], 18) if req_total else 18
    to_pay = PaymentRequest.objects.filter(recipient=user, status='pending', is_expired=False)

    # ---- spending mix (donut), by category from payment descriptions ----
    palette = ['#047857', '#3b82f6', '#f59e0b', '#8b5cf6', '#ef4444', '#14b8a6']
    mix_total = sum(out_by_cat.values())
    mix = [{'label': k, 'value': round(v, 2), 'color': CATEGORY_COLOURS.get(k, palette[i % len(palette)]),
            'pct': round(v / mix_total * 100, 1) if mix_total else 0}
           for i, (k, v) in enumerate(sorted(out_by_cat.items(), key=lambda kv: -kv[1]))]

    # ---- insights: balance forecast and unusual spending ----
    horizon = 14
    forecast = forecast_balance(balances, horizon=horizon)
    period_start = timezone.make_aware(datetime.combine(start_date, time.min))
    usual = [float(a) for a in Transaction.objects.filter(
        sender=user, status='completed', transaction_type__in=['transfer', 'payment_request', 'withdrawal'],
        created_at__gte=period_start - timedelta(days=180), created_at__lt=period_start,
    ).exclude(transaction_type='transfer', receiver=user).values_list('amount', flat=True)[:500]]
    insights = [f for f in [forecast_flag(forecast, horizon)] if f]
    insights += spending_flags(out_by_cat, out_by_cat_prev, days, usual, current_payments)
    forecast_data = None
    if forecast:
        forecast_data = {
            'labels': [(today + timedelta(days=d)).strftime('%b %d') for d in range(1, horizon + 1)],
            'values': forecast['values'], 'low': forecast['low'], 'high': forecast['high'],
        }

    # ---- recent activity table ----
    recent = []
    for tx in (Transaction.objects.filter(Q(sender=user) | Q(receiver=user))
               .select_related('sender', 'receiver').order_by('-created_at')[:6]):
        incoming = tx.id in inflow_ids or (tx.receiver_id == user.id and tx.sender_id != user.id) \
            or tx.transaction_type in ('card_funding', 'deposit')
        other = tx.sender if tx.receiver_id == user.id else tx.receiver
        if tx.sender_id == tx.receiver_id:
            name = 'Card top-up' if tx.transaction_type == 'card_funding' else tx.get_transaction_type_display()
        else:
            name = other.get_full_name() or other.username
        recent.append({'tx': tx, 'incoming': incoming, 'name': name})

    cards = CreditCard.objects.filter(user=user, status='active')
    intl_cur = [t for t in intl if timezone.localtime(t.created_at).date() >= start_date]

    context = {
        'account': account,
        'kyc': kyc,
        'kyc_completed': kyc is not None,
        'range_days': days,
        'range_label': f"{start_date.strftime('%b %d')} – {today.strftime('%b %d, %Y')}",
        'kpis': kpis,
        'chart_data': {
            'labels': labels,
            'balance': balances[cur],
            'combo': combo,
            'mix': mix,
            'forecast': forecast_data,
        },
        'insights': insights[:5],
        'forecast': forecast,
        'balance_change': round(balance_now - balance_start, 2),
        'balance_delta': kpis[0]['delta'],
        'balance_delta_abs': kpis[0]['delta_abs'],
        'funnel': funnel,
        'req_total': req_total,
        'to_pay_count': to_pay.count(),
        'to_pay_amount': to_pay.aggregate(s=Sum('amount'))['s'] or 0,
        'mix': mix,
        'mix_total': mix_total,
        'recent': recent,
        'cards': cards,
        'default_card': cards.filter(is_default=True).first() or cards.first(),
        'intl_count': len(intl_cur),
        'intl_total': sum(float(t.amount_in_usd) for t in intl_cur),
        'intl_spark': json.dumps([round(v, 2) for v in intl_daily[cur]]),
    }
    return render(request, 'core/dashboard.html', context)


def _export_transactions_csv(user, txs, days):
    response = HttpResponse(content_type='text/csv')
    response['Content-Disposition'] = f'attachment; filename="payway-transactions-{days}d.csv"'
    writer = csv.writer(response)
    writer.writerow(['Date', 'Transaction ID', 'Type', 'Direction', 'Counterparty', 'Amount', 'Currency', 'Status'])
    for tx in txs.order_by('-created_at'):
        outgoing = tx.sender_id == user.id and (tx.receiver_id != user.id or tx.transaction_type == 'withdrawal')
        other = tx.receiver if tx.sender_id == user.id else tx.sender
        writer.writerow([
            timezone.localtime(tx.created_at).strftime('%Y-%m-%d %H:%M'), tx.transaction_id,
            tx.get_transaction_type_display(), 'out' if outgoing else 'in',
            other.email, f"{'-' if outgoing else ''}{tx.amount}", tx.currency, tx.status,
        ])
    return response
