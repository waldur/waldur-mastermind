from __future__ import unicode_literals

from django.utils import timezone

from nodeconductor_assembly_waldur.packages import models as packages_models
from . import utils


def add_openstack_packages_details_to_new_invoice(sender, instance, created=False, **kwargs):
    if not created:
        return

    packages = packages_models.OpenStackPackage.objects.filter(
        tenant__service_project_link__project__customer=instance.customer
    )
    datetime_now = timezone.now()
    datetime_month_end = utils.get_current_month_end_datetime()
    for package in packages:
        # price is calculated on hourly basis
        price = package.template.price * 24 * (datetime_month_end - datetime_now).days
        instance.items.create(package=package, price=price, start=datetime_now, end=datetime_month_end)
