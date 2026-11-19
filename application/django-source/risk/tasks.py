from celery import shared_task


@shared_task(name='risk.run_kyc_checks', ignore_result=True, soft_time_limit=120)
def run_kyc_checks(kyc_id):
    from account.models import KYC
    from .kyc import run_checks
    kyc = KYC.objects.filter(pk=kyc_id).first()
    if kyc is not None:
        run_checks(kyc)
