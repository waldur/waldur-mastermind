from django.db import transaction
from django.utils import timezone

from nodeconductor.core import utils as core_utils
from nodeconductor_assembly_waldur.packages import models as packages_models
from nodeconductor_assembly_waldur.support import models as support_models

from . import models, utils


class BaseRegistrator(object):

    def get_customer(self, item):
        """
        Returns customer based on provided item.
        :param item: item to get customer from.
        """
        raise NotImplementedError()

    def register_items(self, items, invoice, start):
        end = core_utils.month_end(start)

        for item in items:
            self._register_item(invoice=invoice, item=item, start=start, end=end)

    def get_chargeable_items(self, customer):
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

        invoice_item = self._find_invoice_item(item, now)
        
        if invoice_item:
            invoice_item.terminate(end=now)

    def _find_invoice_item(self, chargeable_item, now):
        """
        Looks for corresponding invoice item in the invoices by the given date.
        :param item: chargeable item to use for search of invoice item.
        :param now: date of invoice with invoice items.
        :return: invoice item or None
        """
        raise NotImplementedError()


class OpenStackItemRegistrator(BaseRegistrator):

    def _find_invoice_item(self, chargeable_item, now):
        package = chargeable_item
        result = models.OpenStackItem.objects.filter(
            package=package,
            invoice__customer=self.get_customer(package),
            invoice__state=models.Invoice.States.PENDING,
            invoice__year=now.year,
            invoice__month=now.month,
        ).first()
        return result

    def get_customer(self, item):
        return item.tenant.service_project_link.project.customer

    def get_chargeable_items(self, customer):
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


class OfferingItemRegistrator(BaseRegistrator):

    def get_chargeable_items(self, customer):
        result = support_models.Offering.objects.filter(
            project__customer=customer
        ).distinct()
        return result

    def get_customer(self, item):
        return item.project.customer

    def _find_invoice_item(self, chargeable_item, now):
        offering = chargeable_item
        result = models.OfferingItem.objects.filter(
            offering=offering,
            invoice__customer=offering.project.customer,
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


class RegistrationManager(object):
    _registrators = {
        packages_models.OpenStackPackage.__name__: OpenStackItemRegistrator(),
        support_models.Offering.__name__: OfferingItemRegistrator()
    }

    @classmethod
    def get_registrators(cls):
        return cls._registrators.values()

    @classmethod
    def register(cls, item, now=None):
        """
        Registers new item into existing invoice.
        In the beginning of the month new invoice is created and all related items are registered.
        :param item: item to register
        :param start: invoice item start date.
        """
        if now is None:
            now = timezone.now()

        item_registrator = cls._registrators[item.__class__.__name__]
        customer = item_registrator.get_customer(item)

        with transaction.atomic():
            invoice, created = models.Invoice.objects.get_or_create(
                customer=customer,
                month=now.month,
                year=now.year,
            )

            if created:
                for registrator in cls.get_registrators():
                    items = registrator.get_chargeable_items(customer)
                    registrator.register_items(items, invoice, now)
            else:
                item_registrator.register_items([item], invoice, now)

    @classmethod
    def terminate(cls, item, now=None):
        """

        :param item:
        :param now:
        :return:
        """
        if now is None:
            now = timezone.now()

        item_registrator = cls._registrators[item.__class__.__name__]
        customer = item_registrator.get_customer(item)

        with transaction.atomic():
            invoice, created = models.Invoice.objects.get_or_create(
                customer=customer,
                month=now.month,
                year=now.year,
            )

            if created:
                for registrator in cls.get_registrators():
                    items = registrator.get_chargeable_items(customer)
                    registrator.register_items(items, invoice, now)

            item_registrator.terminate(item, now)
