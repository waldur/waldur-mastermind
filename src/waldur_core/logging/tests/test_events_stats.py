from freezegun import freeze_time
from rest_framework import test

from waldur_core.structure.tests import factories as structure_factories

from . import factories


class EventsStatsGetTest(test.APITransactionTestCase):
    def setUp(self) -> None:
        with freeze_time('2021-01-01'):
            self.user = structure_factories.UserFactory(is_staff=True)

        with freeze_time('2021-02-01'):
            self.user2 = structure_factories.UserFactory(is_staff=True)

            event = factories.EventFactory()
            factories.FeedFactory(scope=self.user, event=event)

        self.client.force_login(self.user)
        self.url = factories.EventFactory.get_stats_list_url()

    def test_get_events_stats(self):
        response = self.client.get(
            self.url, {'scope': structure_factories.UserFactory.get_url(self.user)}
        )

        self.assertEqual(200, response.status_code)
        self.assertEqual(2, len(response.data))
        self.assertEqual(
            [
                {'year': 2021, 'month': 2, 'count': 1},
                {'year': 2021, 'month': 1, 'count': 4},
            ],
            response.data,
        )

    def test_events_stats_filter_by_event_type(self):
        response = self.client.get(self.url, {'event_type': 'user_creation_succeeded'})

        self.assertEqual(200, response.status_code)
        self.assertEqual(2, len(response.data))
        self.assertEqual(
            [
                {'year': 2021, 'month': 2, 'count': 1},
                {'year': 2021, 'month': 1, 'count': 1},
            ],
            response.data,
        )

    def test_unauthorized_user_can_not_get_stats(self):
        self.client.logout()

        response = self.client.get(
            self.url, {'scope': structure_factories.UserFactory.get_url(self.user)}
        )

        self.assertEqual(401, response.status_code)
