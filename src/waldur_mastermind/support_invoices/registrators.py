from __future__ import unicode_literals

import logging
from django.contrib.contenttypes.models import ContentType

from waldur_mastermind.invoices import models as invoice_models
from waldur_mastermind.invoices import registrators
from waldur_mastermind.marketplace import models as marketplace_models
from waldur_mastermind.marketplace import utils as marketplace_utils
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
        )
        return list(result)

    def _create_item(self, source, invoice, start, end):
        offering = source

        try:
            resource = marketplace_models.Resource.objects.get(scope=offering)
            plan = resource.plan

            if not plan:
                logger.warning('Skipping support invoice creation because '
                               'billing is not enabled for offering. '
                               'Offering ID: %s', offering.id)
                return

            for plan_component in plan.components.all():
                offering_component = plan_component.component

                if offering_component.billing_type == marketplace_models.OfferingComponent.BillingTypes.FIXED:
                    details = self.get_component_details(offering, plan_component)
                    invoice_models.GenericInvoiceItem.objects.create(
                        content_type=ContentType.objects.get_for_model(offering),
                        object_id=offering.id,
                        project=offering.project,
                        invoice=invoice,
                        start=start,
                        end=end,
                        details=details,
                        unit_price=plan_component.price,
                        unit=plan.unit,
                        product_code=offering_component.product_code or plan.product_code,
                        article_code=offering_component.article_code or plan.article_code,
                    )

        except marketplace_models.Resource.DoesNotExist:
            # If an offering isn't request based support offering
            return invoice_models.GenericInvoiceItem.objects.create(
                content_type=ContentType.objects.get_for_model(offering),
                object_id=offering.id,
                project=offering.project,
                invoice=invoice,
                start=start,
                end=end,
                details=self.get_details(offering),
                unit_price=offering.unit_price,
                unit=offering.unit,
                product_code=offering.product_code,
                article_code=offering.article_code
            )

    def get_details(self, source):
        offering = source
        details = {
            'name': self.get_name(offering),
            'offering_type': offering.type,
            'offering_name': offering.name,
            'offering_uuid': offering.uuid.hex,
            'plan_name': offering.plan.name if offering.plan else ''
        }
        service_provider_info = marketplace_utils.get_service_provider_info(source)
        details.update(service_provider_info)
        return details

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

    def get_component_details(self, offering, plan_component):
        details = self.get_details(offering)
        details.update({
            'plan_component_id': plan_component.id,
            'offering_component_type': plan_component.component.type,
            'offering_component_name': plan_component.component.name,
            'offering_component_measured_unit': plan_component.component.measured_unit,
        })
        return details
