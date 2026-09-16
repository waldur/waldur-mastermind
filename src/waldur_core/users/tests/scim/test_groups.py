"""Integration tests for the inbound SCIM ``/Groups`` endpoint."""

from datetime import timedelta

from constance.test.unittest import override_config
from django.contrib.contenttypes.models import ContentType
from django.utils import timezone
from rest_framework import status, test
from rest_framework.authtoken.models import Token

from waldur_core.permissions.fixtures import CustomerRole, ProjectRole
from waldur_core.permissions.models import CustomerRoleConcealment, UserRole
from waldur_core.permissions.utils import add_user
from waldur_core.structure.models import Customer
from waldur_core.structure.tests import factories as structure_factories
from waldur_core.users.tests.scim.conftest import make_staff_token


@override_config(SCIM_INBOUND_ENABLED=True)
class GroupsEndpointAuthTest(test.APITestCase):
    def test_expired_token_returns_401(self):
        token_key, user = make_staff_token()
        user.token_lifetime = 3600
        user.save(update_fields=["token_lifetime"])
        Token.objects.filter(key=token_key).update(
            created=timezone.now() - timedelta(hours=2)
        )
        self.client.credentials(HTTP_AUTHORIZATION=f"Bearer {token_key}")
        response = self.client.get("/scim/v2/Groups")
        self.assertEqual(response.status_code, status.HTTP_401_UNAUTHORIZED)
        self.assertTrue(Token.objects.filter(key=token_key).exists())


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


@override_config(SCIM_INBOUND_ENABLED=True)
class GroupsInactiveRoleTest(test.APITestCase):
    """A deactivated role must not be reachable through group provisioning.

    Deactivation is how an administrator takes a role out of circulation, and
    every other grant path honours it. It gates *new grants* only, so reading a
    group, removing members and deleting it stay available — otherwise an
    identity provider could no longer deprovision the very role that was just
    disabled.
    """

    def setUp(self):
        token_key, self.svc_user = make_staff_token()
        self.client.credentials(
            HTTP_AUTHORIZATION=f"Bearer {token_key}",
            HTTP_ACCEPT="application/scim+json",
        )
        self.customer = structure_factories.CustomerFactory()
        self.alice = structure_factories.UserFactory(username="alice")
        self.bob = structure_factories.UserFactory(username="bob")
        CustomerRole.OWNER  # noqa: B018

    def _display(self) -> str:
        return f"waldur:customer:{self.customer.uuid.hex}:CUSTOMER.OWNER"

    def _deactivate_role(self):
        role = CustomerRole.OWNER
        role.is_active = False
        role.save(update_fields=["is_active"])
        return role

    def _holds_owner(self, user) -> bool:
        return UserRole.objects.filter(
            user=user, role=CustomerRole.OWNER, is_active=True
        ).exists()

    def _patch_add(self, user):
        return self.client.patch(
            f"/scim/v2/Groups/{self._display()}",
            data={
                "schemas": ["urn:ietf:params:scim:api:messages:2.0:PatchOp"],
                "Operations": [
                    {
                        "op": "add",
                        "path": "members",
                        "value": [{"value": user.uuid.hex}],
                    }
                ],
            },
            format="json",
        )

    def test_create_group_with_inactive_role_returns_400(self):
        self._deactivate_role()
        response = self.client.post(
            "/scim/v2/Groups",
            data={
                "schemas": ["urn:ietf:params:scim:schemas:core:2.0:Group"],
                "displayName": self._display(),
                "members": [{"value": self.alice.uuid.hex}],
            },
            format="json",
        )
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        # The reason has to name the role, or an operator reading the identity
        # provider's log cannot tell which of its groups is misconfigured.
        self.assertIn("CUSTOMER.OWNER", str(response.data))
        self.assertFalse(self._holds_owner(self.alice))

    def test_patch_add_member_with_inactive_role_returns_400(self):
        self._deactivate_role()
        response = self._patch_add(self.alice)
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertIn("CUSTOMER.OWNER", str(response.data))
        self.assertFalse(self._holds_owner(self.alice))

    def test_replace_that_adds_members_with_inactive_role_revokes_nobody(self):
        add_user(self.customer, self.alice, CustomerRole.OWNER)
        self._deactivate_role()
        response = self.client.put(
            f"/scim/v2/Groups/{self._display()}",
            data={
                "schemas": ["urn:ietf:params:scim:schemas:core:2.0:Group"],
                "displayName": self._display(),
                "members": [{"value": self.bob.uuid.hex}],
            },
            format="json",
        )
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertFalse(self._holds_owner(self.bob))
        # The refusal must land before the removal half of the replace, or a
        # failed sync would strip the group's existing members.
        self.assertTrue(self._holds_owner(self.alice))

    def test_get_group_with_inactive_role_still_lists_members(self):
        add_user(self.customer, self.alice, CustomerRole.OWNER)
        self._deactivate_role()
        response = self.client.get(f"/scim/v2/Groups/{self._display()}")
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(
            [m["value"] for m in response.data["members"]], [self.alice.uuid.hex]
        )

    def test_patch_remove_member_with_inactive_role_still_revokes(self):
        add_user(self.customer, self.alice, CustomerRole.OWNER)
        self._deactivate_role()
        response = self.client.patch(
            f"/scim/v2/Groups/{self._display()}",
            data={
                "schemas": ["urn:ietf:params:scim:api:messages:2.0:PatchOp"],
                "Operations": [
                    {
                        "op": "remove",
                        "path": f'members[value eq "{self.alice.uuid.hex}"]',
                    }
                ],
            },
            format="json",
        )
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertFalse(self._holds_owner(self.alice))

    def test_replace_that_only_removes_members_with_inactive_role_still_works(self):
        add_user(self.customer, self.alice, CustomerRole.OWNER)
        add_user(self.customer, self.bob, CustomerRole.OWNER)
        self._deactivate_role()
        response = self.client.put(
            f"/scim/v2/Groups/{self._display()}",
            data={
                "schemas": ["urn:ietf:params:scim:schemas:core:2.0:Group"],
                "displayName": self._display(),
                "members": [{"value": self.alice.uuid.hex}],
            },
            format="json",
        )
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertTrue(self._holds_owner(self.alice))
        self.assertFalse(self._holds_owner(self.bob))

    def test_delete_group_with_inactive_role_still_revokes_members(self):
        add_user(self.customer, self.alice, CustomerRole.OWNER)
        self._deactivate_role()
        response = self.client.delete(f"/scim/v2/Groups/{self._display()}")
        self.assertEqual(response.status_code, status.HTTP_204_NO_CONTENT)
        self.assertFalse(self._holds_owner(self.alice))

    def test_mixed_add_and_remove_with_inactive_role_changes_nothing(self):
        # Identity providers send membership deltas as one PATCH carrying both
        # an add and a remove. The add cannot be satisfied, and a PATCH is all
        # or nothing, so the removal must not land either — a partially applied
        # sync is worse than a refused one, and the 400 names what to fix.
        add_user(self.customer, self.alice, CustomerRole.OWNER)
        self._deactivate_role()
        response = self.client.patch(
            f"/scim/v2/Groups/{self._display()}",
            data={
                "schemas": ["urn:ietf:params:scim:api:messages:2.0:PatchOp"],
                "Operations": [
                    {
                        "op": "remove",
                        "path": f'members[value eq "{self.alice.uuid.hex}"]',
                    },
                    {
                        "op": "add",
                        "path": "members",
                        "value": [{"value": self.bob.uuid.hex}],
                    },
                ],
            },
            format="json",
        )
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertIn("CUSTOMER.OWNER", str(response.data))
        self.assertTrue(self._holds_owner(self.alice))
        self.assertFalse(self._holds_owner(self.bob))

    def test_resync_of_unchanged_membership_with_inactive_role_is_a_noop(self):
        # An identity provider re-sends the full member list on every sync. If
        # nothing is actually being added, there is no grant to refuse.
        add_user(self.customer, self.alice, CustomerRole.OWNER)
        self._deactivate_role()
        response = self.client.put(
            f"/scim/v2/Groups/{self._display()}",
            data={
                "schemas": ["urn:ietf:params:scim:schemas:core:2.0:Group"],
                "displayName": self._display(),
                "members": [{"value": self.alice.uuid.hex}],
            },
            format="json",
        )
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertTrue(self._holds_owner(self.alice))


@override_config(SCIM_INBOUND_ENABLED=True)
class GroupsMemberPolicyTest(test.APITestCase):
    """Per-member grant policy applies to group sync too.

    These are properties of the member rather than of the group, so a rejected
    member is skipped and logged while the rest of the group is still
    provisioned — the behaviour the endpoint already had for concealed roles.
    """

    def setUp(self):
        token_key, self.svc_user = make_staff_token()
        self.client.credentials(
            HTTP_AUTHORIZATION=f"Bearer {token_key}",
            HTTP_ACCEPT="application/scim+json",
        )
        self.customer = structure_factories.CustomerFactory()
        self.project = structure_factories.ProjectFactory(customer=self.customer)
        CustomerRole.OWNER  # noqa: B018
        ProjectRole.MANAGER  # noqa: B018
        ProjectRole.ADMIN  # noqa: B018

    def _project_display(self, role_name: str) -> str:
        return f"waldur:project:{self.project.uuid.hex}:{role_name}"

    def _patch_add(self, display: str, users: list):
        return self.client.patch(
            f"/scim/v2/Groups/{display}",
            data={
                "schemas": ["urn:ietf:params:scim:api:messages:2.0:PatchOp"],
                "Operations": [
                    {
                        "op": "add",
                        "path": "members",
                        "value": [{"value": u.uuid.hex} for u in users],
                    }
                ],
            },
            format="json",
        )

    def test_member_failing_scope_restrictions_is_skipped(self):
        # A project that only admits one email domain must not be populated
        # with everyone the identity provider happens to put in the group.
        self.project.user_email_patterns = [r".*@allowed\.example\.com"]
        self.project.save(update_fields=["user_email_patterns"])
        allowed = structure_factories.UserFactory(
            username="allowed", email="carol@allowed.example.com"
        )
        refused = structure_factories.UserFactory(
            username="refused", email="dave@other.example.net"
        )

        response = self._patch_add(
            self._project_display("PROJECT.ADMIN"), [allowed, refused]
        )

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertTrue(
            UserRole.objects.filter(
                user=allowed, role=ProjectRole.ADMIN, is_active=True
            ).exists()
        )
        self.assertFalse(
            UserRole.objects.filter(
                user=refused, role=ProjectRole.ADMIN, is_active=True
            ).exists()
        )

    @override_config(SCIM_INBOUND_ENABLED=True, ONLY_ONE_PROJECT_MANAGER=True)
    def test_second_project_manager_is_skipped(self):
        first = structure_factories.UserFactory(username="first-manager")
        second = structure_factories.UserFactory(username="second-manager")
        add_user(self.project, first, ProjectRole.MANAGER)

        response = self._patch_add(self._project_display("PROJECT.MANAGER"), [second])

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertFalse(
            UserRole.objects.filter(
                user=second, role=ProjectRole.MANAGER, is_active=True
            ).exists()
        )

    @override_config(SCIM_INBOUND_ENABLED=True, INVITATION_DISABLE_MULTIPLE_ROLES=True)
    def test_second_role_in_scope_is_skipped_when_multiple_roles_disabled(self):
        user = structure_factories.UserFactory(username="already-a-manager")
        add_user(self.project, user, ProjectRole.MANAGER)

        response = self._patch_add(self._project_display("PROJECT.ADMIN"), [user])

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertFalse(
            UserRole.objects.filter(
                user=user, role=ProjectRole.ADMIN, is_active=True
            ).exists()
        )
        # The role they already held is untouched.
        self.assertTrue(
            UserRole.objects.filter(
                user=user, role=ProjectRole.MANAGER, is_active=True
            ).exists()
        )

    @override_config(SCIM_INBOUND_ENABLED=True, ONLY_ONE_PROJECT_MANAGER=True)
    def test_manager_handover_in_one_patch_leaves_a_manager(self):
        # Handing the role over arrives as one PATCH: remove the outgoing
        # manager, add the incoming one. The adds must not be evaluated while
        # the outgoing manager still holds the role, or the only-one-manager
        # rule rejects the incoming one, the removal goes through anyway, and
        # the project is left with no manager at all — reported as 200.
        outgoing = structure_factories.UserFactory(username="outgoing-manager")
        incoming = structure_factories.UserFactory(username="incoming-manager")
        add_user(self.project, outgoing, ProjectRole.MANAGER)

        response = self.client.patch(
            f"/scim/v2/Groups/{self._project_display('PROJECT.MANAGER')}",
            data={
                "schemas": ["urn:ietf:params:scim:api:messages:2.0:PatchOp"],
                "Operations": [
                    {
                        "op": "remove",
                        "path": f'members[value eq "{outgoing.uuid.hex}"]',
                    },
                    {
                        "op": "add",
                        "path": "members",
                        "value": [{"value": incoming.uuid.hex}],
                    },
                ],
            },
            format="json",
        )

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertFalse(
            UserRole.objects.filter(
                user=outgoing, role=ProjectRole.MANAGER, is_active=True
            ).exists()
        )
        self.assertTrue(
            UserRole.objects.filter(
                user=incoming, role=ProjectRole.MANAGER, is_active=True
            ).exists()
        )
