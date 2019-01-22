from django.core.exceptions import ObjectDoesNotExist
from django.utils import timezone

from waldur_mastermind.invoices import registrators
from waldur_mastermind.support import models as support_models

from . import models


def add_new_offering_to_invoice(sender, instance, created=False, **kwargs):
    if created:
        return

    if instance.tracker.has_changed('state') and instance.state == support_models.Offering.States.OK and \
            models.RequestBasedOffering.is_request_based(instance):
        try:
            request_based_offering = models.RequestBasedOffering.objects.get(pk=instance.pk)
        except ObjectDoesNotExist:
            return
        registrators.RegistrationManager.register(request_based_offering, timezone.now())


def terminate_invoice_when_offering_deleted(sender, instance, **kwargs):
    if not models.RequestBasedOffering.is_request_based(instance):
        return

    try:
        request_based_offering = models.RequestBasedOffering.objects.get(pk=instance.pk)
    except ObjectDoesNotExist:
        return
    registrators.RegistrationManager.terminate(request_based_offering, timezone.now())


def terminate_invoice_when_offering_cancelled(sender, instance, created=False, **kwargs):
    if created:
        return

    if not models.RequestBasedOffering.is_request_based(instance):
        return

    if instance.tracker.has_changed('state') and (instance.state == support_models.Offering.States.TERMINATED):
        try:
            request_based_offering = models.RequestBasedOffering.objects.get(pk=instance.pk)
        except ObjectDoesNotExist:
            return
        registrators.RegistrationManager.terminate(request_based_offering, timezone.now())


def switch_plan_resource(sender, instance, created=False, **kwargs):
    if created:
        return

    if not models.RequestBasedOffering.is_request_based(instance.scope):
        return

    if not instance.tracker.has_changed('plan_id'):
        return

    try:
        request_based_offering = models.RequestBasedOffering.objects.get(pk=instance.scope.pk)
    except ObjectDoesNotExist:
        return
    registrators.RegistrationManager.terminate(request_based_offering, timezone.now())
    registrators.RegistrationManager.register(request_based_offering, timezone.now())
