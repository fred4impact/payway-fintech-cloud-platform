"""Template tags for the dashboard-style pages."""
import math

from django import template
from django.utils.html import format_html, mark_safe

register = template.Library()


def _arc(cx, cy, r, a1, a2):
    """SVG arc from angle a1 to a2 (degrees, 180 = left, 0 = right) along the top half."""
    x1, y1 = cx + r * math.cos(math.radians(a1)), cy - r * math.sin(math.radians(a1))
    x2, y2 = cx + r * math.cos(math.radians(a2)), cy - r * math.sin(math.radians(a2))
    return f'M {x1:.2f} {y1:.2f} A {r} {r} 0 0 1 {x2:.2f} {y2:.2f}'


@register.simple_tag
def pw_semi_gauge(pct, uid='g', segments=5):
    """Segmented half-circle gauge filled to `pct` (0-100)."""
    pct = max(0.0, min(100.0, float(pct or 0)))
    cx, cy, r, gap = 130, 124, 96, 3.0
    span = (180 - gap * (segments - 1)) / segments
    filled_to = 180 - 180 * pct / 100          # angle where the fill ends
    parts = []
    for i in range(segments):
        a1 = 180 - i * (span + gap)
        a2 = a1 - span
        parts.append(f'<path d="{_arc(cx, cy, r, a1, a2)}" class="pwx-gauge-track"/>')
        end = max(a2, filled_to)
        if end < a1:
            parts.append(f'<path d="{_arc(cx, cy, r, a1, end)}" class="pwx-gauge-fill" '
                         f'stroke="url(#pwx-stripe-{uid})"/>')
    svg = (
        f'<svg viewBox="0 0 260 136" class="pwx-gauge-svg" role="img" aria-label="{pct:.0f} percent">'
        f'<defs><pattern id="pwx-stripe-{uid}" width="10" height="10" patternUnits="userSpaceOnUse" '
        f'patternTransform="rotate(45)"><rect width="10" height="10" fill="#a6d16b"/>'
        f'<rect width="5" height="10" fill="#bfe08d"/></pattern></defs>'
        + ''.join(parts) + '</svg>'
    )
    return mark_safe(svg)  # nosec B703 B308 - built only from numbers computed above


@register.inclusion_tag('partials/dash/_trend.html')
def pw_trend(t, suffix='from last period'):
    return {'t': t, 'suffix': suffix}
