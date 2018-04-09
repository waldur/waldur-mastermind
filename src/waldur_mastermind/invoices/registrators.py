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

from waldur_core.core import utils as core_utils


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

    def terminate(self, source, now=None):
        """
        Freeze invoice item's usage.
        :param source: chargeable item to use for search of invoice item.
        :param now: date of invoice with invoice items.
        """
        if not now:
            now = timezone.now()

        item = self._find_item(source, now)
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

    def get_name(self, source):
        return source.name

    def get_details(self, source):
        return {}


class RegistrationManager(object):
    """ The highest interface for invoice item registration and termination. """
    _registrators = {}

    @classmethod
    def get_registrators(cls):
        return cls._registrators.values()

    @classmethod
    def add_registrator(cls, model, registrator):
        cls._registrators[model] = registrator()

    @classmethod
    def get_registrator(cls, source):
        return cls._registrators[source.__class__]

    @classmethod
    def get_or_create_invoice(cls, customer, date):
        from . import models
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

    @classmethod
    def get_item(cls, source, now=None):
        if now is None:
            now = timezone.now()

        registrator = cls._registrators[source.__class__]
        return registrator._find_item(source, now)

    @classmethod
    def get_name(cls, source):
        registrator = cls._registrators[source.__class__]
        return registrator.get_name(source)

    @classmethod
    def get_details(cls, source):
        registrator = cls._registrators[source.__class__]
        return registrator.get_details(source)
