import logging

from django.contrib.contenttypes.models import ContentType
from django.db.models import signals
from django.utils import timezone

from waldur_core.structure.models import Project
from waldur_mastermind.invoices import models as invoice_models
from waldur_mastermind.invoices import registrators
from waldur_mastermind.marketplace import PLUGIN_NAME
from waldur_mastermind.marketplace import models as marketplace_models
from waldur_mastermind.marketplace import utils as marketplace_utils

logger = logging.getLogger(__name__)

BillingTypes = marketplace_models.OfferingComponent.BillingTypes
OrderTypes = marketplace_models.OrderItem.Types


class MarketplaceRegistrator(registrators.BaseRegistrator):
    plugin_name = PLUGIN_NAME

    def get_sources(self, customer):
        return marketplace_models.Resource.objects.filter(
            offering__type=self.plugin_name, state=marketplace_models.Resource.States.OK
        ).distinct()

    def get_customer(self, source):
        project = Project.all_objects.get(id=source.project_id)
        return project.customer

    def _create_item(self, source, invoice, start, end, **kwargs):
        resource = source
        plan = resource.plan

        if not plan:
            logger.warning(
                'Skipping an invoice creation because '
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
                    quantity = resource.limits.get(offering_component.type)

                item = invoice_models.InvoiceItem.objects.create(
                    content_type=ContentType.objects.get_for_model(resource),
                    object_id=resource.id,
                    project=resource.project,
                    invoice=invoice,
                    start=start,
                    end=end,
                    unit_price=unit_price,
                    unit=unit,
                    quantity=quantity,
                    product_code=offering_component.product_code or plan.product_code,
                    article_code=offering_component.article_code or plan.article_code,
                )
                self.init_details(item, resource, plan_component)

    def init_details(self, item, resource, plan_component):
        item.details = marketplace_utils.get_offering_details(resource.offering)
        item.details.update(
            {
                'plan_component_id': plan_component.id,
                'offering_component_type': plan_component.component.type,
                'offering_component_name': plan_component.component.name,
                'offering_component_measured_unit': plan_component.component.measured_unit,
                'scope_uuid': item.scope.uuid.hex,
            }
        )
        item.name = self.get_name(item.scope)
        item.save()

    @classmethod
    def handler(cls, sender, instance, created=False, **kwargs):
        resource = instance

        if resource.offering.type != cls.plugin_name:
            return

        if created:
            return

        if (
            resource.state == marketplace_models.Resource.States.OK
            and resource.tracker.previous('state')
            == marketplace_models.Resource.States.CREATING
        ):
            registrators.RegistrationManager.register(
                resource, timezone.now(), order_type=OrderTypes.CREATE
            )
        if (
            resource.state == marketplace_models.Resource.States.TERMINATED
            and instance.tracker.previous('state')
            == marketplace_models.Resource.States.TERMINATING
        ):
            registrators.RegistrationManager.terminate(resource, timezone.now())

    @classmethod
    def connect(cls):
        registrators.RegistrationManager.add_registrator(cls.plugin_name, cls)

        signals.post_save.connect(
            cls.handler,
            sender=marketplace_models.Resource,
            dispatch_uid='%s.handler' % cls.__name__,
        )
