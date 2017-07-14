"""
Business logic of invoice item registration.

Glossary:
 - item - invoice item. Part of the invoice that stores price for
          some object that was bought by customer.
 - source - invoice item source. Object that was bought by customer.

Example: Offering (source) ->  OfferingItem (item).

RegistrationManager represents the highest level of business logic and should be
used for invoice items registration and termination.
Registrators defines items creation and termination logic for each invoice item.
"""
from django.db import transaction
from django.utils import timezone

from nodeconductor.core import utils as core_utils
from nodeconductor.structure.permissions import _get_project
from nodeconductor_assembly_waldur.packages import models as packages_models
from nodeconductor_assembly_waldur.support import models as support_models

from . import models, utils


class BaseRegistrator(object):

    def get_customer(self, source):
        """ Return customer based on provided item. """
        raise NotImplementedError()

    def register(self, sources, invoice, start):
        """ For each source create invoice item and register it in invoice. """
        end = core_utils.month_end(start)
        for source in sources:
            self._create_item(source, invoice, start=start, end=end)

    def get_sources(self, customer):
        """ Return a list of invoice item sources to charge customer for. """
        raise NotImplementedError()

    def _create_item(self, source, invoice, start, end):
        """ Register single chargeable item in the invoice. """
        raise NotImplementedError()

    def terminate(self, item, now=None):
        """
        Freeze invoice item's usage.
        :param item: chargeable item to use for search of invoice item.
        :param now: date of invoice with invoice items.
        """
        if not now:
            now = timezone.now()

        item = self._find_item(item, now)
        if item:
            item.terminate(end=now)

    def _find_item(self, source, now):
        """
        Find item by source and date.
        :param source: object that was bought by customer.
        :param now: date of invoice with invoice items.
        :return: invoice item or None
        """
        raise NotImplementedError()

    def has_sources(self, customer):
        """ Indicate whether customer has any invoice item source. """
        raise NotImplementedError()


class OpenStackItemRegistrator(BaseRegistrator):

    def _find_item(self, source, now):
        result = models.OpenStackItem.objects.filter(
            package=source,
            invoice__customer=self.get_customer(source),
            invoice__state=models.Invoice.States.PENDING,
            invoice__year=now.year,
            invoice__month=now.month,
        ).first()
        return result

    def get_customer(self, source):
        return source.tenant.service_project_link.project.customer

    def get_sources(self, customer):
        return packages_models.OpenStackPackage.objects.filter(
            tenant__service_project_link__project__customer=customer).distinct()

    def has_sources(self, customer):
        return self.get_sources(customer).exists()

    def _create_item(self, source, invoice, start, end):
        package = source
        overlapping_item = models.OpenStackItem.objects.filter(
            invoice=invoice,
            end__day=start.day,
            package_details__contains=package.tenant.name,
        ).order_by('-unit_price').first()

        daily_price = package.template.price
        product_code = package.template.product_code
        article_code = package.template.article_code
        if overlapping_item:
            """
            Notes:
            |- date -| - used during the date
            |- **** -| - used during the day
            |- ---- -| - was requested to use in the current day but will be moved to next or previous one.
            |-***?---| - was used for a half day and '?' stands for a conflict.

            If there is an item that overlaps with current one as shown below:
            |--03.01.2017-|-********-|-***?---|
                                     |----?**-|-06.01.2017-|-******-|
            we have to make next steps:
            1) If item is more expensive -> use it for price calculation
                and register new package starting from next day [-06.01.2017-]
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
            if overlapping_item.unit_price > daily_price:
                if overlapping_item.end.day == utils.get_current_month_end().day:
                    overlapping_item.extend_to_the_end_of_the_day()
                    end = start
                else:
                    start = start + timezone.timedelta(days=1)
            else:
                overlapping_item.shift_backward()

        models.OpenStackItem.objects.create(
            package=package,
            project=_get_project(package),
            unit_price=daily_price,
            unit=models.OpenStackItem.Units.PER_DAY,
            product_code=product_code,
            article_code=article_code,
            invoice=invoice,
            start=start,
            end=end)


class OfferingItemRegistrator(BaseRegistrator):

    def get_sources(self, customer):
        return support_models.Offering.objects.filter(project__customer=customer).distinct()

    def has_sources(self, customer):
        return self.get_sources(customer).exists()

    def get_customer(self, source):
        return source.project.customer

    def _find_item(self, source, now):
        offering = source
        result = models.OfferingItem.objects.filter(
            offering=offering,
            invoice__customer=offering.project.customer,
            invoice__state=models.Invoice.States.PENDING,
            invoice__year=now.year,
            invoice__month=now.month,
        ).first()
        return result

    def _create_item(self, source, invoice, start, end):
        offering = source
        result = models.OfferingItem.objects.create(
            offering=offering,
            project=offering.project,
            unit_price=offering.unit_price,
            unit=offering.unit,
            product_code=offering.product_code,
            article_code=offering.article_code,
            invoice=invoice,
            start=start,
            end=end,
        )
        return result


class RegistrationManager(object):
    """ The highest interface for invoice item registration and termination. """
    _registrators = {
        packages_models.OpenStackPackage: OpenStackItemRegistrator(),
        support_models.Offering: OfferingItemRegistrator(),
    }

    @classmethod
    def get_registrators(cls):
        return cls._registrators.values()

    @classmethod
    def has_sources(cls, customer):
        return any(registrator.has_sources(customer) for registrator in cls.get_registrators())

    @classmethod
    def get_or_create_invoice(cls, customer, date):
        invoice, created = models.Invoice.objects.get_or_create(
            customer=customer,
            month=date.month,
            year=date.year,
        )

        if created:
            for registrator in cls.get_registrators():
                sources = registrator.get_sources(customer)
                registrator.register(sources, invoice, date)

        return invoice, created

    @classmethod
    def register(cls, source, now=None):
        """
        Create new invoice item from source and register it into invoice.

        If invoice does not exist new one will be created.
        """
        if now is None:
            now = timezone.now()

        registrator = cls._registrators[source.__class__]
        customer = registrator.get_customer(source)

        with transaction.atomic():
            invoice, created = cls.get_or_create_invoice(customer, now)
            if not created:
                registrator.register([source], invoice, now)

    @classmethod
    def terminate(cls, source, now=None):
        """
        Terminate invoice item that corresponds given source.

        :param now: time to set as end of item usage.
        """
        if now is None:
            now = timezone.now()

        registrator = cls._registrators[source.__class__]
        customer = registrator.get_customer(source)

        with transaction.atomic():
            cls.get_or_create_invoice(customer, now)
            registrator.terminate(source, now)
