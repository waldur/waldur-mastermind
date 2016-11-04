from django.db import models as django_models
from django.utils import timezone

from nodeconductor_assembly_waldur.packages import models as package_models
from . import utils


class InvoiceQuerySet(django_models.QuerySet):

    def create(self, customer, **kwargs):
        """
        Performs following actions:
            - Create new invoice
            - Connect package details to the invoice and calculates their price
        """
        if 'month' not in kwargs:
            kwargs['month'] = utils.get_current_month()
        if 'year' not in kwargs:
            kwargs['year'] = utils.get_current_year()

        # create invoice
        invoice = super(InvoiceQuerySet, self).create(customer=customer, **kwargs)

        # connect OpenStack packages details
        packages = package_models.OpenStackPackage.objects.filter(
            tenant__service_project_link__project__customer=customer,
        )
        datetime_now = timezone.now()
        datetime_month_end = utils.get_current_month_end_datetime()
        for package in packages:
            invoice.openstack_items.create(package=package, start=datetime_now, end=datetime_month_end)

        return invoice


InvoiceManager = django_models.Manager.from_queryset(InvoiceQuerySet)


class OpenStackItemQuerySet(django_models.QuerySet):

    def create(self, invoice, package, **kwargs):
        """
        Performs following actions:
            - Calculate price till end of current month
            - Create Invoice OpenStack item
        """
        if 'start' not in kwargs:
            kwargs['start'] = timezone.now()
        if 'end' not in kwargs:
            kwargs['end'] = utils.get_current_month_end_datetime()

        # price is calculated on hourly basis
        price = package.template.price * 24 * (kwargs['end'] - kwargs['start']).days
        return super(OpenStackItemQuerySet, self).create(package=package, invoice=invoice, price=price, **kwargs)


OpenStackItemManager = django_models.Manager.from_queryset(OpenStackItemQuerySet)
