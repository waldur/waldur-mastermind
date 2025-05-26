from freezegun import freeze_time
from rest_framework import test

from waldur_core.structure.tests import factories as structure_factories
from waldur_mastermind.invoices.utils import get_current_month, get_current_year

from . import factories


class EventsStatsGetTest(test.APITransactionTestCase):
    def setUp(self) -> None:
        with freeze_time("2021-01-01"):
            self.user = structure_factories.UserFactory(is_staff=True)

        with freeze_time("2021-02-01"):
            self.user2 = structure_factories.UserFactory(is_staff=True)

            event = factories.EventFactory()
            factories.FeedFactory(scope=self.user, event=event)

        self.client.force_login(self.user)
        self.url = factories.EventFactory.get_stats_list_url()

    def test_get_events_stats(self):
        response = self.client.get(
            self.url, {"scope": structure_factories.UserFactory.get_url(self.user)}
        )

        self.assertEqual(200, response.status_code)
        self.assertEqual(3, len(response.data))
        self.assertEqual(
            [
                {"year": get_current_year(), "month": get_current_month(), "count": 1},
                {"year": 2021, "month": 2, "count": 1},
                {"year": 2021, "month": 1, "count": 2},
            ],
            response.data,
        )

    def test_events_stats_filter_by_event_type(self):
        response = self.client.get(self.url, {"event_type": "user_creation_succeeded"})

        self.assertEqual(200, response.status_code)
        self.assertEqual(2, len(response.data))
        self.assertEqual(
            [
                {"year": 2021, "month": 2, "count": 1},
                {"year": 2021, "month": 1, "count": 1},
            ],
            response.data,
        )

    def test_unauthorized_user_can_not_get_stats(self):
        self.client.logout()

        response = self.client.get(
            self.url, {"scope": structure_factories.UserFactory.get_url(self.user)}
        )

        self.assertEqual(401, response.status_code)


class EventsFilteringTest(test.APITransactionTestCase):
    def setUp(self):
        self.user = structure_factories.UserFactory(is_staff=True)
        self.client.force_authenticate(user=self.user)
        self.url = factories.EventFactory.get_list_url()

        # Create events for different event groups
        self.components_event = factories.EventFactory(
            event_type="marketplace_plan_created"
        )
        self.resources_event = factories.EventFactory(
            event_type="marketplace_resource_create_succeeded"
        )

    def test_filter_events_by_feature(self):
        """
        Test filtering events by feature.
        """
        response = self.client.get(self.url, {"feature": "offering_accounting"})
        self.assertEqual(200, response.status_code)
        self.assertEqual(len(response.data), 1)
        self.assertEqual(response.data[0]["event_type"], "marketplace_plan_created")

    def test_filter_events_by_multiple_features(self):
        """
        Test filtering events by multiple features.
        """
        response = self.client.get(
            self.url, {"feature": ["offering_accounting", "resources"]}
        )
        self.assertEqual(200, response.status_code)
        self.assertEqual(len(response.data), 2)
        event_types = [event["event_type"] for event in response.data]
        self.assertEqual(
            sorted(event_types),
            sorted(
                [
                    "marketplace_plan_created",
                    "marketplace_resource_create_succeeded",
                ]
            ),
        )

    def test_filter_events_by_nonexistent_feature(self):
        """
        Test filtering events by nonexistent feature.
        """
        response = self.client.get(self.url, {"feature": "whatever"})
        self.assertEqual(200, response.status_code)
        self.assertEqual(len(response.data), 0)
