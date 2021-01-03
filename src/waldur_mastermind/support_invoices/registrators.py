import logging

from django.contrib.contenttypes.models import ContentType
from django.core.exceptions import MultipleObjectsReturned, ObjectDoesNotExist

from waldur_core.structure.models import Project
from waldur_mastermind.invoices import models as invoice_models
from waldur_mastermind.invoices import registrators
from waldur_mastermind.marketplace import models as marketplace_models
from waldur_mastermind.marketplace import utils as marketplace_utils
from waldur_mastermind.support import models as support_models

logger = logging.getLogger(__name__)

BillingTypes = marketplace_models.OfferingComponent.BillingTypes
OrderTypes = marketplace_models.OrderItem.Types


class OfferingRegistrator(registrators.BaseRegistrator):
    def get_sources(self, customer):
        return support_models.Offering.objects.filter(
            project__customer=customer, state=support_models.Offering.States.OK,
        ).distinct()

    def get_customer(self, source):
        project = Project.all_objects.get(id=source.project_id)
        return project.customer

    def _find_item(self, source, now):
        support_offering = source
        model_type = ContentType.objects.get_for_model(support_offering)
        resources_ids = marketplace_models.Resource.objects.filter(
            object_id=support_offering.id, content_type=model_type
        ).values_list('id', flat=True)
        result = invoice_models.InvoiceItem.objects.filter(
            object_id__in=resources_ids,
            invoice__customer=self.get_customer(support_offering),
            invoice__state=invoice_models.Invoice.States.PENDING,
            invoice__year=now.year,
            invoice__month=now.month,
        )
        return list(result)

    def _create_item(self, source, invoice, start, end, **kwargs):
        support_offering = source

        try:
            resource = marketplace_models.Resource.objects.get(scope=support_offering)
            self.create_items_for_plan(
                invoice, resource, support_offering, start, end, **kwargs
            )

        except marketplace_models.Resource.DoesNotExist:
            logger.error('Offering isn\'t request based support offering.')

    def create_items_for_plan(
        self, invoice, resource, support_offering, start, end, **kwargs
    ):
        plan = resource.plan

        if not plan:
            logger.warning(
                'Skipping support invoice creation because '
                'billing is not enabled for resource. '
                'Resource ID: %s',
                resource.id,
            )
            return

        order_type = kwargs.get('order_type')

        for plan_component in plan.components.all():
            offering_component = plan_component.component

            is_fixed = offering_component.billing_type == BillingTypes.FIXED
            is_usage = offering_component.billing_type == BillingTypes.USAGE
            is_one = offering_component.billing_type == BillingTypes.ONE_TIME
            is_switch = offering_component.billing_type == BillingTypes.ON_PLAN_SWITCH

            if (
                is_fixed
                or (is_one and order_type == OrderTypes.CREATE)
                or (is_switch and order_type == OrderTypes.UPDATE)
                or (is_usage and offering_component.use_limit_for_billing)
            ):
                details = self.get_component_details(support_offering, plan_component)

                unit_price = plan_component.price
                unit = plan.unit
                quantity = 0

                if is_fixed:
                    unit_price *= plan_component.amount
                elif is_one or is_switch:
                    unit = invoice_models.Units.QUANTITY
                    quantity = 1
                elif is_usage:
                    unit = invoice_models.Units.QUANTITY
                    quantity = resource.limits.get(offering_component.type, 0)

                item = invoice_models.InvoiceItem.objects.create(
                    content_type=ContentType.objects.get_for_model(resource),
                    object_id=resource.id,
                    project=resource.project,
                    invoice=invoice,
                    start=start,
                    end=end,
                    details=details,
                    unit_price=unit_price,
                    unit=unit,
                    quantity=quantity,
                    product_code=offering_component.product_code or plan.product_code,
                    article_code=offering_component.article_code or plan.article_code,
                )
                self.init_details(item)

    def get_details(self, source):
        support_offering = source

        try:
            resource = marketplace_models.Resource.objects.get(scope=source)
            details = marketplace_utils.get_offering_details(resource.offering)
        except (ObjectDoesNotExist, MultipleObjectsReturned):
            details = {}

        service_provider_info = marketplace_utils.get_service_provider_info(
            support_offering
        )
        details.update(service_provider_info)
        details['plan_name'] = (
            support_offering.plan.name if support_offering.plan else ''
        )
        return details

    def get_name(self, resource):
        offering = resource.scope

        if offering.plan:
            return '%s (%s / %s)' % (offering.name, offering.type, offering.plan.name)
        else:
            return '%s (%s)' % (offering.name, offering.type)

    def get_component_details(self, support_offering, plan_component):
        details = self.get_details(support_offering)
        details.update(
            {
                'plan_component_id': plan_component.id,
                'offering_component_type': plan_component.component.type,
                'offering_component_name': plan_component.component.name,
                'offering_component_measured_unit': plan_component.component.measured_unit,
            }
        )
        return details
