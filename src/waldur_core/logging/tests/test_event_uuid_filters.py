"""`?project_uuid=` and `?customer_uuid=` find the same events as `?scope=`.

An event belongs to a project when its context names the project or when it
is filed on the project's feed. Some events are only filed: a cost policy's
actions and `role_granted` carry no `project_uuid` in their context.
"""

from rest_framework import status, test

from waldur_core.logging.tests import factories
from waldur_core.structure.tests import fixtures


class EventUuidFilterTest(test.APITestCase):
    def setUp(self):
        self.fixture = fixtures.ProjectFixture()
        self.project = self.fixture.project
        self.customer = self.fixture.customer
        self.other = fixtures.ProjectFixture()
        self.url = factories.EventFactory.get_list_url()

    def _event(self, context=None, feeds=()):
        event = factories.EventFactory(context=context or {})
        for scope in feeds:
            factories.FeedFactory(event=event, scope=scope)
        return event

    def _list(self, user, **params):
        self.client.force_authenticate(user)
        response = self.client.get(self.url, {"page_size": 100, **params})
        self.assertEqual(response.status_code, status.HTTP_200_OK, response.data)
        return [row["uuid"] for row in response.data]

    def test_project_uuid_matches_events_filed_on_the_project_feed(self):
        named = self._event({"project_uuid": self.project.uuid.hex})
        filed = self._event(feeds=[self.project, self.customer])
        both = self._event({"project_uuid": self.project.uuid.hex}, [self.project])
        elsewhere = self._event(
            {"project_uuid": self.other.project.uuid.hex}, [self.other.project]
        )

        uuids = self._list(self.fixture.staff, project_uuid=self.project.uuid.hex)

        # The fixture's own events (creation, role grants) are listed too.
        for event in (named, filed, both):
            self.assertEqual(uuids.count(event.uuid.hex), 1)
        self.assertNotIn(elsewhere.uuid.hex, uuids)

    def test_customer_uuid_matches_events_filed_on_the_customer_feed(self):
        named = self._event({"customer_uuid": self.customer.uuid.hex})
        filed = self._event(feeds=[self.customer])
        elsewhere = self._event(feeds=[self.other.customer])

        uuids = self._list(self.fixture.staff, customer_uuid=self.customer.uuid.hex)

        for event in (named, filed):
            self.assertEqual(uuids.count(event.uuid.hex), 1)
        self.assertNotIn(elsewhere.uuid.hex, uuids)

    def test_events_of_a_removed_project_stay_listed(self):
        filed = self._event(feeds=[self.project])
        self.project.delete()

        uuids = self._list(self.fixture.staff, project_uuid=self.project.uuid.hex)

        self.assertIn(filed.uuid.hex, uuids)

    def test_non_staff_still_need_a_scope(self):
        # The UUID filters do not widen what a project role may read: without
        # `scope`, the listing stays empty for non-staff users.
        self._event(feeds=[self.project])

        uuids = self._list(self.fixture.admin, project_uuid=self.project.uuid.hex)

        self.assertEqual(uuids, [])
