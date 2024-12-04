import logging
from datetime import datetime

from django.db.models.query import QuerySet

from waldur_mastermind.marketplace import models
from waldur_mastermind.marketplace_remote import models as remote_models
from waldur_mastermind.marketplace_remote.constants import (
    OFFERING_FIELDS,
)

from . import PLUGIN_NAME, utils

logger = logging.getLogger(__name__)


def run_synchronisation(sync: remote_models.RemoteSynchronisation) -> None:
    try:
        initialize_sync(sync)
        process_sync(sync)
        sync.state = remote_models.RemoteSynchronisation.States.OK

    except Exception as e:
        handle_sync_error(sync, e)
        sync.state = remote_models.RemoteSynchronisation.States.ERRED

    finally:
        sync.last_execution = datetime.now()
        sync.save()


def initialize_sync(sync: remote_models.RemoteSynchronisation) -> None:
    sync.error_message = ""
    sync.last_output = ""
    sync.state = remote_models.RemoteSynchronisation.States.PROCESSING
    sync.save()


def process_sync(sync: remote_models.RemoteSynchronisation) -> None:
    existing_offerings = models.Offering.objects.filter(
        type=PLUGIN_NAME, customer=sync.local_service_provider.customer
    )
    processed_offering_ids: set[int] = set()

    for category_mapping in sync.remotelocalcategory_set.all():
        remote_offerings = utils.get_remote_offerings(
            sync.api_url,
            sync.token,
            sync.local_service_provider.customer.uuid.hex,
            category_mapping.remote_category,
        )

        for remote_offering in remote_offerings:
            offering_id = process_remote_offering(
                remote_offering, sync, category_mapping
            )
            processed_offering_ids.add(offering_id)

    archive_stale_offerings(existing_offerings, processed_offering_ids, sync)


def process_remote_offering(
    remote_offering: dict,
    sync: remote_models.RemoteSynchronisation,
    category_mapping: remote_models.RemoteLocalCategory,
) -> int:
    local_offering = models.Offering.objects.filter(
        backend_id=remote_offering["uuid"],
        type=PLUGIN_NAME,
    ).first()

    if local_offering:
        update_existing_offering(local_offering, remote_offering, sync)
    else:
        local_offering = create_new_offering(remote_offering, sync, category_mapping)

    return local_offering.id


def update_existing_offering(
    local_offering: models.Offering,
    remote_offering: dict,
    sync: remote_models.RemoteSynchronisation,
) -> None:
    models.Offering.objects.filter(id=local_offering.id).update(
        state=remote_offering["state_code"],
        **{key: remote_offering[key] for key in OFFERING_FIELDS},
    )
    sync.last_output += (
        f"The offering {local_offering} has been updated successfully. \n"
    )
    logger.info(
        "The offering %s has been updated successfully.",
        local_offering,
    )


def create_new_offering(
    remote_offering: dict,
    sync: remote_models.RemoteSynchronisation,
    category_mapping: remote_models.RemoteLocalCategory,
) -> models.Offering:
    secret_options = {
        "api_url": sync.api_url,
        "token": sync.token,
        "customer_uuid": sync.remote_organization_uuid.hex,
    }
    local_offering = utils.import_offering(
        remote_offering,
        sync.local_service_provider.customer,
        category_mapping.local_category,
        secret_options,
    )
    sync.last_output += (
        f"Creation of offering {local_offering} completed successfully. \n"
    )
    logger.info(
        "Creation of offering %s completed successfully.",
        local_offering,
    )
    return local_offering


def archive_stale_offerings(
    existing_offerings: QuerySet,
    processed_ids: set[int],
    sync: remote_models.RemoteSynchronisation,
) -> None:
    stale_offerings = existing_offerings.exclude(id__in=processed_ids)
    if stale_offerings.exists():
        stale_offerings.update(state=models.Offering.States.ARCHIVED)
        for offering in stale_offerings:
            sync.last_output += f"The offering {offering} has been archived as it no longer exists in remote. \n"
            logger.info(
                "The offering %s has been archived as it no longer exists in remote.",
                offering,
            )


def handle_sync_error(
    sync: remote_models.RemoteSynchronisation, error: Exception
) -> None:
    sync.error_message = str(error)
    logger.error(
        "Sync %s failed.",
        sync,
    )
