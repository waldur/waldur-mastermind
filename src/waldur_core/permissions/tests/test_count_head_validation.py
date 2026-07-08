from unittest import mock

from django.db import DEFAULT_DB_ALIAS, connections
from django.test.utils import CaptureQueriesContext
from rest_framework import status, test

from waldur_core.core.pagination import RESULT_COUNT_HEADER
from waldur_core.permissions.fixtures import ProjectRole
from waldur_core.structure.tests import factories as structure_factories


class ListUsersCountHeadTest(test.APITestCase):
    """Runtime validation of GET /projects/{uuid}/list_users/ HEAD (count)."""

    def setUp(self):
        self.staff = structure_factories.UserFactory(is_staff=True)
        self.client.force_authenticate(self.staff)
        self.project = structure_factories.ProjectFactory()
        self.url = f"http://testserver/api/projects/{self.project.uuid}/list_users/"

    def _add_members(self, count):
        for _ in range(count):
            self.project.add_user(structure_factories.UserFactory(), ProjectRole.ADMIN)

    def test_head_returns_count_with_empty_body(self):
        self._add_members(3)

        response = self.client.head(self.url)

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(int(response[RESULT_COUNT_HEADER]), 3)
        # HEAD must carry no body over the wire.
        self.assertEqual(response.content, b"")

    def test_head_query_count_is_independent_of_team_size(self):
        self._add_members(3)
        # Warm up (lazy imports / permission caches).
        self.client.head(self.url)

        def measure():
            with CaptureQueriesContext(connections[DEFAULT_DB_ALIAS]) as ctx:
                response = self.client.head(self.url)
            return len(ctx), int(response[RESULT_COUNT_HEADER])

        q_small, count_small = measure()
        self._add_members(20)
        q_large, count_large = measure()

        self.assertEqual(count_small, 3)
        self.assertEqual(count_large, 23)
        # Work must not scale with the number of rows: the count comes from a
        # COUNT(*), not from fetching/serialising the whole team.
        self.assertEqual(q_small, q_large)

    def test_head_skips_serialisation(self):
        """The count-only HEAD short-circuit must not serialise rows."""
        from waldur_core.permissions import serializers as permission_serializers

        self._add_members(3)
        self.client.head(self.url)  # warm up

        with mock.patch.object(
            permission_serializers.UserRoleDetailsSerializer,
            "to_representation",
        ) as to_repr:
            response = self.client.head(self.url)

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(int(response[RESULT_COUNT_HEADER]), 3)
        to_repr.assert_not_called()

    def test_head_count_respects_query_filters(self):
        """The count must reflect the filtered subset, not the whole team."""
        self._add_members(3)
        target = structure_factories.UserFactory()
        self.project.add_user(target, ProjectRole.ADMIN)

        # Unfiltered: 4 members.
        self.assertEqual(int(self.client.head(self.url)[RESULT_COUNT_HEADER]), 4)
        # Filtered to a single user: count follows the filter.
        response = self.client.head(f"{self.url}?user={target.uuid.hex}")
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(int(response[RESULT_COUNT_HEADER]), 1)
        # And matches what the GET list returns under the same filter.
        get_response = self.client.get(f"{self.url}?user={target.uuid.hex}")
        self.assertEqual(len(get_response.data), 1)


class CustomerUsersCountHeadTest(test.APITestCase):
    """Runtime validation of GET /customers/{uuid}/users/ HEAD (count)."""

    def setUp(self):
        self.support = structure_factories.UserFactory(is_support=True)
        self.client.force_authenticate(self.support)
        self.customer = structure_factories.CustomerFactory()
        self.url = f"http://testserver/api/customers/{self.customer.uuid}/users/"

    def _add_users(self, count):
        from waldur_core.permissions.fixtures import CustomerRole

        for _ in range(count):
            self.customer.add_user(
                structure_factories.UserFactory(), CustomerRole.OWNER
            )

    def test_head_returns_count_with_empty_body(self):
        self._add_users(3)

        response = self.client.head(self.url)

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(int(response[RESULT_COUNT_HEADER]), 3)
        self.assertEqual(response.content, b"")

    def test_head_skips_serialisation(self):
        """The optimised ListModelMixin.list path must not serialise rows."""
        self._add_users(3)

        from waldur_core.structure import serializers as structure_serializers

        with mock.patch.object(
            structure_serializers.CustomerUserSerializer,
            "to_representation",
        ) as to_repr:
            response = self.client.head(self.url)

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(int(response[RESULT_COUNT_HEADER]), 3)
        to_repr.assert_not_called()

    def test_head_count_respects_query_filters(self):
        """The count must reflect the filtered subset and match the GET list."""
        from waldur_core.permissions.fixtures import CustomerRole

        self._add_users(3)
        target = structure_factories.UserFactory(username="zzz_distinct_target")
        self.customer.add_user(target, CustomerRole.OWNER)

        count_all = int(self.client.head(self.url)[RESULT_COUNT_HEADER])

        filtered = f"{self.url}?username=zzz_distinct_target"
        count_filtered = int(self.client.head(filtered)[RESULT_COUNT_HEADER])
        list_filtered = len(self.client.get(filtered).data)

        # The filter narrows the set, and the count matches the GET list exactly.
        self.assertLess(count_filtered, count_all)
        self.assertEqual(count_filtered, 1)
        self.assertEqual(count_filtered, list_filtered)
