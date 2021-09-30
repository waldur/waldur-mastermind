import logging
from datetime import timedelta

from django.core.exceptions import ObjectDoesNotExist
from django.db import transaction
from django.db.models import signals
from django.utils import timezone

from waldur_core.core import utils as core_utils
from waldur_core.structure.models import Project
from waldur_mastermind.common import mixins as common_mixins
from waldur_mastermind.common.utils import parse_datetime
from waldur_mastermind.invoices import models as invoice_models
from waldur_mastermind.invoices import registrators
from waldur_mastermind.invoices.registrators import RegistrationManager
from waldur_mastermind.invoices.utils import get_current_month_end, get_full_days
from waldur_mastermind.marketplace import PLUGIN_NAME
from waldur_mastermind.marketplace import models as marketplace_models
from waldur_mastermind.marketplace import utils

logger = logging.getLogger(__name__)

BillingTypes = marketplace_models.OfferingComponent.BillingTypes
LimitPeriods = marketplace_models.OfferingComponent.LimitPeriods
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
            is_one = offering_component.billing_type == BillingTypes.ONE_TIME
            is_switch = offering_component.billing_type == BillingTypes.ON_PLAN_SWITCH
            is_limit = offering_component.billing_type == BillingTypes.LIMIT

            if is_limit:
                # Avoid creating invoice item for limit-based components
                # if limit period is total and resource is not being created
                if offering_component.limit_period == LimitPeriods.TOTAL:
                    if order_type == OrderTypes.CREATE:
                        self.create_component_item(
                            source, plan_component, invoice, start, end
                        )
                        continue
                    else:
                        continue
                self.create_component_item(source, plan_component, invoice, start, end)
                continue

            if (
                is_fixed
                or (is_one and order_type == OrderTypes.CREATE)
                or (is_switch and order_type == OrderTypes.UPDATE)
            ):
                unit_price = plan_component.price
                unit = plan.unit
                quantity = 0

                if is_fixed:
                    unit_price *= plan_component.amount
                elif is_one or is_switch:
                    unit = invoice_models.Units.QUANTITY
                    quantity = 1

                invoice_models.InvoiceItem.objects.create(
                    name=f'{self.get_name(resource)} / {self.get_component_name(plan_component)}',
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
    def get_total_quantity(cls, unit, value, start, end):
        if unit == invoice_models.InvoiceItem.Units.PER_DAY:
            return value * get_full_days(start, end)
        return value

    @classmethod
    def create_component_item(cls, source, plan_component, invoice, start, end):
        offering_component = plan_component.component
        limit = source.limits.get(offering_component.type, 0)
        if not limit or limit == -1:
            return
        details = cls.get_component_details(source, plan_component)
        quantity = cls.convert_quantity(limit, offering_component.type)
        details['resource_limit_periods'] = [
            utils.serialize_resource_limit_period(
                {'start': start, 'end': end, 'quantity': quantity}
            )
        ]
        total_quantity = cls.get_total_quantity(
            plan_component.plan.unit, quantity, start, end
        )

        unit = plan_component.plan.unit
        if (
            plan_component.component.billing_type == BillingTypes.LIMIT
            and plan_component.component.limit_period == LimitPeriods.TOTAL
        ):
            unit = invoice_models.Units.QUANTITY

        invoice_models.InvoiceItem.objects.create(
            name=f'{RegistrationManager.get_name(source)} / {cls.get_component_name(plan_component)}',
            resource=source,
            project=Project.all_objects.get(id=source.project_id),
            unit_price=plan_component.price,
            unit=unit,
            quantity=total_quantity,
            article_code=offering_component.article_code or source.plan.article_code,
            invoice=invoice,
            start=start,
            end=end,
            details=details,
            measured_unit=offering_component.measured_unit,
        )

    @classmethod
    def update_component_item(cls, source, component_type, invoice, new_quantity):
        invoice_item = invoice_models.InvoiceItem.objects.get(
            resource=source,
            details__offering_component_type=component_type,
            invoice=invoice,
        )
        resource_limit_periods = invoice_item.details['resource_limit_periods']
        old_period = resource_limit_periods.pop()
        old_quantity = int(old_period['quantity'])
        old_start = parse_datetime(old_period['start'])
        today = timezone.now()
        new_quantity = cls.convert_quantity(new_quantity, component_type)
        if old_quantity == new_quantity:
            # Skip update if limit is the same
            return
        if old_quantity > new_quantity:
            old_end = today.replace(hour=23, minute=59, second=59)
            new_start = old_end + timedelta(seconds=1)
        else:
            new_start = today.replace(hour=0, minute=0, second=0)
            old_end = new_start - timedelta(seconds=1)
        old_period = utils.serialize_resource_limit_period(
            {'start': old_start, 'end': old_end, 'quantity': old_quantity}
        )
        new_period = utils.serialize_resource_limit_period(
            {
                'start': new_start,
                'end': get_current_month_end(),
                'quantity': new_quantity,
            }
        )
        resource_limit_periods.extend([old_period, new_period])
        plan_component = source.plan.components.get(component__type=component_type)
        invoice_item.quantity = sum(
            cls.get_total_quantity(
                plan_component.plan.unit,
                period['quantity'],
                parse_datetime(period['start']),
                parse_datetime(period['end']),
            )
            for period in resource_limit_periods
        )
        invoice_item.save(update_fields=['details', 'quantity'])

    @classmethod
    @transaction.atomic
    def create_or_update_component_item(cls, source, invoice, component_type, quantity):
        if invoice_models.InvoiceItem.objects.filter(
            resource=source,
            details__offering_component_type=component_type,
            invoice=invoice,
        ).exists():
            cls.update_component_item(source, component_type, invoice, quantity)
        else:
            start = timezone.now()
            end = get_current_month_end()
            plan_component = source.plan.components.get(component__type=component_type)
            cls.create_component_item(source, plan_component, invoice, start, end)

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

        if resource.tracker.has_changed('limits'):
            today = timezone.now()
            invoice, _ = registrators.RegistrationManager.get_or_create_invoice(
                resource.project.customer, core_utils.month_start(today)
            )
            valid_limits = set(
                resource.offering.components.filter(
                    billing_type=BillingTypes.LIMIT
                ).values_list('type', flat=True)
            )
            for component_type, new_quantity in resource.limits.items():
                if component_type not in valid_limits:
                    continue
                offering_component = resource.offering.components.get(
                    type=component_type
                )
                if (
                    offering_component.billing_type == BillingTypes.LIMIT
                    and offering_component.limit_period == LimitPeriods.TOTAL
                ):
                    cls.create_invoice_item_for_total_limit(
                        resource,
                        invoice,
                        component_type,
                        new_quantity,
                        offering_component,
                    )
                else:
                    cls.create_or_update_component_item(
                        resource, invoice, component_type, new_quantity
                    )

    @classmethod
    def create_invoice_item_for_total_limit(
        cls, resource, invoice, component_type, new_quantity, offering_component
    ):
        if resource.state != ResourceStates.OK:
            return
        related_invoice_items = invoice_models.InvoiceItem.objects.filter(
            resource=resource, details__offering_component_type=component_type,
        )
        if not related_invoice_items.exists():
            cls.create_or_update_component_item(
                resource, invoice, component_type, new_quantity
            )
        else:
            total = 0
            for invoice_item in related_invoice_items:
                if invoice_item.unit_price < 0:
                    total -= invoice_item.quantity
                else:
                    total += invoice_item.quantity
            diff = new_quantity - total
            if diff == 0:
                return
            plan_component = resource.plan.components.get(
                component__type=component_type
            )
            details = cls.get_component_details(resource, plan_component)
            start = timezone.now()
            end = get_current_month_end()
            invoice_models.InvoiceItem.objects.create(
                name=f'{RegistrationManager.get_name(resource)} / {cls.get_component_name(plan_component)}',
                resource=resource,
                project=Project.all_objects.get(id=resource.project_id),
                unit_price=plan_component.price if diff > 0 else -plan_component.price,
                unit=invoice_models.Units.QUANTITY,
                quantity=diff if diff > 0 else -diff,
                article_code=offering_component.article_code
                or resource.plan.article_code,
                invoice=invoice,
                start=start,
                end=end,
                details=details,
                measured_unit=offering_component.measured_unit,
            )

    @classmethod
    def update_invoice_when_usage_is_reported(
        cls, sender, instance, created=False, **kwargs
    ):
        component_usage = instance
        resource = component_usage.resource

        if not created and not component_usage.tracker.has_changed('usage'):
            return

        if resource.offering.type != cls.plugin_name:
            return

        offering_component = component_usage.component
        # It is allowed to report usage for limit-based components but they are ignored in invoicing
        if offering_component.billing_type != BillingTypes.USAGE:
            return

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
            item.quantity = cls.convert_quantity(
                component_usage.usage, offering_component.type
            )
            item.save()
        else:
            try:
                plan_component = plan.components.get(component=offering_component)
            except ObjectDoesNotExist:
                logger.warning(
                    'Skipping processing of component usage with ID %s because '
                    'plan component is not defined.',
                    component_usage.id,
                )
                return
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
                quantity=cls.convert_quantity(
                    component_usage.usage, offering_component.type
                ),
                unit=common_mixins.UnitPriceMixin.Units.QUANTITY,
                measured_unit=offering_component.measured_unit,
                article_code=offering_component.article_code or plan.article_code,
                name=resource.name + ' / ' + offering_component.name,
            )

    @classmethod
    def convert_quantity(cls, usage, component_type: str):
        return usage

    @classmethod
    def get_component_name(cls, plan_component):
        return plan_component.component.name

    @classmethod
    def connect(cls):
        registrators.RegistrationManager.add_registrator(cls.plugin_name, cls)

        signals.post_save.connect(
            cls.on_resource_post_save,
            sender=marketplace_models.Resource,
            dispatch_uid='%s.on_resource_post_save' % cls.__name__,
        )

        signals.post_save.connect(
            cls.update_invoice_when_usage_is_reported,
            sender=marketplace_models.ComponentUsage,
            dispatch_uid='waldur_mastermind.marketplace.'
            'update_invoice_when_usage_is_reported_%s' % cls.__name__,
        )
