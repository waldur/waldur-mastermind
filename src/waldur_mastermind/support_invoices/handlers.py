from django.utils import timezone

from waldur_mastermind.invoices import registrators
from waldur_mastermind.support import models as support_models

from . import models


def add_new_offering_to_invoice(sender, instance, created=False, **kwargs):
    if created:
        return

    if instance.tracker.has_changed('state') and instance.state == support_models.Offering.States.OK and \
            models.RequestBasedOffering.is_request_based(instance):
        request_based_offering = models.RequestBasedOffering.objects.get(pk=instance.pk)
        registrators.RegistrationManager.register(request_based_offering, timezone.now())


def terminate_invoice_when_offering_deleted(sender, instance, **kwargs):
    if not models.RequestBasedOffering.is_request_based(instance):
        return

    request_based_offering = models.RequestBasedOffering.objects.get(pk=instance.pk)
    registrators.RegistrationManager.terminate(request_based_offering, timezone.now())


def terminate_invoice_when_offering_cancelled(sender, instance, created=False, **kwargs):
    if created:
        return

    if not models.RequestBasedOffering.is_request_based(instance):
        return

    if instance.tracker.has_changed('state') and (instance.state == support_models.Offering.States.TERMINATED):
        request_based_offering = models.RequestBasedOffering.objects.get(pk=instance.pk)
        registrators.RegistrationManager.terminate(request_based_offering, timezone.now())
