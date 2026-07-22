import json
import os
import uuid
from unittest import mock

import httpx
import respx
from rest_framework import test

from waldur_mastermind.marketplace import models as marketplace_models
from waldur_mastermind.marketplace.enums import REMOTE_OFFERING, OfferingStates
from waldur_mastermind.marketplace.tests import factories as marketplace_factories
from waldur_mastermind.marketplace_remote import models, tasks
from waldur_mastermind.marketplace_remote.tests import factories, fixtures


class RemoteOfferingsSyncTest(test.APITestCase):
    def setUp(self):
        self.fixture = fixtures.MarketplaceRemoteFixture()
        self.api_url = self.fixture.remote_synchronisation.api_url

        self.fixture.remote_local_category.remote_category
        current_dir = os.path.dirname(os.path.abspath(__file__))
        self.offering_file_path = os.path.join(current_dir, "offering.json")

        with open(self.offering_file_path, encoding="utf-8") as file:
            self.remote_offering = json.load(file)

        respx.start()
        self.mock_thumbnail()
        self.mock_marketplace_categories()
        mock.patch("waldur_mastermind.marketplace_remote.utils.import_plans").start()

    def tearDown(self):
        respx.stop()
        super().tearDown()
        mock.patch.stopall()

    def mock_marketplace_categories(self):
        respx.get(f"{self.api_url}/api/marketplace-categories/").respond(
            200,
            json=[
                {
                    "title": "Hosting",
                    "uuid": self.fixture.remote_local_category.remote_category.hex,
                },
            ],
        )

    def mock_public_offerings(self, offerings):
        respx.get(f"{self.api_url}/api/marketplace-public-offerings/").respond(
            200, json=offerings
        )

    def mock_offering_categories(self, offerings):
        respx.get(f"{self.api_url}/api/public-offerings-categories/").respond(
            200, json=offerings
        )

    def mock_marketplace_screenshots(self):
        respx.get(f"{self.api_url}/api/marketplace-screenshots/").respond(json=[])

    def mock_thumbnail(self):
        respx.get(self.remote_offering["thumbnail"]).respond(
            200, content=b"test-image-data"
        )

    def _create_local_offering(self, **kwargs):
        params = dict(
            customer=self.fixture.service_provider.customer,
            type=REMOTE_OFFERING,
            backend_id=self.remote_offering["uuid"],
            secret_options={
                "api_url": self.api_url,
                "token": self.fixture.remote_synchronisation.token,
                "customer_uuid": self.fixture.remote_synchronisation.remote_organization_uuid.hex,
            },
            category=self.fixture.remote_local_category.local_category,
        )
        params.update(kwargs)
        offering = marketplace_factories.OfferingFactory(**params)
        return offering

    def test_sync_updates_existing_offerings(self):
        self.mock_marketplace_screenshots()
        self.mock_public_offerings([self.remote_offering])
        offering = self._create_local_offering()
        tasks.remote_offerings_sync()
        offering.refresh_from_db()
        self.fixture.remote_synchronisation.refresh_from_db()
        self.assertEqual(
            self.fixture.remote_synchronisation.state,
            models.RemoteSynchronisation.States.OK,
        )
        self.assertEqual(offering.name, self.remote_offering["name"])

    def test_sync_creates_new_offerings(self):
        # Mock the marketplace-screenshots-list endpoint success response
        self.mock_marketplace_screenshots()
        self.mock_public_offerings([self.remote_offering])

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

    def test_sync_handles_errors(self):
        respx.get(f"{self.api_url}/api/marketplace-public-offerings/").respond(
            500, text="Internal Server Error"
        )

        tasks.remote_offerings_sync()

        self.fixture.remote_synchronisation.refresh_from_db()
        self.assertEqual(
            self.fixture.remote_synchronisation.state,
            models.RemoteSynchronisation.States.ERRED,
        )
        self.assertEqual(
            "Internal Server Error", self.fixture.remote_synchronisation.error_message
        )

    def test_sync_continues_when_screenshots_endpoint_unreachable(self):
        respx.get(f"{self.api_url}/api/marketplace-screenshots/").mock(
            side_effect=httpx.ConnectError("tlsv1 alert internal error")
        )
        self.mock_public_offerings([self.remote_offering])
        offering = self._create_local_offering()

        tasks.remote_offerings_sync()

        offering.refresh_from_db()
        self.fixture.remote_synchronisation.refresh_from_db()
        self.assertEqual(
            self.fixture.remote_synchronisation.state,
            models.RemoteSynchronisation.States.OK,
        )
        self.assertEqual(offering.name, self.remote_offering["name"])

    def test_sync_refreshes_stale_offering_credentials(self):
        self.mock_marketplace_screenshots()
        self.mock_public_offerings([self.remote_offering])
        offering = self._create_local_offering(
            secret_options={
                "api_url": "http://old.example.com",
                "token": "stale-token",
                "customer_uuid": self.fixture.remote_synchronisation.remote_organization_uuid.hex,
            }
        )

        tasks.remote_offerings_sync()

        offering.refresh_from_db()
        self.assertEqual(offering.secret_options["api_url"], self.api_url)
        self.assertEqual(
            offering.secret_options["token"],
            self.fixture.remote_synchronisation.token,
        )
        self.fixture.remote_synchronisation.refresh_from_db()
        self.assertEqual(
            self.fixture.remote_synchronisation.state,
            models.RemoteSynchronisation.States.OK,
        )

    def test_sync_erred_when_remote_unreachable(self):
        respx.get(f"{self.api_url}/api/marketplace-public-offerings/").mock(
            side_effect=httpx.ConnectError("tlsv1 alert internal error")
        )

        tasks.remote_offerings_sync()

        self.fixture.remote_synchronisation.refresh_from_db()
        self.assertEqual(
            self.fixture.remote_synchronisation.state,
            models.RemoteSynchronisation.States.ERRED,
        )
        self.assertIn(
            "tlsv1 alert internal error",
            self.fixture.remote_synchronisation.error_message,
        )

    def test_sync_removes_stale_offerings(self):
        self.mock_marketplace_screenshots()
        self.remote_offering["state"] = "Archived"
        self.mock_public_offerings([self.remote_offering])

        stale_offering = self._create_local_offering()

        tasks.remote_offerings_sync()

        stale_offering.refresh_from_db()
        self.assertEqual(stale_offering.state, OfferingStates.ARCHIVED)

        self.fixture.remote_synchronisation.refresh_from_db()
        self.assertEqual(
            self.fixture.remote_synchronisation.state,
            models.RemoteSynchronisation.States.OK,
        )

    def test_category_of_remote_offering_has_been_changed(self):
        self.mock_public_offerings([])
        self.mock_offering_categories([])
        stale_offering = self._create_local_offering()

        tasks.remote_offerings_sync()

        stale_offering.refresh_from_db()
        self.assertEqual(stale_offering.state, OfferingStates.ARCHIVED)

        self.fixture.remote_synchronisation.refresh_from_db()
        self.assertEqual(
            self.fixture.remote_synchronisation.state,
            models.RemoteSynchronisation.States.OK,
        )

    def test_category_of_sync_has_been_changed(self):
        self.mock_marketplace_screenshots()
        self.mock_public_offerings([self.remote_offering])
        stale_offering = self._create_local_offering(
            state=OfferingStates.ARCHIVED,
            category=marketplace_factories.CategoryFactory(),
        )
        tasks.remote_offerings_sync()

        stale_offering.refresh_from_db()
        self.assertEqual(stale_offering.state, OfferingStates.ACTIVE)
        self.assertEqual(
            stale_offering.category, self.fixture.remote_local_category.local_category
        )

        self.fixture.remote_synchronisation.refresh_from_db()
        self.assertEqual(
            self.fixture.remote_synchronisation.state,
            models.RemoteSynchronisation.States.OK,
        )

    def test_two_service_providers_import_the_same_offerings(self):
        self.mock_marketplace_screenshots()
        self.mock_public_offerings([self.remote_offering])
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
                type=REMOTE_OFFERING,
                secret_options__customer_uuid=remote_organization_uuid.hex,
            ).count(),
            2,
        )

    def test_imports_multiple_offerings_into_same_local_category(self):
        self.mock_marketplace_screenshots()
        with open(self.offering_file_path, encoding="utf-8") as file:
            second_remote_offering = json.load(file)

        second_remote_offering["name"] = "offering_name_2"
        second_remote_offering["uuid"] = uuid.uuid4().hex
        second_remote_offering["category_uuid"] = uuid.uuid4().hex

        self.mock_public_offerings([self.remote_offering, second_remote_offering])

        tasks.remote_offerings_sync()

        self.fixture.remote_synchronisation.refresh_from_db()
        self.assertEqual(
            self.fixture.remote_synchronisation.state,
            models.RemoteSynchronisation.States.OK,
            self.fixture.remote_synchronisation.error_message,
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
