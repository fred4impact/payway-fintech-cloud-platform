"""Spending insights for the dashboard.

* categorise(): sorts a payment into a spending category from its description, using
  keyword rules (there are no labelled examples to train a classifier on yet);
* forecast_balance(): projects the balance forward with a linear trend fitted to recent
  end-of-day balances, with a band from the fit's residual spread;
* spending_flags(): calls out categories that jumped against the previous period and
  single payments far larger than the user's usual.
"""
import re
import statistics

CATEGORIES = [
    # (category, colour, single-word keywords) - first match wins, so specific ones go first
    ('Rent & housing', '#0f766e', 'rent landlord mortgage lease deposit housing apartment flat property'),
    ('Bills & utilities', '#3b82f6', 'bill electric electricity water utility utilities internet broadband phone mobile '
                                     'airtime data subscription netflix spotify insurance tv cable'),
    ('Groceries', '#16a34a', 'grocery groceries supermarket market food shopping walmart tesco costco aldi lidl '
                             'whole foods kroger shoprite'),
    ('Dining', '#f59e0b', 'restaurant dinner lunch breakfast coffee cafe pizza burger takeaway takeout eats '
                          'doordash deliveroo bar drinks brunch'),
    ('Transport', '#8b5cf6', 'uber lyft bolt taxi cab fuel petrol gas station train bus metro transit parking toll car'),
    ('Travel', '#06b6d4', 'flight airline hotel airbnb booking trip travel vacation holiday visa'),
    ('Health', '#ef4444', 'pharmacy doctor hospital clinic dental dentist medical medicine health gym fitness'),
    ('Education', '#6366f1', 'school tuition course books university college fees class training'),
    ('Shopping', '#ec4899', 'amazon clothes shoes fashion electronics shop store purchase order gift'),
    ('Entertainment', '#a855f7', 'cinema movie concert tickets game games gaming party event'),
    ('Savings & investing', '#047857', 'savings save invest investment stocks crypto pension'),
    ('Family & friends', '#14b8a6', 'mum mom dad mother father family sister brother friend wife husband son daughter '
                                    'kids allowance birthday wedding pocket money loan payback repay owe split'),
]
FALLBACK = {
    'withdrawal': ('Card withdrawals', '#64748b'),
    'international': ('International', '#0ea5e9'),
    'payment_request': ('Paid requests', '#84cc16'),
    'transfer': ('Transfers to people', '#94a3b8'),
}
_PATTERNS = [(name, colour, re.compile(r'\b(' + '|'.join(re.escape(k) for k in sorted(set(words.split()), key=len, reverse=True)) + r')\b', re.I))
             for name, colour, words in CATEGORIES]
COLOURS = {name: colour for name, colour, _ in CATEGORIES} | {v[0]: v[1] for v in FALLBACK.values()}


def categorise(description='', kind='transfer'):
    """Category name for an outgoing payment. `kind` is a transaction type or 'international'."""
    text = description or ''
    if kind == 'withdrawal':
        return FALLBACK['withdrawal'][0]
    for name, _, pattern in _PATTERNS:
        if pattern.search(text):
            return name
    return FALLBACK.get(kind, FALLBACK['transfer'])[0]


def forecast_balance(balances, horizon=14, window=60):
    """Project end-of-day balances `horizon` days ahead.

    Returns {'values', 'low', 'high', 'slope_per_day', 'runs_out_in_days'} or None when
    there's too little data or no movement to fit.
    """
    series = [float(b) for b in balances[-window:]]
    if len(series) < 7 or max(series) == min(series):
        return None
    import numpy as np
    x = np.arange(len(series))
    slope, intercept = np.polyfit(x, series, 1)
    residual = float(np.std(np.asarray(series) - (slope * x + intercept)))
    last = series[-1]
    values, low, high = [], [], []
    for d in range(1, horizon + 1):
        v = float(last + slope * d)                       # anchor the trend on today's actual balance
        band = float(residual * (1 + d / horizon))        # uncertainty grows the further out we go
        values.append(round(v, 2))
        low.append(round(v - band, 2))
        high.append(round(v + band, 2))
    runs_out = None
    if slope < 0 and last > 0:
        runs_out = int(round(last / -float(slope)))
    return {'values': values, 'low': low, 'high': high, 'slope_per_day': round(float(slope), 2),
            'runs_out_in_days': runs_out}


def spending_flags(current, previous, period_days, history_amounts=(), current_payments=()):
    """Insights about the current period.

    current / previous: {category: total} for this period and the one before it.
    history_amounts: amounts of the user's past outgoing payments (for "unusually large").
    current_payments: (when, amount, label) for this period's outgoing payments.
    """
    flags = []
    for cat, total in sorted(current.items(), key=lambda kv: -kv[1]):
        before = previous.get(cat, 0)
        if before >= 20 and total >= 1.5 * before and total - before >= 50:
            flags.append({'tone': 'warn', 'icon': 'fa-arrow-trend-up', 'category': cat,
                          'text': f'{cat} spending is up {((total - before) / before):.0%} on the previous {period_days} days '
                                  f'(${total:,.0f} vs ${before:,.0f}).'})
        elif before == 0 and total >= 200 and sum(previous.values()) > 0:
            flags.append({'tone': 'info', 'icon': 'fa-circle-plus', 'category': cat,
                          'text': f'New this period: ${total:,.0f} on {cat.lower()}.'})
    for cat, before in previous.items():
        total = current.get(cat, 0)
        if before >= 100 and total <= 0.5 * before:
            flags.append({'tone': 'good', 'icon': 'fa-arrow-trend-down', 'category': cat,
                          'text': f'{cat} spending is down {((before - total) / before):.0%} on the previous {period_days} days.'})

    amounts = list(history_amounts)
    if len(amounts) >= 8:
        median = statistics.median(amounts)
        mad = statistics.median(abs(a - median) for a in amounts) or median * 0.25 or 1
        limit = median + 6 * mad
        for when, amount, label in sorted(current_payments, key=lambda p: -p[1])[:3]:
            if amount > limit and amount >= 2 * median:
                flags.append({'tone': 'warn', 'icon': 'fa-triangle-exclamation', 'category': None,
                              'text': f'Unusually large payment: ${amount:,.2f} to {label} on {when:%b %d} '
                                      f'(your typical payment is about ${median:,.0f}).'})
    order = {'warn': 0, 'info': 1, 'good': 2}
    return sorted(flags, key=lambda f: order[f['tone']])[:5]


def forecast_flag(forecast, horizon=14):
    if not forecast:
        return None
    days = forecast['runs_out_in_days']
    if days is not None and days <= 45:
        return {'tone': 'warn', 'icon': 'fa-hourglass-half', 'category': None,
                'text': f'At the current rate your balance would run out in about {days} days '
                        f'(about ${-forecast["slope_per_day"]:,.0f} a day going out).'}
    change = forecast['values'][-1] - (forecast['values'][0] - forecast['slope_per_day'])
    if abs(change) >= 1:
        direction = 'up' if change > 0 else 'down'
        return {'tone': 'info' if change < 0 else 'good', 'icon': 'fa-chart-line', 'category': None,
                'text': f'On current trends your balance will be {direction} about ${abs(change):,.0f} '
                        f'over the next {horizon} days.'}
    return None

