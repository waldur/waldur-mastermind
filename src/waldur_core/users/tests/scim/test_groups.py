"""Integration tests for the inbound SCIM ``/Groups`` endpoint."""

from constance.test.unittest import override_config
from django.contrib.contenttypes.models import ContentType
from rest_framework import status, test

from waldur_core.permissions.fixtures import CustomerRole, ProjectRole
from waldur_core.permissions.models import CustomerRoleConcealment, UserRole
from waldur_core.permissions.utils import add_user
from waldur_core.structure.models import Customer
from waldur_core.structure.tests import factories as structure_factories
from waldur_core.users.tests.scim.conftest import make_staff_token


@override_config(SCIM_INBOUND_ENABLED=True)
class GroupsEndpointTest(test.APITestCase):
    def setUp(self):
        token_key, self.svc_user = make_staff_token()
        self.client.credentials(
            HTTP_AUTHORIZATION=f"Bearer {token_key}",
            HTTP_ACCEPT="application/scim+json",
        )
        self.customer = structure_factories.CustomerFactory()
        self.project = structure_factories.ProjectFactory(customer=self.customer)
        self.alice = structure_factories.UserFactory(username="alice")
        self.bob = structure_factories.UserFactory(username="bob")
        # Touch the classproperty so the system roles exist before the view
        # tries to resolve them.
        CustomerRole.OWNER  # noqa: B018
        ProjectRole.MANAGER  # noqa: B018

    def _customer_owner_display(self) -> str:
        return f"waldur:customer:{self.customer.uuid.hex}:CUSTOMER.OWNER"

    def _project_manager_display(self) -> str:
        return f"waldur:project:{self.project.uuid.hex}:PROJECT.MANAGER"

    def test_create_group_with_members(self):
        body = {
            "schemas": ["urn:ietf:params:scim:schemas:core:2.0:Group"],
            "displayName": self._customer_owner_display(),
            "members": [
                {"value": self.alice.uuid.hex},
                {"value": self.bob.uuid.hex},
            ],
        }
        response = self.client.post("/scim/v2/Groups", data=body, format="json")
        self.assertEqual(response.status_code, status.HTTP_201_CREATED, response.data)
        self.assertEqual(response.data["displayName"], self._customer_owner_display())
        self.assertEqual(len(response.data["members"]), 2)
        # Check Waldur role assignment was created.
        roles = UserRole.objects.filter(
            user__in=[self.alice, self.bob], role=CustomerRole.OWNER, is_active=True
        )
        self.assertEqual(roles.count(), 2)

    def test_create_group_unknown_role_returns_400(self):
        body = {
            "schemas": ["urn:ietf:params:scim:schemas:core:2.0:Group"],
            "displayName": f"waldur:customer:{self.customer.uuid.hex}:NO.SUCH.ROLE",
            "members": [],
        }
        response = self.client.post("/scim/v2/Groups", data=body, format="json")
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertEqual(response.data["scimType"], "invalidValue")

    def test_create_group_bad_displayname_returns_400(self):
        body = {
            "schemas": ["urn:ietf:params:scim:schemas:core:2.0:Group"],
            "displayName": "not-a-waldur-group",
            "members": [],
        }
        response = self.client.post("/scim/v2/Groups", data=body, format="json")
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertEqual(response.data["scimType"], "invalidValue")

    def test_create_group_unknown_scope_returns_404(self):
        body = {
            "schemas": ["urn:ietf:params:scim:schemas:core:2.0:Group"],
            "displayName": "waldur:customer:00000000000000000000000000000000:CUSTOMER.OWNER",
            "members": [],
        }
        response = self.client.post("/scim/v2/Groups", data=body, format="json")
        self.assertEqual(response.status_code, status.HTTP_404_NOT_FOUND)

    def test_create_group_unknown_member_returns_400(self):
        body = {
            "schemas": ["urn:ietf:params:scim:schemas:core:2.0:Group"],
            "displayName": self._customer_owner_display(),
            "members": [{"value": "ffffffffffffffffffffffffffffffff"}],
        }
        response = self.client.post("/scim/v2/Groups", data=body, format="json")
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)

    def test_get_group_lists_members(self):
        add_user(self.customer, self.alice, CustomerRole.OWNER)
        response = self.client.get(f"/scim/v2/Groups/{self._customer_owner_display()}")
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(len(response.data["members"]), 1)
        self.assertEqual(response.data["members"][0]["value"], self.alice.uuid.hex)

    def test_list_groups_returns_only_active(self):
        add_user(self.project, self.alice, ProjectRole.MANAGER)
        response = self.client.get("/scim/v2/Groups")
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        display_names = {r["displayName"] for r in response.data["Resources"]}
        self.assertIn(self._project_manager_display().lower(), display_names)

    def test_patch_add_member(self):
        add_user(self.customer, self.alice, CustomerRole.OWNER)
        body = {
            "schemas": ["urn:ietf:params:scim:api:messages:2.0:PatchOp"],
            "Operations": [
                {
                    "op": "add",
                    "path": "members",
                    "value": [{"value": self.bob.uuid.hex}],
                }
            ],
        }
        response = self.client.patch(
            f"/scim/v2/Groups/{self._customer_owner_display()}",
            data=body,
            format="json",
        )
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertTrue(
            UserRole.objects.filter(
                user=self.bob, role=CustomerRole.OWNER, is_active=True
            ).exists()
        )

    def test_patch_add_member_skips_concealed_role(self):
        # A role concealed for the organization must not be granted via SCIM,
        # and the rejection must not abort the whole group sync.
        add_user(self.customer, self.alice, CustomerRole.OWNER)
        CustomerRoleConcealment.objects.create(
            role=CustomerRole.OWNER,
            content_type=ContentType.objects.get_for_model(Customer),
            object_id=self.customer.id,
        )
        body = {
            "schemas": ["urn:ietf:params:scim:api:messages:2.0:PatchOp"],
            "Operations": [
                {
                    "op": "add",
                    "path": "members",
                    "value": [{"value": self.bob.uuid.hex}],
                }
            ],
        }
        response = self.client.patch(
            f"/scim/v2/Groups/{self._customer_owner_display()}",
            data=body,
            format="json",
        )
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertFalse(
            UserRole.objects.filter(
                user=self.bob, role=CustomerRole.OWNER, is_active=True
            ).exists()
        )

    def test_patch_remove_member_via_filter(self):
        add_user(self.customer, self.alice, CustomerRole.OWNER)
        body = {
            "schemas": ["urn:ietf:params:scim:api:messages:2.0:PatchOp"],
            "Operations": [
                {
                    "op": "remove",
                    "path": f'members[value eq "{self.alice.uuid.hex}"]',
                }
            ],
        }
        response = self.client.patch(
            f"/scim/v2/Groups/{self._customer_owner_display()}",
            data=body,
            format="json",
        )
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertFalse(
            UserRole.objects.filter(
                user=self.alice, role=CustomerRole.OWNER, is_active=True
            ).exists()
        )

    def test_patch_replace_members(self):
        add_user(self.customer, self.alice, CustomerRole.OWNER)
        body = {
            "schemas": ["urn:ietf:params:scim:api:messages:2.0:PatchOp"],
            "Operations": [
                {
                    "op": "replace",
                    "path": "members",
                    "value": [{"value": self.bob.uuid.hex}],
                }
            ],
        }
        response = self.client.patch(
            f"/scim/v2/Groups/{self._customer_owner_display()}",
            data=body,
            format="json",
        )
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertFalse(
            UserRole.objects.filter(
                user=self.alice, role=CustomerRole.OWNER, is_active=True
            ).exists()
        )
        self.assertTrue(
            UserRole.objects.filter(
                user=self.bob, role=CustomerRole.OWNER, is_active=True
            ).exists()
        )

    def test_delete_group_removes_all_members(self):
        add_user(self.customer, self.alice, CustomerRole.OWNER)
        add_user(self.customer, self.bob, CustomerRole.OWNER)
        response = self.client.delete(
            f"/scim/v2/Groups/{self._customer_owner_display()}"
        )
        self.assertEqual(response.status_code, status.HTTP_204_NO_CONTENT)
        self.assertEqual(
            UserRole.objects.filter(role=CustomerRole.OWNER, is_active=True).count(),
            0,
        )

    def test_filter_displayname_lookup(self):
        add_user(self.customer, self.alice, CustomerRole.OWNER)
        response = self.client.get(
            f'/scim/v2/Groups?filter=displayName eq "{self._customer_owner_display()}"'
        )
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(response.data["totalResults"], 1)
