import logging

from django.contrib.contenttypes.models import ContentType

from waldur_core.core import utils as core_utils
from waldur_mastermind.common import mixins as common_mixins
from waldur_mastermind.invoices import models as invoice_models
from waldur_mastermind.invoices import registrators
from waldur_mastermind.marketplace import models as marketplace_models
from waldur_mastermind.marketplace.models import Resource
from waldur_mastermind.support import models as support_models


logger = logging.getLogger(__name__)


def is_request_based(offering):
    return Resource.objects.filter(scope=offering).exists()


def get_offering_items():
    model_type = ContentType.objects.get_for_model(support_models.Offering)
    return invoice_models.GenericInvoiceItem.objects.filter(content_type=model_type)


def component_usage_register(component_usage):
    from waldur_mastermind.support_invoices.registrators import OfferingRegistrator

    plan_period = component_usage.plan_period
    if not plan_period:
        logger.warning('Skipping processing of component usage with ID %s because '
                       'plan period is not defined.', component_usage.id)
        return
    plan = plan_period.plan

    try:
        plan_component = plan.components.get(component=component_usage.component)
        item = invoice_models.GenericInvoiceItem.objects.get(scope=component_usage.resource.scope,
                                                             details__plan_period_id=plan_period.id,
                                                             details__plan_component_id=plan_component.id)
        item.quantity = component_usage.usage
        item.unit_price = plan_component.price
        item.save()
    except invoice_models.GenericInvoiceItem.DoesNotExist:
        offering = component_usage.resource.scope
        customer = offering.project.customer
        invoice, created = registrators.RegistrationManager.get_or_create_invoice(customer, component_usage.date)

        details = OfferingRegistrator().get_component_details(offering, plan_component)
        details['plan_period_id'] = plan_period.id
        offering_component = plan_component.component

        month_start = core_utils.month_start(component_usage.date)
        month_end = core_utils.month_end(component_usage.date)

        start = month_start if not component_usage.plan_period.start else \
            max(component_usage.plan_period.start, month_start)
        end = month_end if not component_usage.plan_period.end else \
            min(component_usage.plan_period.end, month_end)

        invoice_models.GenericInvoiceItem.objects.create(
            content_type=ContentType.objects.get_for_model(offering),
            object_id=offering.id,
            project=offering.project,
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
    except invoice_models.GenericInvoiceItem.MultipleObjectsReturned:
        logger.warning('Skipping the invoice item unit price update '
                       'because multiple GenericInvoiceItem objects found. Scope: %s %s, date: %s.',
                       component_usage.resource.content_type,
                       component_usage.resource.object_id,
                       component_usage.date)
