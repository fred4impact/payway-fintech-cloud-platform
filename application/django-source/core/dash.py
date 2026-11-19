"""Small data helpers for the dashboard-style pages (security, settings)."""
from datetime import timedelta

from django.utils import timezone


def change(current, previous):
    """Percent change, or None when there's nothing to compare against."""
    current, previous = float(current or 0), float(previous or 0)
    if previous == 0:
        return None
    return round((current - previous) / previous * 100, 1)


def trend(current, previous, good_when='up'):
    """{'pct', 'dir', 'good'} for a "+5.1% from last period" line."""
    pct = change(current, previous)
    if pct is None:
        return None
    direction = 'up' if pct > 0 else 'down' if pct < 0 else 'flat'
    good = direction == 'flat' or (direction == good_when)
    return {'pct': abs(pct), 'dir': direction, 'good': good}


def last_days(n=7):
    today = timezone.localdate()
    return [today - timedelta(days=n - 1 - i) for i in range(n)]


def split_bars(days, up, down, up_label, down_label, fmt='{:,.0f}'):
    """Bars above and below a zero line (e.g. money in / money out, sign-ins / failures).

    `up` and `down` map day -> value. The plot area is split between the two sides in
    proportion to their peaks, so one scale is used for both.
    """
    up_vals = [float(up.get(d, 0)) for d in days]
    down_vals = [float(down.get(d, 0)) for d in days]
    peak_up, peak_down = max(up_vals + [0]), max(down_vals + [0])
    total = peak_up + peak_down or 1
    top = 0.5 if not (peak_up or peak_down) else min(0.85, max(0.55, peak_up / total))
    today = timezone.localdate()
    bars = []
    for d, u, v in zip(days, up_vals, down_vals):
        bars.append({
            'label': d.strftime('%a'), 'date': d, 'today': d == today,
            'up': u, 'down': v,
            'up_h': round(u / peak_up * 100, 1) if peak_up else 0,
            'down_h': round(v / peak_down * 100, 1) if peak_down else 0,
            'up_txt': fmt.format(u), 'down_txt': fmt.format(v),
        })
    peak = max(range(len(bars)), key=lambda i: bars[i]['up'] + bars[i]['down']) if bars else None
    return {
        'bars': bars, 'top_pct': round(top * 100), 'bottom_pct': round((1 - top) * 100),
        'up_label': up_label, 'down_label': down_label,
        'peak_up': fmt.format(peak_up) if peak_up else '', 'peak_down': fmt.format(peak_down) if peak_down else '',
        'focus': peak,
    }


def segments(items, palette):
    """Shares for a segmented bar + legend. items: [(label, value)]."""
    total = sum(v for _, v in items) or 0
    out = []
    for i, (label, value) in enumerate(items):
        out.append({'label': label, 'value': value, 'color': palette[i % len(palette)],
                    'pct': round(value / total * 100) if total else 0})
    return out, total
