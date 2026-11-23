"""The entities offered when restricting a personal access token.

Each one listed must be one the create endpoint accepts as a binding, and an
entity the user can only see (another organization's offering in the
catalogue, say) must not be listed.
"""

from datetime import timedelta

from django.utils import timezone
from rest_framework import status, test

from waldur_core.configuration.testing import override_config
from waldur_core.core.enums import ReauthenticationAction
from waldur_core.core.tests.helpers import reauthenticated
from waldur_core.permissions.enums import PermissionEnum
from waldur_core.permissions.fixtures import CustomerRole, OfferingRole, ProjectRole
from waldur_core.permissions.tests.factories import RoleFactory
from waldur_core.permissions.tests.pat_utils import use_pat
from waldur_core.structure.tests import factories as structure_factories
from waldur_mastermind.marketplace.tests import factories as marketplace_factories
from waldur_mastermind.marketplace.tests import fixtures as marketplace_fixtures

URL = "/api/personal-access-tokens/available_binding_entities/"
PAT_URL = "/api/personal-access-tokens/"
# Any permission the roles below grant; which one does not matter here.
SCOPE = PermissionEnum.LIST_ORDERS.value


@override_config(PAT_ENABLED=True)
class BindableEntitiesTest(test.APITestCase):
    def setUp(self):
        self.fixture = marketplace_fixtures.MarketplaceFixture()
        for role in (
            CustomerRole.OWNER,
            OfferingRole.MANAGER,
            ProjectRole.ADMIN,
            ProjectRole.MANAGER,
            ProjectRole.MEMBER,
        ):
            role.add_permission(PermissionEnum.LIST_ORDERS)
        self.offering = self.fixture.offering
        # Visible to everyone, bindable by nobody here.
        self.foreign_offering = marketplace_factories.OfferingFactory(shared=True)
        self.foreign_customer = structure_factories.CustomerFactory()

    def entities(self, user, type_key, **params):
        self.client.force_authenticate(user)
        response = self.client.get(URL, {"type": type_key, **params})
        self.assertEqual(response.status_code, status.HTTP_200_OK, response.data)
        return {str(row["uuid"]).replace("-", "") for row in response.data}

    def assert_create_accepts(self, user, type_key, uuids):
        user.can_use_personal_access_tokens = True
        user.save(update_fields=["can_use_personal_access_tokens"])
        self.client.force_authenticate(user)
        for uuid in uuids:
            response = self.client.post(
                PAT_URL,
                {
                    "name": "probe",
                    "scopes": [SCOPE],
                    "expires_at": (timezone.now() + timedelta(days=1)).isoformat(),
                    "allowed_scopes": [{"type": type_key, "uuid": uuid}],
                },
                format="json",
                **reauthenticated(
                    user, ReauthenticationAction.CREATE_PERSONAL_ACCESS_TOKEN
                ),
            )
            self.assertEqual(
                response.status_code,
                status.HTTP_201_CREATED,
                (type_key, uuid, response.data),
            )

    def test_owner_gets_its_organization_and_what_is_under_it(self):
        owner = self.fixture.offering_owner
        customer = self.fixture.offering_customer
        self.assertEqual(self.entities(owner, "customer"), {customer.uuid.hex})
        offerings = self.entities(owner, "offering")
        self.assertIn(self.offering.uuid.hex, offerings)
        self.assertNotIn(self.foreign_offering.uuid.hex, offerings)
        projects = self.entities(owner, "project")
        self.assertIn(self.fixture.offering_project.uuid.hex, projects)
        self.assertNotIn(self.fixture.project.uuid.hex, projects)
        for type_key in ("customer", "offering", "project"):
            self.assert_create_accepts(owner, type_key, self.entities(owner, type_key))

    def test_project_member_gets_its_project_only(self):
        member = self.fixture.member
        self.assertEqual(
            self.entities(member, "project"), {self.fixture.project.uuid.hex}
        )
        self.assertEqual(self.entities(member, "customer"), set())
        self.assertEqual(self.entities(member, "offering"), set())
        # The project's resources are under it.
        self.assertIn(self.fixture.resource.uuid.hex, self.entities(member, "resource"))
        self.assert_create_accepts(
            member, "resource", self.entities(member, "resource")
        )

    def test_offering_manager_gets_the_offering_and_its_resources(self):
        manager = self.fixture.offering_manager
        self.assertEqual(self.entities(manager, "offering"), {self.offering.uuid.hex})
        self.assertIn(
            self.fixture.resource.uuid.hex, self.entities(manager, "resource")
        )
        self.assertEqual(self.entities(manager, "customer"), set())
        self.assert_create_accepts(
            manager, "resource", self.entities(manager, "resource")
        )

    def test_a_role_that_grants_nothing_offers_nothing(self):
        user = structure_factories.UserFactory()
        empty = structure_factories.ProjectFactory()
        role = RoleFactory(content_type=ProjectRole.MEMBER.content_type)
        empty.add_user(user, role)
        self.assertEqual(self.entities(user, "project"), set())

    def test_staff_gets_any_entity(self):
        offerings = self.entities(
            self.fixture.staff, "offering", query=self.foreign_offering.name
        )
        self.assertIn(self.foreign_offering.uuid.hex, offerings)

    def test_query_filters_by_name(self):
        owner = self.fixture.offering_owner
        self.assertEqual(self.entities(owner, "offering", query="no such name"), set())
        self.assertEqual(
            self.entities(owner, "offering", query=self.offering.name),
            {self.offering.uuid.hex},
        )

    def test_unknown_type_is_refused(self):
        self.client.force_authenticate(self.fixture.offering_owner)
        response = self.client.get(URL, {"type": "global"})
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)

    def test_refused_to_a_personal_access_token(self):
        use_pat(self.client, self.fixture.offering_owner, SCOPE)
        response = self.client.get(URL, {"type": "customer"})
        self.assertEqual(response.status_code, status.HTTP_403_FORBIDDEN)
