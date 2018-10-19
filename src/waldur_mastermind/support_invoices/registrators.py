from __future__ import unicode_literals

import logging

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
        order_item = self.get_order_item(offering)
        for offering_component in order_item.offering.components.all():
            try:
                plan_component = offering_component.components.get(plan=order_item.plan)
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
                              'quantity': utils.get_quantity(plan_component, order_item, start, end),
                              }
                )
            except marketplace_models.PlanComponent.DoesNotExist:
                pass

    def get_order_item(self, offering):
        try:
            return marketplace_models.OrderItem.objects.get(scope=offering)
        except marketplace_models.OrderItem.DoesNotExist:
            logger.debug('Skip support invoice.')
