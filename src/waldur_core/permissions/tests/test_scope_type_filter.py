from django.contrib.contenttypes.models import ContentType
from rest_framework import status, test

from waldur_core.permissions import models, utils
from waldur_core.permissions.enums import TYPE_MAP
from waldur_core.permissions.fixtures import GlobalRole
from waldur_core.permissions.tests import factories as permission_factories
from waldur_core.structure.tests import factories, fixtures


class UserPermissionScopeTypeFilterTest(test.APITestCase):
    def setUp(self):
        self.fixture = fixtures.CustomerFixture()
        self.owner = self.fixture.owner
        self.desk = factories.UserFactory()
        utils.grant_global_role(self.desk, GlobalRole.get("GLOBAL.DESK"))
        self.client.force_authenticate(factories.UserFactory(is_staff=True))

    def list_grants(self, scope_type):
        response = self.client.get(
            "/api/user-permissions/",
            {"scope_type": scope_type, "page_size": 100},
        )
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        return {(row["user_username"], row["scope_type"]) for row in response.data}

    def test_public_name_matches_the_site_scope(self):
        self.assertEqual(self.list_grants("global"), {(self.desk.username, "global")})

    def test_public_name_that_is_the_model_name_is_unchanged(self):
        self.assertEqual(
            self.list_grants("customer"), {(self.owner.username, "customer")}
        )

    def test_model_name_still_matches(self):
        self.assertEqual(
            self.list_grants("globalrolescope"), {(self.desk.username, "global")}
        )

    def test_unknown_value_matches_nothing(self):
        self.assertEqual(self.list_grants("nonsense"), set())


class RoleAvailabilityScopeTypeFilterTest(test.APITestCase):
    def setUp(self):
        role = permission_factories.RoleFactory(
            content_type=ContentType.objects.get_by_natural_key(*TYPE_MAP["customer"])
        )
        self.provider_availability = self.make(role, "service_provider")
        self.make(role, "customer")
        self.client.force_authenticate(factories.UserFactory(is_staff=True))

    def make(self, role, scope_type):
        return models.RoleAvailability.objects.create(
            role=role,
            content_type=ContentType.objects.get_by_natural_key(*TYPE_MAP[scope_type]),
            object_id=1,
        )

    def test_public_name_matches_a_model_with_another_name(self):
        response = self.client.get(
            "/api/role-availabilities/", {"scope_type": "service_provider"}
        )
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(
            [row["uuid"] for row in response.data],
            [self.provider_availability.uuid.hex],
        )
