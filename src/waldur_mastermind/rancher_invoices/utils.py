import datetime
import logging

from django.contrib.contenttypes.models import ContentType
from django.core import exceptions as django_exceptions
from django.db.models import Q

from waldur_core.core import models as core_models
from waldur_core.core import utils as core_utils
from waldur_core.core.utils import month_start
from waldur_mastermind.common import mixins as common_mixins
from waldur_mastermind.invoices import models as invoice_models
from waldur_mastermind.invoices import registrators
from waldur_mastermind.marketplace import models as marketplace_models
from waldur_mastermind.marketplace.plugins import manager
from waldur_mastermind.marketplace_rancher import PLUGIN_NAME

logger = logging.getLogger(__name__)


def create_usage(cluster):
    try:
        resource = marketplace_models.Resource.objects.get(scope=cluster)
    except django_exceptions.ObjectDoesNotExist:
        logger.debug('Skipping node usage synchronization because this '
                     'marketplace.Resource does not exist.'
                     'Cluster ID: %s', cluster.id)
        return

    date = datetime.date.today()
    usage = cluster.node_set.filter(state=core_models.StateMixin.States.OK).count()

    for component in manager.get_components(PLUGIN_NAME):
        try:
            offering_component = marketplace_models.OfferingComponent.objects.get(
                offering=resource.offering,
                type=component.type
            )
            plan_period = marketplace_models.ResourcePlanPeriod.objects. \
                filter(Q(start__lte=date) | Q(start__isnull=True)). \
                filter(Q(end__gt=date) | Q(end__isnull=True)). \
                get(resource=resource)

            try:
                component_usage = marketplace_models.ComponentUsage.objects.get(
                    resource=resource,
                    component=offering_component,
                    billing_period=month_start(date),
                    plan_period=plan_period,
                )
                component_usage.usage = max(usage, component_usage.usage)
                component_usage.save()
            except django_exceptions.ObjectDoesNotExist:
                marketplace_models.ComponentUsage.objects.create(
                    resource=resource,
                    component=offering_component,
                    usage=usage,
                    date=date,
                    billing_period=month_start(date),
                    plan_period=plan_period,
                )

        except marketplace_models.OfferingComponent.DoesNotExist:
            logger.warning('Skipping node usage synchronization because this '
                           'marketplace.OfferingComponent does not exist.'
                           'Cluster ID: %s', cluster.id)
        except marketplace_models.ResourcePlanPeriod.DoesNotExist:
            logger.warning('Skipping node usage synchronization because this '
                           'marketplace.ResourcePlanPeriod does not exist.'
                           'Cluster ID: %s', cluster.id)


def component_usage_register(component_usage):
    offering_component = component_usage.component

    plan_period = component_usage.plan_period
    if not plan_period:
        logger.warning('Skipping processing of component usage with ID %s because '
                       'plan period is not defined.', component_usage.id)
        return
    plan = plan_period.plan

    try:
        plan_component = plan.components.get(component=offering_component)
        item = invoice_models.InvoiceItem.objects.get(scope=component_usage.resource.scope,
                                                      details__plan_period_id=plan_period.id,
                                                      details__plan_component_id=plan_component.id,
                                                      invoice__year=component_usage.billing_period.year,
                                                      invoice__month=component_usage.billing_period.month)
        item.quantity = component_usage.usage
        item.unit_price = plan_component.price
        item.save()
    except invoice_models.InvoiceItem.DoesNotExist:
        cluster = component_usage.resource.scope
        customer = cluster.customer
        invoice, created = registrators.RegistrationManager.get_or_create_invoice(customer, component_usage.date)
        details = {
            'cluster_id': cluster.id,
            'offering_id': offering_component.offering.id,
            'plan_period_id': plan_period.id,
            'plan_component_id': plan_component.id
        }
        month_start = core_utils.month_start(component_usage.date)
        month_end = core_utils.month_end(component_usage.date)

        start = month_start if not component_usage.plan_period.start else \
            max(component_usage.plan_period.start, month_start)
        end = month_end if not component_usage.plan_period.end else \
            min(component_usage.plan_period.end, month_end)

        invoice_models.InvoiceItem.objects.create(
            content_type=ContentType.objects.get_for_model(cluster),
            object_id=cluster.id,
            project=cluster.project,
            invoice=invoice,
            start=start,
            end=end,
            details=details,
            unit_price=plan_component.price,
            quantity=component_usage.usage,
            unit=common_mixins.UnitPriceMixin.Units.QUANTITY,
            product_code=offering_component.product_code or plan.product_code,
            article_code=offering_component.article_code or plan.article_code,
            name=component_usage.resource.name + '(%s)' % offering_component.offering.name,
        )

    except marketplace_models.PlanComponent.DoesNotExist:
        logger.warning('Plan component for usage component %s is not found.', component_usage.id)
    except invoice_models.InvoiceItem.MultipleObjectsReturned:
        logger.warning('Skipping the invoice item unit price update '
                       'because multiple GenericInvoiceItem objects found. Scope: %s %s, date: %s.',
                       component_usage.resource.content_type,
                       component_usage.resource.object_id,
                       component_usage.date)
