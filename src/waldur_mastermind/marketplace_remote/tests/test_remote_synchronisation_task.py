import json
import os
import uuid
from unittest import mock

from rest_framework import test

from waldur_mastermind.marketplace import models as marketplace_models
from waldur_mastermind.marketplace.tests import factories as marketplace_factories
from waldur_mastermind.marketplace_remote import (
    PLUGIN_NAME,
    models,
    tasks,
)

from . import factories, fixtures


@mock.patch("waldur_mastermind.marketplace_remote.utils.get_remote_offerings")
class RemoteOfferingsSyncTest(test.APITransactionTestCase):
    def setUp(self):
        self.fixture = fixtures.MarketplaceRemoteFixture()

        self.fixture.remote_local_category.remote_category
        current_dir = os.path.dirname(os.path.abspath(__file__))
        self.offering_file_path = os.path.join(current_dir, "offering.json")

        with open(self.offering_file_path, encoding="utf-8") as file:
            self.remote_offering = json.load(file)

        self.remote_categories = [
            {
                "title": "Hosting",
                "uuid": self.fixture.remote_local_category.remote_category.hex,
            },
        ]

        mock.patch(
            "waldur_mastermind.marketplace_remote.utils.import_offering_thumbnail"
        ).start()
        mock.patch(
            "waldur_mastermind.marketplace_remote.utils.import_offering_thumbnail"
        ).start()
        self.remote_category_mock = mock.patch(
            "waldur_mastermind.marketplace_remote.utils.get_remote_categories_names"
        ).start()
        self.remote_category_mock.return_value = self.remote_categories

        mock.patch("waldur_mastermind.marketplace_remote.utils.import_plans").start()

    def tearDown(self):
        super().tearDown()
        mock.patch.stopall()

    def _create_local_offering(self, **kwargs):
        params = dict(
            customer=self.fixture.service_provider.customer,
            type=PLUGIN_NAME,
            backend_id=self.remote_offering["uuid"],
            secret_options={
                "api_url": self.fixture.remote_synchronisation.api_url,
                "token": self.fixture.remote_synchronisation.token,
                "customer_uuid": self.fixture.remote_synchronisation.remote_organization_uuid.hex,
            },
            category=self.fixture.remote_local_category.local_category,
        )
        params.update(kwargs)
        offering = marketplace_factories.OfferingFactory(**params)
        return offering

    def test_sync_updates_existing_offerings(self, mock_get_remote_offerings):
        mock_get_remote_offerings.return_value = [self.remote_offering]
        offering = self._create_local_offering()
        tasks.remote_offerings_sync()
        offering.refresh_from_db()
        self.fixture.remote_synchronisation.refresh_from_db()
        self.assertEqual(
            self.fixture.remote_synchronisation.state,
            models.RemoteSynchronisation.States.OK,
        )
        self.assertEqual(offering.name, self.remote_offering["name"])

    def test_sync_creates_new_offerings(self, mock_get_remote_offerings):
        mock_get_remote_offerings.return_value = [self.remote_offering]

        tasks.remote_offerings_sync()

        self.fixture.remote_synchronisation.refresh_from_db()
        self.assertEqual(
            self.fixture.remote_synchronisation.state,
            models.RemoteSynchronisation.States.OK,
        )
        self.assertTrue(
            marketplace_models.Offering.objects.filter(
                backend_id=self.remote_offering["uuid"]
            ).exists()
        )

    def test_sync_handles_errors(self, mock_get_remote_offerings):
        mock_get_remote_offerings.side_effect = Exception("Test error")

        tasks.remote_offerings_sync()

        self.fixture.remote_synchronisation.refresh_from_db()
        self.assertEqual(
            self.fixture.remote_synchronisation.state,
            models.RemoteSynchronisation.States.ERRED,
        )
        self.assertEqual(
            "Test error", self.fixture.remote_synchronisation.error_message
        )

    def test_sync_removes_stale_offerings(self, mock_get_remote_offerings):
        mock_get_remote_offerings.return_value = [self.remote_offering]
        self.remote_offering["state"] = "Archived"
        self.remote_offering["state_code"] = marketplace_models.Offering.States.ARCHIVED

        stale_offering = self._create_local_offering()

        tasks.remote_offerings_sync()

        stale_offering.refresh_from_db()
        self.assertEqual(
            stale_offering.state, marketplace_models.Offering.States.ARCHIVED
        )

        self.fixture.remote_synchronisation.refresh_from_db()
        self.assertEqual(
            self.fixture.remote_synchronisation.state,
            models.RemoteSynchronisation.States.OK,
        )

    def test_category_of_remote_offering_has_been_changed(
        self, mock_get_remote_offerings
    ):
        mock_get_remote_offerings.return_value = []
        stale_offering = self._create_local_offering()

        tasks.remote_offerings_sync()

        stale_offering.refresh_from_db()
        self.assertEqual(
            stale_offering.state, marketplace_models.Offering.States.ARCHIVED
        )

        self.fixture.remote_synchronisation.refresh_from_db()
        self.assertEqual(
            self.fixture.remote_synchronisation.state,
            models.RemoteSynchronisation.States.OK,
        )

    def test_category_of_sync_has_been_changed(self, mock_get_remote_offerings):
        mock_get_remote_offerings.return_value = [self.remote_offering]
        stale_offering = self._create_local_offering(
            state=marketplace_models.Offering.States.ARCHIVED,
            category=marketplace_factories.CategoryFactory(),
        )

        tasks.remote_offerings_sync()

        stale_offering.refresh_from_db()
        self.assertEqual(
            stale_offering.state, marketplace_models.Offering.States.ACTIVE
        )
        self.assertEqual(
            stale_offering.category, self.fixture.remote_local_category.local_category
        )

        self.fixture.remote_synchronisation.refresh_from_db()
        self.assertEqual(
            self.fixture.remote_synchronisation.state,
            models.RemoteSynchronisation.States.OK,
        )

    def test_two_service_providers_import_the_same_offerings(
        self, mock_get_remote_offerings
    ):
        mock_get_remote_offerings.return_value = [self.remote_offering]
        remote_organization_uuid = (
            self.fixture.remote_synchronisation.remote_organization_uuid
        )
        second_remote_synchronisation = factories.RemoteSynchronisationFactory(
            remote_organization_uuid=remote_organization_uuid,
        )
        factories.RemoteLocalCategoryFactory(
            remote_synchronisation=second_remote_synchronisation,
            remote_category=self.fixture.remote_local_category.remote_category,
        )

        tasks.remote_offerings_sync()
        self.fixture.remote_synchronisation.refresh_from_db()
        self.assertEqual(
            self.fixture.remote_synchronisation.state,
            models.RemoteSynchronisation.States.OK,
        )

        self.assertEqual(
            marketplace_models.Offering.objects.filter(
                type=PLUGIN_NAME,
                secret_options__customer_uuid=remote_organization_uuid.hex,
            ).count(),
            2,
        )

    def test_imports_multiple_offerings_into_same_local_category(
        self, mock_get_remote_offerings
    ):
        with open(self.offering_file_path, encoding="utf-8") as file:
            second_remote_offering = json.load(file)

        second_remote_offering["name"] = "offering_name_2"
        second_remote_offering["uuid"] = uuid.uuid4().hex
        second_remote_offering["category_uuid"] = uuid.uuid4().hex

        mock_get_remote_offerings.return_value = [
            self.remote_offering,
            second_remote_offering,
        ]

        tasks.remote_offerings_sync()

        self.fixture.remote_synchronisation.refresh_from_db()
        self.assertEqual(
            self.fixture.remote_synchronisation.state,
            models.RemoteSynchronisation.States.OK,
        )
        self.assertTrue(
            marketplace_models.Offering.objects.filter(
                backend_id=self.remote_offering["uuid"]
            ).exists()
        )
        self.assertTrue(
            marketplace_models.Offering.objects.filter(
                backend_id=second_remote_offering["uuid"]
            ).exists()
        )

        local_category = self.fixture.remote_local_category.local_category
        self.assertTrue(
            marketplace_models.Offering.objects.filter(
                backend_id=self.remote_offering["uuid"], category=local_category
            ).exists()
        )
        self.assertTrue(
            marketplace_models.Offering.objects.filter(
                backend_id=second_remote_offering["uuid"], category=local_category
            ).exists()
        )
