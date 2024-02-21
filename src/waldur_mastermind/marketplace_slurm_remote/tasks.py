import datetime

from celery import shared_task
from django.utils import timezone

from waldur_mastermind.marketplace import models as marketplace_models
from waldur_mastermind.marketplace import utils
from waldur_mastermind.marketplace_slurm_remote import PLUGIN_NAME


@shared_task(name="waldur_mastermind.marketplace_slurm_remote.sync_offering_users")
def sync_offering_users():
    offerings = marketplace_models.Offering.objects.filter(
        type=PLUGIN_NAME,
        state__in=[
            marketplace_models.Offering.States.ACTIVE,
            marketplace_models.Offering.States.PAUSED,
        ],
        secret_options__service_provider_can_create_offering_user=True,
    ).exclude(
        plugin_options__username_generation_policy=utils.UsernameGenerationPolicy.SERVICE_PROVIDER.value
    )

    utils.user_offerings_mapping(offerings)


@shared_task(
    name="waldur_mastermind.marketplace_slurm_remote.mark_offering_backend_as_disconnected_after_timeout"
)
def mark_offering_backend_as_disconnected_after_timeout():
    one_hour_ago = timezone.now() - datetime.timedelta(hours=1)
    integration_statuses = marketplace_models.IntegrationStatus.objects.filter(
        status=marketplace_models.IntegrationStatus.States.ACTIVE,
        offering__type=PLUGIN_NAME,
        last_request_timestamp__lt=one_hour_ago,
    )
    for integration_status in integration_statuses:
        integration_status.set_backend_disconnected()
        integration_status.save(update_fields=["status"])
