from __future__ import unicode_literals

import logging

from django.core.exceptions import ObjectDoesNotExist

from waldur_mastermind.invoices import models as invoice_models
from waldur_mastermind.invoices import registrators
from waldur_mastermind.marketplace import models as marketplace_models
from waldur_mastermind.support import models as support_models

from . import utils, models

logger = logging.getLogger(__name__)


class OfferingRegistrator(registrators.BaseRegistrator):
    def get_sources(self, customer):
        return models.RequestBasedOffering.objects.filter(project__customer=customer,
                                                          state=support_models.Offering.States.OK).distinct()

    def get_customer(self, source):
        return source.project.customer

    def _find_item(self, source, now):
        offering = source
        result = invoice_models.GenericInvoiceItem.objects.filter(
            scope=offering,
            invoice__customer=offering.project.customer,
            invoice__state=invoice_models.Invoice.States.PENDING,
            invoice__year=now.year,
            invoice__month=now.month,
        ).first()
        return result

    def _create_item(self, source, invoice, start, end):
        offering = source
        try:
            resource = marketplace_models.Resource.objects.get(scope=offering)
        except ObjectDoesNotExist:
            logger.warning('Skipping support invoice creation because resource does not exist. '
                           'Offering ID: %s', offering.id)
            return

        if not resource.plan:
            logger.warning('Skipping support invoice creation because resource does not have plan. '
                           'Resource ID: %s', resource.id)
            return

        for offering_component in resource.offering.components.all():
            try:
                plan_component = offering_component.components.get(plan=resource.plan)
                invoice_models.GenericInvoiceItem.objects.get_or_create(
                    scope=offering,
                    project=offering.project,
                    invoice=invoice,
                    start=start,
                    end=end,
                    details={'name': offering_component.type},
                    defaults={'product_code': offering.product_code,
                              'article_code': offering.article_code,
                              'unit_price': plan_component.price,
                              'unit': invoice_models.GenericInvoiceItem.Units.QUANTITY,
                              'quantity': utils.get_quantity(plan_component, resource, start, end),
                              }
                )
            except marketplace_models.PlanComponent.DoesNotExist:
                pass
