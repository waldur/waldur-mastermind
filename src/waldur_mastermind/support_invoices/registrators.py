from __future__ import unicode_literals

import logging

from django.core.exceptions import ObjectDoesNotExist

from waldur_mastermind.invoices import models as invoice_models
from waldur_mastermind.invoices import registrators
from waldur_mastermind.marketplace import models as marketplace_models
from waldur_mastermind.support import models as support_models

from . import utils

logger = logging.getLogger(__name__)


class OfferingRegistrator(registrators.BaseRegistrator):
    def get_sources(self, customer):
        return support_models.Offering.objects.filter(
            project__customer=customer,
            state=support_models.Offering.States.OK,
        ).distinct()

    def get_customer(self, source):
        return source.project.customer

    def _find_item(self, source, now):
        offering = source
        result = utils.get_offering_items().filter(
            object_id=offering.id,
            invoice__customer=offering.project.customer,
            invoice__state=invoice_models.Invoice.States.PENDING,
            invoice__year=now.year,
            invoice__month=now.month,
        ).first()
        return result

    def _create_item(self, source, invoice, start, end):
        offering = source

        try:
            plan = marketplace_models.Resource.objects.get(scope=offering).plan
            if not plan:
                logger.warning('Skipping support invoice creation because '
                               'billing is not enabled for offering. '
                               'Offering ID: %s', offering.id)
                return
        except ObjectDoesNotExist:
            plan = offering

        return invoice_models.GenericInvoiceItem.objects.create(
            scope=offering,
            project=offering.project,
            invoice=invoice,
            start=start,
            end=end,
            details=self.get_details(offering),
            unit_price=plan.unit_price,
            unit=plan.unit,
            product_code=plan.product_code,
            article_code=plan.article_code,
        )

    def get_details(self, source):
        offering = source
        return {
            'name': self.get_name(offering),
            'offering_type': offering.type,
            'offering_name': offering.name,
            'offering_uuid': offering.uuid.hex,
        }

    def get_name(self, offering):
        if offering.plan:
            return '%s (%s / %s)' % (offering.name, offering.type, offering.plan.name)
        else:
            return '%s (%s)' % (offering.name, offering.type)

    def terminate(self, source, now=None):
        super(OfferingRegistrator, self).terminate(source, now)
        offering = source

        if not utils.is_request_based(offering):
            utils.get_offering_items().filter(object_id=offering.id).update(object_id=None)
