from django.utils import timezone

from waldur_mastermind.invoices import registrators
from waldur_mastermind.support import models as support_models

from .utils import is_request_based, component_usage_register


def add_new_offering_to_invoice(sender, instance, created=False, **kwargs):
    if created:
        return

    if instance.tracker.has_changed('state') and instance.state == support_models.Offering.States.OK and \
            is_request_based(instance):
        request_based_offering = support_models.Offering.objects.get(pk=instance.pk)
        registrators.RegistrationManager.register(request_based_offering, timezone.now())


def terminate_invoice_when_offering_deleted(sender, instance, **kwargs):
    if not is_request_based(instance):
        return

    request_based_offering = support_models.Offering.objects.get(pk=instance.pk)
    registrators.RegistrationManager.terminate(request_based_offering, timezone.now())


def terminate_invoice_when_offering_cancelled(sender, instance, created=False, **kwargs):
    if created:
        return

    if not is_request_based(instance):
        return

    if instance.tracker.has_changed('state') and (instance.state == support_models.Offering.States.TERMINATED):
        request_based_offering = support_models.Offering.objects.get(pk=instance.pk)
        registrators.RegistrationManager.terminate(request_based_offering, timezone.now())


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
    registrators.RegistrationManager.register(support_offering, timezone.now())


def update_invoice_on_offering_deletion(sender, instance, **kwargs):
    state = instance.state

    if is_request_based(instance):
        return

    if state == support_models.Offering.States.TERMINATED:
        # no need to terminate offering item if it was already terminated before.
        return

    registrators.RegistrationManager.terminate(instance, timezone.now())


def add_new_offering_details_to_invoice(sender, instance, created=False, **kwargs):
    state = instance.state

    if is_request_based(instance):
        return

    if (state == support_models.Offering.States.OK and
            support_models.Offering.States.REQUESTED == instance.tracker.previous('state')):
        registrators.RegistrationManager.register(instance, timezone.now())
    if (state == support_models.Offering.States.TERMINATED and
            support_models.Offering.States.OK == instance.tracker.previous('state')):
        registrators.RegistrationManager.terminate(instance, timezone.now())


def update_invoice_item(sender, instance, created=False, **kwargs):
    component_usage = instance

    if not created and not component_usage.tracker.has_changed('usage'):
            return

    if not isinstance(component_usage.resource.scope, support_models.Offering):
        return

    component_usage_register(component_usage)
