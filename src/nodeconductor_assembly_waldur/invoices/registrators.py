from django.db import transaction
from django.db.models import Q
from django.utils import timezone

from nodeconductor.core import utils as core_utils
from nodeconductor.structure import models as structure_models
from nodeconductor_assembly_waldur.packages import models as packages_models
from nodeconductor_assembly_waldur.support import models as support_models

from . import models, utils


class BaseRegistrator(object):

    def update_invoices(self):
        """
        - For every customer change state of the invoices for previous months from "pending" to "billed"
          and freeze their items.
        - Create new invoice for every customer in current month if not created yet.
        """
        date = timezone.now()

        old_invoices = models.Invoice.objects.filter(
            Q(state=models.Invoice.States.PENDING, year__lt=date.year) |
            Q(state=models.Invoice.States.PENDING, year=date.year, month__lt=date.month)
        )
        for invoice in old_invoices:
            # TODO [TM:1/31/17] Refactor.
            invoice.set_created()

        for customer in structure_models.Customer.objects.iterator():
            items = self._get_chargeable_items(customer)
            if items:
                invoice, created = models.Invoice.objects.get_or_create(
                    customer=customer,
                    month=date.month,
                    year=date.year,
                )
                self._register_items(items, invoice=invoice, start=core_utils.month_start(date))

    def register(self, item, start=None):
        """
        Registers new item into existing invoice.
        In the beginning of the month new invoice is created and all related items are registered.
        :param item: item to register
        :param start: invoice item start date.
        """

        if start is None:
            start = timezone.now()

        customer = self._get_customer(item)
        invoice, created = models.Invoice.objects.get_or_create(
            customer=customer,
            month=start.month,
            year=start.year,
        )

        if created:
            items = self._get_chargeable_items(customer)
        else:
            items = [item]

        self._register_items(items, invoice=invoice, start=start)

    def _get_customer(self, item):
        """
        Returns customer based on provided item.
        :param item: item to get customer from.
        """
        raise NotImplementedError()

    def _register_items(self, items, invoice, start=None):
        end = core_utils.month_end(start)

        with transaction.atomic():
            for item in items:
                self._register_item(invoice=invoice, item=item, start=start, end=end)

    def _get_chargeable_items(self, customer):
        """
        Returns a list of items to charge customer for.
        :param customer: customer to look for chargeable items, for instance open stack packages or offerings.
        """
        raise NotImplementedError()

    def _register_item(self, invoice, item, start, end):
        """
        Registers single chargeable item in the invoice.
        :param invoice: invoice to register item in
        :param item: item to register in the invoice
        :param start: date when item usage has started
        :param end: date when item usage has ended
        :return: registered invoice item
        """
        raise NotImplementedError()

    def terminate(self, item, now=None):
        """
        Freezes invoice item's usage.
        :param item: chargeable item to use for search of invoice item.
        :param now: date of invoice with invoice items.
        """
        if not now:
            now = timezone.now()

        freezable = self._find_invoice_item(item, now)
        
        if freezable:
            freezable.freeze(end=now, deletion=True)

    def _find_invoice_item(self, item, now):
        """
        Looks for corresponding invoice item in the invoices by the given date.
        :param item: chargeable item to use for search of invoice item.
        :param now: date of invoice with invoice items.
        :return: invoice item or None
        """
        raise NotImplementedError()


class OpenStackItemRegistrator(BaseRegistrator):

    def _find_invoice_item(self, item, now):
        package = item
        result = models.OpenStackItem.objects.get(
            package=package,
            invoice__customer=self._get_customer(package),
            invoice__state=models.Invoice.States.PENDING,
            invoice__year=now.year,
            invoice__month=now.month,
        )
        return result

    def _get_customer(self, item):
        return item.tenant.service_project_link.project.customer

    def _get_chargeable_items(self, customer):
        result = packages_models.OpenStackPackage.objects.filter(
            tenant__service_project_link__project__customer=customer,
        ).distinct()
        return result

    def _register_item(self, invoice, item, start, end):
        package = item
        overlapping_item = models.OpenStackItem.objects.filter(
            invoice=invoice,
            end__day=start.day,
            package_details__contains=package.tenant.name,
        ).order_by('-daily_price').first()

        daily_price = package.template.price
        if overlapping_item:
            """
            Notes:
            |- date -| - used during the date
            |- **** -| - used during the day
            |- ---- -| - was requested to use in the current day but will be moved to next or previous one.
            |-***?---| - was used for a half day and '?' stands for a conflict.

            If there is an item that overlaps with current one as shown below:
            |--03.01.2017-|-********-|-***?---|
                                     |----?**-|-01.06.2017-|-******-|
            we have to make next steps:
            1) If item is more expensive -> use it for price calculation
                and register new package starting from next day [-01.06.2017-]
            |--03.01.2017-|-********-|-*****-|
                                     |-------|-06.01.2017-|-******-|

            2) If old package item is more expensive and it is the end of the month
            extend package usage till the end of the day and set current package end date to start date,
            so that usage days is 0 but it is still registered in the invoice.
            |--29.01.2017-|-********-|-***31.01.2017***-|
                                     |----31.01.2017----|

            3) If item is cheaper do exactly the opposite and shift its end date to yesterday,
            so new package will be registered today
            |--03.01.2017-|-********-|-------|
                                     |-*****-|-06.01.2017-|-******-|
            """
            if overlapping_item.daily_price > daily_price:
                if overlapping_item.end.day == utils.get_current_month_end().day:
                    overlapping_item.extend_to_the_end_of_the_day()
                    end = start
                else:
                    start = start + timezone.timedelta(days=1)
            else:
                overlapping_item.shift_backward()

        models.OpenStackItem.objects.create(
            package=package,
            daily_price=daily_price,
            invoice=invoice,
            start=start,
            end=end)


class OfferingItemRestirator(BaseRegistrator):

    def _get_chargeable_items(self, customer):
        result = support_models.Offering.objects.filter(
            project__customer=customer
        ).distinct()
        return result

    def _get_customer(self, item):
        return item.project.customer

    def _find_invoice_item(self, item, now):
        result = models.OfferingItem.objects.filter(
            offering=item,
            invoice__customer=item.project.customer,
            invoice__state=models.Invoice.States.PENDING,
            invoice__year=now.year,
            invoice__month=now.month,
        ).first()
        return result

    def _register_item(self, invoice, item, start, end):
        offering = item
        result = models.OfferingItem.objects.create(
            offering=offering,
            daily_price=offering.price,
            invoice=invoice,
            start=start,
            end=end
        )
        return result
