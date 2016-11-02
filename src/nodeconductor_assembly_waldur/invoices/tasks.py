from datetime import date

from celery import shared_task

from nodeconductor_assembly_waldur.packages import models as package_models
from . import models


@shared_task(name='invoices.create_monthly_invoices_for_openstack_packages')
def create_monthly_invoices_for_openstack_packages():
    today = date.today()

    models.Invoice.objects.filter(
        state=models.Invoice.States.PENDING,
        month__lt=today.month,
        year__lte=today.year
    ).update(state=models.Invoice.States.BILLED)

    customer_ids = set(package_models.OpenStackPackage.objects.all().values_list(
        'tenant__service_project_link__project__customer',
        flat=True
    ))
    for customer_id in customer_ids:
        models.Invoice.objects.get_or_create(
            customer_id=customer_id,
            state=models.Invoice.States.PENDING,
            month=today.month,
            year=today.year
        )
