from django.contrib.auth.decorators import login_required
from django.shortcuts import get_object_or_404, render

from .models import RiskDecision
from .payments import FLOW_LABELS


@login_required
def payment_held_view(request, decision_id):
    """What the customer sees after a payment is held. Deliberately doesn't list the
    scoring reasons: those are for staff, and would show a fraudster what to avoid."""
    decision = get_object_or_404(RiskDecision, pk=decision_id, user=request.user, kind='payment')
    return render(request, 'risk/payment_held.html', {
        'decision': decision,
        'flow_label': FLOW_LABELS.get(decision.subject, 'Payment'),
    })
