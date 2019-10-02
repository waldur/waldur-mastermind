import logging

from django.contrib.contenttypes.models import ContentType

from waldur_core.core import utils as core_utils
from waldur_mastermind.common import mixins as common_mixins
from waldur_mastermind.invoices import models as invoice_models
from waldur_mastermind.invoices import registrators
from waldur_mastermind.marketplace import models as marketplace_models


logger = logging.getLogger(__name__)


def component_usage_register(component_usage):
    from waldur_mastermind.slurm_invoices.registrators import AllocationRegistrator

    plan_period = component_usage.plan_period
    if not plan_period:
        logger.warning('Skipping processing of component usage with ID %s because '
                       'plan period is not defined.', component_usage.id)
        return

    try:
        plan = plan_period.plan
        plan_component = plan.components.get(component=component_usage.component)
        allocation = component_usage.resource.scope
        customer = allocation.project.customer
        invoice, created = registrators.RegistrationManager.get_or_create_invoice(customer, component_usage.date)

        details = AllocationRegistrator().get_component_details(allocation, plan_component)
        details['plan_period_id'] = plan_period.id
        offering_component = plan_component.component

        month_start = core_utils.month_start(component_usage.date)
        month_end = core_utils.month_end(component_usage.date)

        start = month_start if not component_usage.plan_period.start else \
            max(component_usage.plan_period.start, month_start)
        end = month_end if not component_usage.plan_period.end else \
            min(component_usage.plan_period.end, month_end)

        invoice_models.InvoiceItem.objects.create(
            content_type=ContentType.objects.get_for_model(allocation),
            object_id=allocation.id,
            project=allocation.project,
            invoice=invoice,
            start=start,
            end=end,
            details=details,
            unit_price=plan_component.price,
            quantity=component_usage.usage,
            unit=common_mixins.UnitPriceMixin.Units.QUANTITY,
            product_code=offering_component.product_code or plan.product_code,
            article_code=offering_component.article_code or plan.article_code,
        )

    except marketplace_models.PlanComponent.DoesNotExist:
        logger.warning('Plan component for usage component %s is not found.', component_usage.id)
