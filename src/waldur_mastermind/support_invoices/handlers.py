import datetime

from django.utils import timezone

from waldur_core.core import utils as core_utils
from waldur_mastermind.invoices import registrators
from waldur_mastermind.marketplace import models as marketplace_models
from waldur_mastermind.support import models as support_models

from .utils import component_usage_register

OrderTypes = marketplace_models.OrderItem.Types


def terminate_invoice_when_offering_deleted(sender, instance, **kwargs):

    request_based_offering = support_models.Offering.objects.get(pk=instance.pk)

    if request_based_offering.state == support_models.Offering.States.TERMINATED:
        return

    registrators.RegistrationManager.terminate(request_based_offering, timezone.now())
    return


def terminate_invoice_when_offering_cancelled(
    sender, instance, created=False, **kwargs
):
    if created:
        return

    if instance.tracker.has_changed('state') and (
        instance.state == support_models.Offering.States.TERMINATED
    ):
        request_based_offering = support_models.Offering.objects.get(pk=instance.pk)
        registrators.RegistrationManager.terminate(
            request_based_offering, timezone.now()
        )


def switch_plan_resource(sender, instance, created=False, **kwargs):
    if created:
        return

    resource = instance
    support_offering = resource.scope

    if not isinstance(support_offering, support_models.Offering):
        return

    if not resource.tracker.has_changed('plan_id'):
        return

    registrators.RegistrationManager.terminate(support_offering, timezone.now())

    if resource.plan.scope:
        support_offering.plan = resource.plan.scope
        support_offering.save(update_fields=['plan'])

    support_offering.unit_price = instance.plan.unit_price
    support_offering.unit = instance.plan.unit
    support_offering.save()

    registrators.RegistrationManager.register(
        support_offering, timezone.now(), order_type=OrderTypes.UPDATE
    )


def add_new_offering_details_to_invoice(sender, instance, created=False, **kwargs):
    state = instance.state

    if (
        state == support_models.Offering.States.OK
        and support_models.Offering.States.REQUESTED
        == instance.tracker.previous('state')
    ):
        registrators.RegistrationManager.register(
            instance, timezone.now(), order_type=OrderTypes.CREATE
        )
    if (
        state == support_models.Offering.States.TERMINATED
        and support_models.Offering.States.OK == instance.tracker.previous('state')
    ):
        registrators.RegistrationManager.terminate(instance, timezone.now())


def add_component_usage(sender, instance, created=False, **kwargs):
    component_usage = instance

    if not created and not component_usage.tracker.has_changed('usage'):
        return

    if not isinstance(component_usage.resource.scope, support_models.Offering):
        return

    component_usage_register(component_usage)


def create_recurring_usage_if_invoice_has_been_created(
    sender, instance, created=False, **kwargs
):
    if not created:
        return

    invoice = instance

    now = timezone.now()
    prev_month = (now.replace(day=1) - datetime.timedelta(days=1)).date()
    prev_month_start = prev_month.replace(day=1)
    usages = marketplace_models.ComponentUsage.objects.filter(
        resource__project__customer=invoice.customer,
        recurring=True,
        billing_period__gte=prev_month_start,
    )

    if not usages:
        return

    for usage in usages:
        marketplace_models.ComponentUsage.objects.create(
            resource=usage.resource,
            component=usage.component,
            usage=usage.usage,
            description=usage.description,
            date=now,
            plan_period=usage.plan_period,
            recurring=usage.recurring,
            billing_period=core_utils.month_start(now),
        )
