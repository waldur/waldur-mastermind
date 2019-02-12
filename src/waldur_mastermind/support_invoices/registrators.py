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
            resource = marketplace_models.Resource.objects.get(scope=offering)

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
        except ObjectDoesNotExist:
            invoice_models.GenericInvoiceItem.objects.get_or_create(
                scope=offering,
                project=offering.project,
                invoice=invoice,
                start=start,
                end=end,
                defaults={'unit_price': offering.unit_price,
                          'unit': offering.unit,
                          'product_code': offering.product_code,
                          'article_code': offering.article_code,
                          'details': self.get_details(offering),
                          }
            )

    def get_details(self, source):
        offering = source
        return {
            'name': '%s (%s)' % (offering.name, offering.type),
            'offering_type': offering.type,
            'offering_name': offering.name,
            'offering_uuid': offering.uuid.hex,
        }

    def get_name(self, source):
        return '%s (%s)' % (source.name, source.type)

    def terminate(self, source, now=None):
        super(OfferingRegistrator, self).terminate(source, now)
        offering = source

        if not utils.is_request_based(offering):
            utils.get_offering_items().filter(object_id=offering.id).update(object_id=None)
