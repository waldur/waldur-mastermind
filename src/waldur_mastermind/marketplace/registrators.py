import logging

from django.db.models import signals
from django.utils import timezone

from waldur_core.core import utils as core_utils
from waldur_core.structure.models import Project
from waldur_mastermind.common import mixins as common_mixins
from waldur_mastermind.invoices import models as invoice_models
from waldur_mastermind.invoices import registrators
from waldur_mastermind.marketplace import PLUGIN_NAME
from waldur_mastermind.marketplace import models as marketplace_models
from waldur_mastermind.marketplace import utils

logger = logging.getLogger(__name__)

BillingTypes = marketplace_models.OfferingComponent.BillingTypes
OrderTypes = marketplace_models.OrderItem.Types
ResourceStates = marketplace_models.Resource.States


class MarketplaceRegistrator(registrators.BaseRegistrator):
    plugin_name = PLUGIN_NAME

    def _find_item(self, source, now):
        """
        Find an item or some items by source and date.
        :param source: object that was bought by customer.
        :param now: date of invoice with invoice items.
        :return: invoice item, item's list (or another iterable object, f.e. tuple or queryset) or None
        """

        return list(
            invoice_models.InvoiceItem.objects.filter(
                resource=source,
                invoice__customer=self.get_customer(source),
                invoice__state=invoice_models.Invoice.States.PENDING,
                invoice__year=now.year,
                invoice__month=now.month,
                end=core_utils.month_end(now),
            )
        )

    def get_sources(self, customer):
        return (
            marketplace_models.Resource.objects.filter(
                offering__type=self.plugin_name, project__customer=customer,
            )
            .exclude(
                state__in=[
                    marketplace_models.Resource.States.CREATING,
                    marketplace_models.Resource.States.TERMINATED,
                ]
            )
            .distinct()
        )

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
                    quantity = resource.limits.get(offering_component.type, 0)

                invoice_models.InvoiceItem.objects.create(
                    name=self.get_name(resource) + ' / ' + offering_component.name,
                    details=self.get_component_details(resource, plan_component),
                    resource=resource,
                    project=resource.project,
                    invoice=invoice,
                    start=start,
                    end=end,
                    unit_price=unit_price,
                    unit=unit,
                    quantity=quantity,
                    measured_unit=plan_component.component.measured_unit,
                    article_code=offering_component.article_code or plan.article_code,
                )

    @classmethod
    def get_component_details(cls, resource, plan_component):
        customer = resource.offering.customer
        service_provider = getattr(customer, 'serviceprovider', None)

        return {
            'resource_name': resource.name,
            'resource_uuid': resource.uuid.hex,
            'plan_name': resource.plan.name if resource.plan else '',
            'plan_uuid': resource.plan.uuid.hex if resource.plan else '',
            'offering_type': resource.offering.type,
            'offering_name': resource.offering.name,
            'offering_uuid': resource.offering.uuid.hex,
            'service_provider_name': customer.name,
            'service_provider_uuid': ''
            if not service_provider
            else service_provider.uuid.hex,
            'plan_component_id': plan_component.id,
            'offering_component_type': plan_component.component.type,
            'offering_component_name': plan_component.component.name,
        }

    def get_name(self, resource):
        if resource.plan:
            return '%s (%s / %s)' % (
                resource.name,
                resource.offering.name,
                resource.plan.name,
            )
        else:
            return '%s (%s)' % (resource.name, resource.offering.name)

    @classmethod
    def on_resource_post_save(cls, sender, instance, created=False, **kwargs):
        resource = instance

        if resource.offering.type != cls.plugin_name:
            return

        if created:
            return

        if (
            resource.state == ResourceStates.OK
            and resource.tracker.previous('state') == ResourceStates.CREATING
        ):
            registrators.RegistrationManager.register(
                resource, timezone.now(), order_type=OrderTypes.CREATE
            )
        if (
            resource.state == ResourceStates.TERMINATED
            and instance.tracker.previous('state') == ResourceStates.TERMINATING
        ):
            registrators.RegistrationManager.terminate(resource, timezone.now())

        if resource.tracker.has_changed('plan_id'):
            registrators.RegistrationManager.terminate(resource, timezone.now())
            registrators.RegistrationManager.register(
                resource, timezone.now(), order_type=OrderTypes.UPDATE
            )

    @classmethod
    def add_component_usage(cls, sender, instance, created=False, **kwargs):
        component_usage = instance
        resource = component_usage.resource

        if not created and not component_usage.tracker.has_changed('usage'):
            return

        if resource.offering.type != cls.plugin_name:
            return

        offering_component = component_usage.component

        plan_period = component_usage.plan_period
        if not plan_period:
            logger.warning(
                'Skipping processing of component usage with ID %s because '
                'plan period is not defined.',
                component_usage.id,
            )
            return
        plan = plan_period.plan

        item = utils.get_invoice_item_for_component_usage(component_usage)
        if item:
            item.quantity = cls.convert_usage_quantity(
                component_usage.usage, offering_component.type
            )
            item.save()
        else:
            plan_component = plan.components.get(component=offering_component)
            customer = resource.project.customer
            invoice, created = registrators.RegistrationManager.get_or_create_invoice(
                customer, component_usage.date
            )

            details = cls.get_component_details(resource, plan_component)

            month_start = core_utils.month_start(component_usage.date)
            month_end = core_utils.month_end(component_usage.date)

            start = (
                month_start
                if not component_usage.plan_period.start
                else max(component_usage.plan_period.start, month_start)
            )
            end = (
                month_end
                if not component_usage.plan_period.end
                else min(component_usage.plan_period.end, month_end)
            )

            invoice_models.InvoiceItem.objects.create(
                resource=resource,
                project=resource.project,
                invoice=invoice,
                start=start,
                end=end,
                details=details,
                unit_price=plan_component.price,
                quantity=cls.convert_usage_quantity(
                    component_usage.usage, offering_component.type
                ),
                unit=common_mixins.UnitPriceMixin.Units.QUANTITY,
                measured_unit=offering_component.measured_unit,
                article_code=offering_component.article_code or plan.article_code,
                name=resource.name + ' / ' + offering_component.name,
            )

    @classmethod
    def convert_usage_quantity(cls, usage, component_type: str):
        return usage

    @classmethod
    def connect(cls):
        registrators.RegistrationManager.add_registrator(cls.plugin_name, cls)

        signals.post_save.connect(
            cls.on_resource_post_save,
            sender=marketplace_models.Resource,
            dispatch_uid='%s.on_resource_post_save' % cls.__name__,
        )

        signals.post_save.connect(
            cls.add_component_usage,
            sender=marketplace_models.ComponentUsage,
            dispatch_uid='%s.add_component_usage' % cls.__name__,
        )
