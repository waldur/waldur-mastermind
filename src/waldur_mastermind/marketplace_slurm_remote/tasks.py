from celery import shared_task

from waldur_mastermind.marketplace import models as marketplace_models
from waldur_mastermind.marketplace_slurm_remote import PLUGIN_NAME, utils


@shared_task(name='waldur_mastermind.marketplace_slurm_remote.sync_offering_users')
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
