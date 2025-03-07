import logging

from django.db.models.query import QuerySet
from django.utils import timezone
from waldur_client import WaldurClient

from waldur_mastermind.marketplace import models
from waldur_mastermind.marketplace_remote import models as remote_models
from waldur_mastermind.marketplace_remote.constants import (
    OFFERING_FIELDS,
)

from . import PLUGIN_NAME, utils

logger = logging.getLogger(__name__)


class RemoteSynchronisationRunner:
    def __init__(self, sync: remote_models.RemoteSynchronisation):
        self.sync: remote_models.RemoteSynchronisation = sync

    def run(self) -> None:
        try:
            self._initialize_sync()
            self._process_sync()
            self.sync.state = remote_models.RemoteSynchronisation.States.OK

        except Exception as e:
            self._handle_sync_error(e)
            self.sync.state = remote_models.RemoteSynchronisation.States.ERRED

        finally:
            self.sync.last_execution = timezone.now()
            self.sync.save()

    def _initialize_sync(self) -> None:
        self.sync.error_message = ""
        self.sync.last_output = ""
        self.sync.state = remote_models.RemoteSynchronisation.States.PROCESSING
        self.sync.save()

    def _process_sync(self) -> None:
        existing_offerings = models.Offering.objects.filter(
            type=PLUGIN_NAME,
            customer=self.sync.local_service_provider.customer,
            secret_options__customer_uuid=self.sync.remote_organization_uuid.hex,
        )
        processed_offering_ids: set[int] = set()

        client = WaldurClient(self.sync.api_url, self.sync.token)

        remote_categories = utils.get_remote_categories_names(client)

        remote_categories_mapping = {
            item["uuid"]: item["title"] for item in remote_categories
        }
        mappings_to_update = []

        for category_mapping in self.sync.remotelocalcategory_set.all():
            # check if category name needs an update
            remote_category_name = remote_categories_mapping.get(
                category_mapping.remote_category.hex
            )
            self.sync.last_output += (
                f"Processing remote category {remote_category_name}...\n"
            )

            if not remote_category_name:
                message = f"Category {category_mapping.remote_category.hex} not found in the remote Waldur"
                logger.warning(message)
                self.sync.last_output += f"\n{message}"
                continue
            if remote_category_name != category_mapping.remote_category_name:
                category_mapping.remote_category_name = remote_category_name
                self.sync.last_output += f"\nRemote category name has changed, updating. New name: {remote_category_name}"
                mappings_to_update.append(category_mapping)

            # retrieve remote offerings
            remote_offerings = utils.get_remote_offerings(
                client,
                self.sync.remote_organization_uuid,
                category_uuid=category_mapping.remote_category.hex,
            )

            for remote_offering in remote_offerings:
                self.sync.last_output += f'\tProcessing {remote_offering["name"]}...'
                local_offering = existing_offerings.filter(
                    backend_id=remote_offering["uuid"],
                ).first()

                if local_offering:
                    self._update_existing_offering(
                        local_offering, remote_offering, category_mapping.local_category
                    )
                else:
                    local_offering = self._create_new_offering(
                        remote_offering, category_mapping.local_category
                    )

                processed_offering_ids.add(local_offering.id)

        # bulk update remote category names
        if mappings_to_update:
            self.sync.remotelocalcategory_set.bulk_update(
                mappings_to_update, ["remote_category_name"]
            )

        self._archive_stale_offerings(existing_offerings, processed_offering_ids)

    def _update_existing_offering(
        self,
        local_offering: models.Offering,
        remote_offering: dict,
        local_category: models.Category,
    ) -> None:
        models.Offering.objects.filter(id=local_offering.id).update(
            state=remote_offering["state_code"],
            category=local_category,
            **{key: remote_offering[key] for key in OFFERING_FIELDS},
        )
        self.sync.last_output += f"The offering {local_offering} / {local_category.title} has been updated successfully. \n"
        logger.info(
            "The offering %s has been updated successfully.",
            local_offering,
        )

    def _create_new_offering(
        self,
        remote_offering: dict,
        local_category: models.Category,
    ) -> models.Offering:
        secret_options = {
            "api_url": self.sync.api_url,
            "token": self.sync.token,
            "customer_uuid": self.sync.remote_organization_uuid.hex,
        }
        local_offering = utils.upsert_offering(
            remote_offering,
            self.sync.local_service_provider.customer,
            local_category,
            secret_options,
        )
        self.sync.last_output += f"\t\nCreation of offering {local_offering} / {local_category.title} completed successfully. \n"
        logger.info(
            "Creation of offering %s completed successfully.",
            local_offering,
        )
        return local_offering

    def _handle_sync_error(self, error: Exception) -> None:
        self.sync.error_message = str(error)
        logger.error(
            "Sync %s failed.",
            self.sync,
        )

    def _archive_stale_offerings(
        self,
        existing_offerings: QuerySet,
        processed_ids: set[int],
    ) -> None:
        stale_offerings = existing_offerings.exclude(id__in=processed_ids)

        if stale_offerings.exists():
            stale_offerings.update(state=models.Offering.States.ARCHIVED)
            for offering in stale_offerings:
                self.sync.last_output += f"The offering {offering} has been archived as it no longer exists in remote. \n"
                logger.info(
                    "The offering %s has been archived as it no longer exists in remote.",
                    offering,
                )
