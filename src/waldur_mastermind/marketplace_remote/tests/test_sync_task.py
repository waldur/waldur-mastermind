import json
import os
from unittest import mock

from rest_framework import test

from waldur_mastermind.marketplace import models as marketplace_models
from waldur_mastermind.marketplace.tests import factories as marketplace_factories
from waldur_mastermind.marketplace_remote import (
    PLUGIN_NAME,
    models,
    tasks,
)

from . import fixtures


@mock.patch("waldur_mastermind.marketplace_remote.utils.get_remote_offerings")
class RemoteOfferingsSyncTest(test.APITransactionTestCase):
    def setUp(self):
        self.fixture = fixtures.MarketplaceRemoteFixture()
        self.customer = self.fixture.customer

        current_dir = os.path.dirname(os.path.abspath(__file__))
        file_path = os.path.join(current_dir, "offering.json")

        with open(file_path, encoding="utf-8") as file:
            self.remote_offering = json.load(file)

        mock.patch(
            "waldur_mastermind.marketplace_remote.utils.import_offering_thumbnail"
        ).start()
        mock.patch(
            "waldur_mastermind.marketplace_remote.utils.import_offering_components"
        ).start()
        mock.patch("waldur_mastermind.marketplace_remote.utils.import_plans").start()

    def tearDown(self):
        super().tearDown()
        mock.patch.stopall()

    def test_sync_updates_existing_offerings(self, mock_get_remote_offerings):
        mock_get_remote_offerings.return_value = [self.remote_offering]
        offering = marketplace_factories.OfferingFactory(
            customer=self.fixture.customer,
            type=PLUGIN_NAME,
            name="Old name",
            backend_id=self.remote_offering["uuid"],
        )
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
        mock_get_remote_offerings.return_value = []

        stale_offering = marketplace_factories.OfferingFactory(
            customer=self.fixture.service_provider.customer,
            type=PLUGIN_NAME,
            backend_id="stale-uuid",
            state=marketplace_models.Offering.States.ACTIVE,
        )

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
