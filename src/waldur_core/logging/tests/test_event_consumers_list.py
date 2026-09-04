"""Listing and filtering of EventConsumer records."""

from rest_framework import status, test

from waldur_core.logging.tests import factories as logging_factories
from waldur_core.structure.tests import factories as structure_factories

URL = "/api/event-consumers/"


class EventConsumerListTest(test.APITestCase):
    def setUp(self):
        self.staff = structure_factories.UserFactory(is_staff=True)
        self.owner = structure_factories.UserFactory(
            username="consumer-owner", first_name="Consumer", last_name="Owner"
        )
        self.customer = structure_factories.CustomerFactory()

        self.global_consumer = logging_factories.EventConsumerFactory.with_scopes(
            user=self.staff, object_types=["user_role"]
        )
        self.scoped_consumer = logging_factories.EventConsumerFactory.with_scopes(
            self.customer, user=self.owner, object_types=["resource", "order"]
        )

    def test_list_exposes_the_owner_of_each_consumer(self):
        self.client.force_authenticate(self.staff)

        response = self.client.get(URL)

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        rows = {row["uuid"]: row for row in response.json()}
        scoped = rows[self.scoped_consumer.uuid.hex]
        self.assertEqual(scoped["user_uuid"], self.owner.uuid.hex)
        self.assertEqual(scoped["user_username"], "consumer-owner")
        self.assertEqual(scoped["user_full_name"], self.owner.full_name)
        self.assertEqual(scoped["object_types"], ["resource", "order"])

    def test_consumers_are_filtered_by_is_global(self):
        self.client.force_authenticate(self.staff)

        response = self.client.get(URL, {"is_global": True})
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(
            [row["uuid"] for row in response.json()],
            [self.global_consumer.uuid.hex],
        )

        response = self.client.get(URL, {"is_global": False})
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(
            [row["uuid"] for row in response.json()],
            [self.scoped_consumer.uuid.hex],
        )

    def test_consumers_are_filtered_by_owner(self):
        self.client.force_authenticate(self.staff)

        response = self.client.get(URL, {"user_uuid": self.owner.uuid.hex})

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(
            [row["uuid"] for row in response.json()],
            [self.scoped_consumer.uuid.hex],
        )

    def test_malformed_owner_uuid_is_rejected(self):
        """A truncated UUID must not read as "no consumers"."""
        self.client.force_authenticate(self.staff)

        response = self.client.get(URL, {"user_uuid": "not-a-uuid"})

        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
