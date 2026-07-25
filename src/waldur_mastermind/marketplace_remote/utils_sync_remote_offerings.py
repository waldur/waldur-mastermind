from __future__ import annotations

import logging
from typing import TYPE_CHECKING

from django.db.models.query import QuerySet
from django.utils import timezone
from httpx import TransportError

# waldur_api_client pulls in a large generated attrs/pydantic model graph
# (~70 MB resident). Its symbols are imported lazily inside the methods below so
# the SDK does not load at Django startup. See the "Lazy imports for heavy
# optional backends" section of CLAUDE.md.
from waldur_core.core.client import get_waldur_client

if TYPE_CHECKING:
    from waldur_api_client.models.public_offering_details import PublicOfferingDetails
from waldur_mastermind.marketplace import models
from waldur_mastermind.marketplace.enums import REMOTE_OFFERING, OfferingStates
from waldur_mastermind.marketplace_remote import models as remote_models
from waldur_mastermind.marketplace_remote import utils

logger = logging.getLogger(__name__)


class RemoteSynchronisationRunner:
    def __init__(self, sync: remote_models.RemoteSynchronisation):
        self.sync: remote_models.RemoteSynchronisation = sync

    def run(self) -> None:
        from waldur_api_client.errors import UnexpectedStatus

        try:
            self._initialize_sync()
            self._process_sync()
            self.sync.state = remote_models.RemoteSynchronisation.States.OK

        except (UnexpectedStatus, TransportError) as e:
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
        from waldur_api_client.api.marketplace_categories import (
            marketplace_categories_list,
        )
        from waldur_api_client.models.marketplace_category_field_enum import (
            MarketplaceCategoryFieldEnum,
        )

        existing_offerings = models.Offering.objects.filter(
            type=REMOTE_OFFERING,
            customer=self.sync.local_service_provider.customer,
            secret_options__customer_uuid=self.sync.remote_organization_uuid.hex,
        )
        processed_offering_ids: set[int] = set()
        client = get_waldur_client(self.sync.api_url, self.sync.token)
        remote_categories = marketplace_categories_list.sync_all(
            client=client,
            field=[
                MarketplaceCategoryFieldEnum.UUID,
                MarketplaceCategoryFieldEnum.TITLE,
            ],
        )

        remote_categories_mapping = {
            item.uuid.hex: item.title for item in remote_categories
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
                self.sync.last_output += f"\tProcessing {remote_offering.name}..."
                local_offering = existing_offerings.filter(
                    backend_id=remote_offering.uuid.hex,
                ).first()

                if local_offering:
                    self._refresh_offering_credentials(local_offering)
                    updated_local_offering = utils.upsert_offering(
                        remote_offering=remote_offering,
                        local_category=category_mapping.local_category,
                        local_offering=local_offering,
                    )
                    self.sync.last_output += f"The offering {updated_local_offering} / {category_mapping.local_category.title} has been updated successfully. \n"
                    logger.info(
                        "The offering %s has been updated successfully.",
                        updated_local_offering,
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

    def _refresh_offering_credentials(self, offering: models.Offering) -> None:
        # Offerings keep their own copy of the remote credentials in
        # secret_options; propagate changes made to the synchronisation
        # settings so that offering-scoped clients don't use stale values.
        expected = {"api_url": self.sync.api_url, "token": self.sync.token}
        updates = {
            key: value
            for key, value in expected.items()
            if offering.secret_options.get(key) != value
        }
        if not updates:
            return
        offering.secret_options.update(updates)
        offering.save(update_fields=["secret_options"])
        message = (
            f"Updated {' and '.join(updates)} in secret options of offering {offering}."
        )
        self.sync.last_output += f"\t{message}\n"
        logger.info(message)

    def _create_new_offering(
        self,
        remote_offering: PublicOfferingDetails,
        local_category: models.Category,
    ) -> models.Offering:
        secret_options = {
            "api_url": self.sync.api_url,
            "token": self.sync.token,
            "customer_uuid": self.sync.remote_organization_uuid.hex,
        }
        local_offering = utils.upsert_offering(
            remote_offering=remote_offering,
            local_customer=self.sync.local_service_provider.customer,
            local_category=local_category,
            secret_options=secret_options,
        )
        self.sync.last_output += f"\t\nCreation of offering {local_offering} / {local_category.title} completed successfully. \n"
        logger.info(
            "Creation of offering %s completed successfully.",
            local_offering,
        )
        return local_offering

    def _handle_sync_error(self, error: Exception) -> None:
        from waldur_api_client.errors import UnexpectedStatus

        if isinstance(error, UnexpectedStatus):
            self.sync.error_message = error.content.decode("utf-8")
        else:
            self.sync.error_message = str(error)
        logger.error("Sync %s failed.", self.sync)

    def _archive_stale_offerings(
        self,
        existing_offerings: QuerySet,
        processed_ids: set[int],
    ) -> None:
        stale_offerings = existing_offerings.exclude(id__in=processed_ids)

        if stale_offerings.exists():
            stale_offerings.update(state=OfferingStates.ARCHIVED)
            for offering in stale_offerings:
                self.sync.last_output += f"The offering {offering} has been archived as it no longer exists in remote. \n"
                logger.info(
                    "The offering %s has been archived as it no longer exists in remote.",
                    offering,
                )
