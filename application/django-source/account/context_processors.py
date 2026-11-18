from .models import Notification


def notification_context(request):
    """Add notification context to all templates"""
    if request.user.is_authenticated:
        # Get unread notification count
        unread_count = Notification.objects.filter(
            user=request.user, 
            status='unread'
        ).count()
        
        # Get recent notifications (last 5)
        recent_notifications = Notification.objects.filter(
            user=request.user
        ).order_by('-created_at')[:5]
        
        return {
            'unread_notifications_count': unread_count,
            'recent_notifications': recent_notifications,
        }
    
    return {
        'unread_notifications_count': 0,
        'recent_notifications': [],
    }


# Signed-in pages render inside the sidebar app shell; marketing/auth pages keep the top nav.
_SHELL_EXCLUDED = {'login', 'register', 'logout', 'two_factor_verify', 'home'}

_NAV_SECTIONS = {
    'dashboard': 'overview',
    'transactions': 'transactions', 'transaction_detail': 'transactions',
    'money_transfer': 'send', 'search_account': 'send',
    'payment_requests': 'requests', 'create_payment_request': 'requests',
    'payment_request_detail': 'requests', 'settle_payment_request': 'requests',
    'international_transfers': 'international', 'create_international_transfer': 'international',
    'international_transfer_detail': 'international', 'swift_code_search': 'international',
    'currency_converter': 'international', 'transfer_fee_calculator': 'international',
    'credit_cards': 'cards', 'add_credit_card': 'cards', 'edit_credit_card': 'cards',
    'delete_credit_card': 'cards', 'card_funding': 'cards', 'card_withdrawal': 'cards',
    'kyc': 'kyc', 'kyc_detail': 'kyc',
    'notifications': 'notifications', 'notification_settings': 'notifications',
    'security_dashboard': 'security', 'security_logs': 'security', 'device_management': 'security',
    'two_factor_setup': 'security', 'security_settings': 'security',
    'account_info': 'profile',
}


def app_shell_context(request):
    match = getattr(request, 'resolver_match', None)
    url_name = match.url_name if match else None
    use_shell = (
        request.user.is_authenticated
        and match is not None
        and match.app_name in ('core', 'account', 'userauths')
        and url_name not in _SHELL_EXCLUDED
    )
    kyc_status = None
    if use_shell:
        from .models import KYC
        kyc_status = KYC.objects.filter(user=request.user).values_list('verification_status', flat=True).first()
    return {
        'app_shell': use_shell,
        'nav_section': _NAV_SECTIONS.get(url_name, ''),
        'shell_kyc_status': kyc_status,
    }
