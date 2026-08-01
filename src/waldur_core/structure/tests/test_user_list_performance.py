from django.db import connection
from django.test import utils as django_test
from rest_framework import status, test

from waldur_core.permissions.fixtures import CustomerRole, ProjectRole
from waldur_core.structure.tests import factories, fixtures


class UserListPermissionScopeTest(test.APITestCase):
    """Permission scopes must be resolved once per page, not once per user.

    Scope loading was already batched inside _serialize_permissions, but only
    across one user's own permissions, so the cost still grew with page size.
    """

    def setUp(self):
        self.fixture = fixtures.ProjectFixture()
        self.staff = factories.UserFactory(is_staff=True)
        self.client.force_authenticate(self.staff)
        self.url = factories.UserFactory.get_list_url()

    def _add_users(self, count):
        for _i in range(count):
            user = factories.UserFactory()
            # Two scope types per user, so both branches of the scope
            # resolution are exercised.
            self.fixture.project.add_user(user, ProjectRole.ADMIN)
            self.fixture.customer.add_user(user, CustomerRole.OWNER)

    def _get(self):
        response = self.client.get(self.url)
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        return response

    def test_query_count_does_not_grow_with_page_size(self):
        self._add_users(2)
        self._get()  # warm ContentType and permission caches

        with django_test.CaptureQueriesContext(connection) as baseline:
            self._get()

        self._add_users(6)

        with self.assertNumQueries(len(baseline)):
            self._get()

    def test_permissions_payload_is_unchanged(self):
        self._add_users(2)
        response = self._get()

        with_permissions = [row for row in response.data if row.get("permissions")]
        self.assertEqual(len(with_permissions), 2)
        for row in with_permissions:
            scopes = {p["scope_type"] for p in row["permissions"]}
            self.assertEqual(scopes, {"project", "customer"})
            for permission in row["permissions"]:
                self.assertIsNotNone(permission["scope_uuid"])

    def test_detail_view_still_resolves_scopes(self):
        """The page cache must not be required - detail has no page."""
        self._add_users(1)
        user = self.fixture.project.get_users().first()

        response = self.client.get(factories.UserFactory.get_url(user))

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertTrue(response.data["permissions"])
        self.assertIsNotNone(response.data["permissions"][0]["scope_uuid"])
