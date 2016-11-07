import datetime

from django.db import models as django_models

from nodeconductor.core import utils as core_utils
from nodeconductor_assembly_waldur.packages import models as package_models


class InvoiceQuerySet(django_models.QuerySet):

    def get_or_create_with_items(self, customer, month, year):
        """
        Performs following actions:
            - Create new invoice or return existing one
            - Connect packages details to created invoice and calculate their price
        """
        # Avoid circular import
        from models import Invoice, OpenStackItem
        try:
            return Invoice.objects.get(customer=customer, month=month, year=year,
                                       state=Invoice.States.PENDING), False
        except Invoice.DoesNotExist:
            pass

        # create invoice
        invoice = self.create(customer=customer, month=month, year=year)

        # connect OpenStack packages details
        date = datetime.date(day=1, year=year, month=month)
        start_datetime = core_utils.month_start(date)
        end_datetime = core_utils.month_end(date)

        packages = package_models.OpenStackPackage.objects.filter(
            tenant__service_project_link__project__customer=customer,
        )
        for package in packages:
            OpenStackItem.objects.create_with_price(invoice, package, start_datetime, end_datetime)

        return invoice, True


InvoiceManager = django_models.Manager.from_queryset(InvoiceQuerySet)


class OpenStackItemQuerySet(django_models.QuerySet):

    def create_with_price(self, invoice, package, start_datetime, end_datetime):
        """
        Performs following actions:
            - Calculate price from "start_datetime" till "end_datetime"
            - Create Invoice OpenStack item
        """
        # Avoid circular import
        from models import OpenStackItem
        price = OpenStackItem.calculate_price_for_period(package.template.price, start_datetime, end_datetime)
        return self.create(package=package, invoice=invoice, price=price,
                           start=start_datetime, end=end_datetime)


OpenStackItemManager = django_models.Manager.from_queryset(OpenStackItemQuerySet)
