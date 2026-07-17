from django.contrib.contenttypes.models import ContentType
from rest_framework import status, test
from rest_framework.exceptions import ValidationError
from rest_framework.reverse import reverse

from waldur_core.permissions import utils
from waldur_core.permissions.enums import PermissionEnum
from waldur_core.permissions.fixtures import CustomerRole, ProjectRole
from waldur_core.permissions.models import (
    CustomerRoleConcealment,
    Role,
    RoleAvailability,
    UserRole,
)
from waldur_core.permissions.serializers import clone_role_for_customer
from waldur_core.structure.models import Customer, Project
from waldur_core.structure.tests import factories as structure_factories
from waldur_core.structure.tests import fixtures as structure_fixtures


def conceal(role, customer):
    return CustomerRoleConcealment.objects.create(
        role=role,
        content_type=ContentType.objects.get_for_model(Customer),
        object_id=customer.id,
    )


class RoleConcealmentGrantTest(test.APITestCase):
    def setUp(self):
        self.fixture = structure_fixtures.ProjectFixture()
        self.customer = self.fixture.customer
        self.project = self.fixture.project
        self.user = structure_factories.UserFactory()

    def test_concealed_customer_role_cannot_be_granted_on_customer(self):
        conceal(CustomerRole.OWNER, self.customer)
        with self.assertRaisesMessage(
            ValidationError, "Role is concealed for this organization."
        ):
            self.customer.add_user(self.user, CustomerRole.OWNER)

    def test_concealed_project_role_blocks_grant_in_org_project(self):
        # Concealment is bound to the customer; the ancestor walk must extend it
        # to the organization's projects.
        conceal(ProjectRole.MEMBER, self.customer)
        with self.assertRaisesMessage(
            ValidationError, "Role is concealed for this organization."
        ):
            self.project.add_user(self.user, ProjectRole.MEMBER)

    def test_concealment_in_one_org_does_not_affect_another(self):
        conceal(ProjectRole.MEMBER, self.customer)
        other_project = structure_factories.ProjectFactory()
        # Grant in an unrelated organization's project must still work.
        other_project.add_user(self.user, ProjectRole.MEMBER)
        self.assertTrue(
            UserRole.objects.filter(
                user=self.user, role=ProjectRole.MEMBER, is_active=True
            ).exists()
        )

    def test_force_bypasses_concealment(self):
        conceal(CustomerRole.OWNER, self.customer)
        self.customer.add_user(self.user, CustomerRole.OWNER, force=True)
        self.assertTrue(self.customer.has_user(self.user, CustomerRole.OWNER))

    def test_validate_role_grant_rejects_concealed_role(self):
        conceal(CustomerRole.OWNER, self.customer)
        with self.assertRaisesMessage(
            ValidationError, "Role is concealed for this organization."
        ):
            utils.validate_role_grant(self.customer, self.user, CustomerRole.OWNER)

    def test_existing_grant_survives_concealment_but_new_grant_is_blocked(self):
        self.customer.add_user(self.user, CustomerRole.OWNER)
        grant = UserRole.objects.get(
            user=self.user, role=CustomerRole.OWNER, is_active=True
        )
        conceal(CustomerRole.OWNER, self.customer)

        grant.refresh_from_db()
        self.assertTrue(grant.is_active)  # grandfathered

        other_user = structure_factories.UserFactory()
        with self.assertRaises(ValidationError):
            self.customer.add_user(other_user, CustomerRole.OWNER)


class ScopeAncestorsTest(test.APITestCase):
    def test_none_parents_are_dropped(self):
        # A scope with a nullable parent FK set to None (e.g. an offering with
        # no customer) must not leak None into the ancestor list, else the
        # unconditional concealment check crashes on get_for_model(None).
        class FakeScope:
            customer = None

        ancestors = utils.get_scope_ancestors(FakeScope())
        self.assertNotIn(None, ancestors)


class AvailabilityPrimitiveEnforcementTest(test.APITestCase):
    """The add_user primitive (not just validate_role_grant) enforces availability."""

    def setUp(self):
        self.fixture = structure_fixtures.ProjectFixture()
        self.customer = self.fixture.customer
        self.project = self.fixture.project
        self.user = structure_factories.UserFactory()

        # An org-private project role: bound (available) to this customer only.
        self.clone = Role.objects.create(
            name=f"PROJECT.{self.customer.uuid.hex}.MEMBER",
            content_type=ContentType.objects.get_for_model(Project),
            is_system_role=False,
        )
        RoleAvailability.objects.create(
            role=self.clone,
            content_type=ContentType.objects.get_for_model(Customer),
            object_id=self.customer.id,
        )

    def test_clone_grantable_in_owning_org_project(self):
        self.project.add_user(self.user, self.clone)
        self.assertTrue(self.project.has_user(self.user, self.clone))

    def test_clone_not_grantable_in_another_org_project(self):
        other_project = structure_factories.ProjectFactory()
        with self.assertRaisesMessage(
            ValidationError, "Role is not available for this scope."
        ):
            other_project.add_user(self.user, self.clone)


class AvailableForCustomerFilterTest(test.APITestCase):
    def setUp(self):
        from rest_framework.reverse import reverse

        self.customer_a = structure_factories.CustomerFactory()
        self.customer_b = structure_factories.CustomerFactory()
        # Staff so the filter test exercises the filter itself, not the
        # per-user visibility scoping (covered by RoleVisibilityTest).
        self.user = structure_factories.UserFactory(is_staff=True)
        customer_ct = ContentType.objects.get_for_model(Customer)

        # Ensure some system roles exist.
        self.system_owner = CustomerRole.OWNER
        self.system_reader = CustomerRole.READER

        def make_clone(customer, name):
            role = Role.objects.create(
                name=name,
                content_type=customer_ct,
                is_system_role=False,
            )
            RoleAvailability.objects.create(
                role=role, content_type=customer_ct, object_id=customer.id
            )
            return role

        self.clone_a = make_clone(
            self.customer_a, f"CUSTOMER.{self.customer_a.uuid.hex}.X"
        )
        self.clone_b = make_clone(
            self.customer_b, f"CUSTOMER.{self.customer_b.uuid.hex}.X"
        )
        # Conceal a system role for customer A only.
        conceal(self.system_reader, self.customer_a)

        self.url = "http://testserver" + reverse("role-list")

    def _uuids_for(self, customer):
        self.client.force_authenticate(self.user)
        response = self.client.get(
            self.url, {"available_for_customer": customer.uuid.hex, "page_size": 200}
        )
        self.assertEqual(response.status_code, 200)
        return {row["uuid"] for row in response.data}

    def test_filter_returns_system_plus_own_private_minus_concealed(self):
        uuids = self._uuids_for(self.customer_a)
        # own private clone present
        self.assertIn(self.clone_a.uuid.hex, uuids)
        # other org's private clone absent (no leakage)
        self.assertNotIn(self.clone_b.uuid.hex, uuids)
        # non-concealed system role present
        self.assertIn(self.system_owner.uuid.hex, uuids)
        # concealed system role absent
        self.assertNotIn(self.system_reader.uuid.hex, uuids)

    def test_concealed_role_visible_for_other_org(self):
        uuids = self._uuids_for(self.customer_b)
        # reader is only concealed for A, so it shows for B
        self.assertIn(self.system_reader.uuid.hex, uuids)
        self.assertIn(self.clone_b.uuid.hex, uuids)
        self.assertNotIn(self.clone_a.uuid.hex, uuids)


class RoleFilterAndOrderingTest(test.APITestCase):
    """Server-side filters and ordering exposed on the roles endpoint."""

    def setUp(self):
        self.staff = structure_factories.UserFactory(is_staff=True)
        self.customer = structure_factories.CustomerFactory()
        customer_ct = ContentType.objects.get_for_model(Customer)
        self.clone = Role.objects.create(
            name=f"CUSTOMER.{self.customer.uuid.hex}.X",
            description="Alpha org role",
            content_type=customer_ct,
            is_system_role=False,
        )
        RoleAvailability.objects.create(
            role=self.clone, content_type=customer_ct, object_id=self.customer.id
        )
        # Give the clone a live assignment so users_count ordering is exercised.
        member = structure_factories.UserFactory()
        self.customer.add_user(member, self.clone)
        self.url = "http://testserver" + reverse("role-list")
        self.client.force_authenticate(self.staff)

    def _get(self, **params):
        params.setdefault("page_size", 200)
        response = self.client.get(self.url, params)
        self.assertEqual(response.status_code, 200)
        return response.data

    def test_is_system_role_filter(self):
        system = {r["uuid"] for r in self._get(is_system_role=True)}
        custom = {r["uuid"] for r in self._get(is_system_role=False)}
        self.assertIn(self.clone.uuid.hex, custom)
        self.assertNotIn(self.clone.uuid.hex, system)

    def test_content_type_filter(self):
        customer_only = {r["content_type"] for r in self._get(content_type="customer")}
        self.assertEqual(customer_only, {"customer"})

    def test_query_matches_description(self):
        uuids = {r["uuid"] for r in self._get(query="Alpha org")}
        self.assertIn(self.clone.uuid.hex, uuids)

    def test_ordering_by_users_count(self):
        desc = self._get(o="-users_count")
        # The only role with an active assignment sorts first.
        self.assertEqual(desc[0]["uuid"], self.clone.uuid.hex)
        self.assertEqual(desc[0]["users_count"], 1)


class RoleVisibilityTest(test.APITestCase):
    """Non-staff users must not see other organizations' private roles."""

    def setUp(self):
        customer_ct = ContentType.objects.get_for_model(Customer)
        self.customer_a = structure_factories.CustomerFactory()
        self.customer_b = structure_factories.CustomerFactory()

        def make_clone(customer):
            role = Role.objects.create(
                name=f"CUSTOMER.{customer.uuid.hex}.X",
                content_type=customer_ct,
                is_system_role=False,
            )
            RoleAvailability.objects.create(
                role=role, content_type=customer_ct, object_id=customer.id
            )
            return role

        self.clone_a = make_clone(self.customer_a)
        self.clone_b = make_clone(self.customer_b)
        self.system_owner = CustomerRole.OWNER
        self.url = "http://testserver" + reverse("role-list")

    def _list(self, user):
        self.client.force_authenticate(user)
        response = self.client.get(self.url, {"page_size": 200})
        self.assertEqual(response.status_code, 200)
        return {row["uuid"] for row in response.data}

    def test_member_sees_own_private_but_not_others(self):
        member = structure_factories.UserFactory()
        self.customer_a.add_user(member, CustomerRole.OWNER)
        uuids = self._list(member)
        self.assertIn(self.clone_a.uuid.hex, uuids)
        self.assertNotIn(self.clone_b.uuid.hex, uuids)
        self.assertIn(self.system_owner.uuid.hex, uuids)

    def test_project_member_sees_org_private(self):
        project = structure_factories.ProjectFactory(customer=self.customer_a)
        project_member = structure_factories.UserFactory()
        project.add_user(project_member, ProjectRole.ADMIN)
        uuids = self._list(project_member)
        self.assertIn(self.clone_a.uuid.hex, uuids)
        self.assertNotIn(self.clone_b.uuid.hex, uuids)

    def test_staff_sees_all(self):
        staff = structure_factories.UserFactory(is_staff=True)
        uuids = self._list(staff)
        self.assertIn(self.clone_a.uuid.hex, uuids)
        self.assertIn(self.clone_b.uuid.hex, uuids)

    def test_non_member_sees_public_not_private(self):
        outsider = structure_factories.UserFactory()
        uuids = self._list(outsider)
        self.assertIn(self.system_owner.uuid.hex, uuids)
        self.assertNotIn(self.clone_a.uuid.hex, uuids)
        self.assertNotIn(self.clone_b.uuid.hex, uuids)

    def test_anonymous_sees_public_not_private(self):
        self.client.force_authenticate(None)
        response = self.client.get(self.url, {"page_size": 200})
        self.assertEqual(response.status_code, 200)
        uuids = {row["uuid"] for row in response.data}
        self.assertIn(self.system_owner.uuid.hex, uuids)
        self.assertNotIn(self.clone_a.uuid.hex, uuids)
        self.assertNotIn(self.clone_b.uuid.hex, uuids)


class RoleCloneEndpointTest(test.APITestCase):
    def setUp(self):
        self.staff = structure_factories.UserFactory(is_staff=True)
        self.regular = structure_factories.UserFactory()
        self.customer = structure_factories.CustomerFactory()
        project_ct = ContentType.objects.get_for_model(Project)
        self.template = Role.objects.create(
            name="PROJECT.TESTTEMPLATE",
            content_type=project_ct,
            is_system_role=False,
            description="Test Member",
        )
        self.template.add_permission(PermissionEnum.CREATE_PROJECT_PERMISSION.value)
        self.url = reverse(
            "role-clone-to-customer", kwargs={"uuid": self.template.uuid.hex}
        )

    def test_staff_can_clone_role_into_customer(self):
        self.client.force_authenticate(self.staff)
        response = self.client.post(self.url, {"customer": self.customer.uuid.hex})
        self.assertEqual(response.status_code, status.HTTP_201_CREATED, response.data)
        expected_name = f"PROJECT.{self.customer.slug}.TESTTEMPLATE"
        self.assertEqual(response.data["name"], expected_name)
        self.assertEqual(response.data["content_type"], "project")
        self.assertEqual(response.data["description"], "Test Member")
        self.assertEqual(
            list(response.data["permissions"]),
            [PermissionEnum.CREATE_PROJECT_PERMISSION],
        )
        self.assertEqual(response.data["template_uuid"], self.template.uuid.hex)
        self.assertEqual(response.data["template_name"], self.template.name)
        # The clone reports the organization that owns it.
        self.assertEqual(response.data["customer_uuid"], self.customer.uuid.hex)
        self.assertEqual(response.data["customer_name"], self.customer.name)
        clone = Role.objects.get(name=expected_name)
        customer_ct = ContentType.objects.get_for_model(Customer)
        self.assertTrue(
            clone.availability.filter(
                content_type=customer_ct, object_id=self.customer.id
            ).exists()
        )
        self.assertFalse(clone.is_system_role)
        self.assertEqual(clone.template_id, self.template.id)

    def test_clone_copies_translated_descriptions(self):
        self.template.description_et = "Liige"
        self.template.save()
        self.client.force_authenticate(self.staff)
        response = self.client.post(self.url, {"customer": self.customer.uuid.hex})
        self.assertEqual(response.status_code, status.HTTP_201_CREATED, response.data)
        clone = Role.objects.get(uuid=response.data["uuid"])
        self.assertEqual(clone.description_et, "Liige")

    def test_clone_conceals_template_by_default(self):
        self.client.force_authenticate(self.staff)
        response = self.client.post(self.url, {"customer": self.customer.uuid.hex})
        self.assertEqual(response.status_code, status.HTTP_201_CREATED, response.data)
        customer_ct = ContentType.objects.get_for_model(Customer)
        self.assertTrue(
            CustomerRoleConcealment.objects.filter(
                role=self.template,
                content_type=customer_ct,
                object_id=self.customer.id,
            ).exists()
        )

    def test_clone_without_conceal_keeps_template_visible(self):
        self.client.force_authenticate(self.staff)
        response = self.client.post(
            self.url,
            {"customer": self.customer.uuid.hex, "conceal_template": False},
        )
        self.assertEqual(response.status_code, status.HTTP_201_CREATED, response.data)
        customer_ct = ContentType.objects.get_for_model(Customer)
        self.assertFalse(
            CustomerRoleConcealment.objects.filter(
                role=self.template,
                content_type=customer_ct,
                object_id=self.customer.id,
            ).exists()
        )

    def test_cloning_same_template_twice_is_rejected(self):
        self.client.force_authenticate(self.staff)
        self.client.post(self.url, {"customer": self.customer.uuid.hex})
        response = self.client.post(self.url, {"customer": self.customer.uuid.hex})
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)

    def test_non_staff_cannot_clone(self):
        self.client.force_authenticate(self.regular)
        response = self.client.post(self.url, {"customer": self.customer.uuid.hex})
        self.assertIn(
            response.status_code,
            (status.HTTP_403_FORBIDDEN, status.HTTP_404_NOT_FOUND),
        )

    def test_cannot_clone_resource_scope_role(self):
        from waldur_mastermind.marketplace import models as marketplace_models

        resource_ct = ContentType.objects.get_for_model(marketplace_models.Resource)
        role = Role.objects.create(
            name="Cluster Admin", content_type=resource_ct, is_system_role=False
        )
        url = reverse("role-clone-to-customer", kwargs={"uuid": role.uuid.hex})
        self.client.force_authenticate(self.staff)
        response = self.client.post(url, {"customer": self.customer.uuid.hex})
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)


class UserPermissionCustomerFilterTest(test.APITestCase):
    """The customer_uuid filter scopes grants to a customer and its projects."""

    def setUp(self):
        self.staff = structure_factories.UserFactory(is_staff=True)
        self.fixture_a = structure_fixtures.ProjectFixture()
        self.customer_a = self.fixture_a.customer
        self.project_a = self.fixture_a.project
        self.customer_b = structure_factories.CustomerFactory()

        self.owner_a = structure_factories.UserFactory()
        self.customer_a.add_user(self.owner_a, CustomerRole.OWNER)
        self.admin_a = structure_factories.UserFactory()
        self.project_a.add_user(self.admin_a, ProjectRole.ADMIN)
        self.owner_b = structure_factories.UserFactory()
        self.customer_b.add_user(self.owner_b, CustomerRole.OWNER)

        self.url = "/api/user-permissions/"
        self.client.force_authenticate(self.staff)

    def _usernames(self, **params):
        params["is_active"] = True
        response = self.client.get(self.url, {**params, "page_size": 200})
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        return {row["user_name"] for row in response.data}

    def test_customer_scope_grant_is_included(self):
        names = self._usernames(
            role_uuid=CustomerRole.OWNER.uuid.hex,
            customer_uuid=self.customer_a.uuid.hex,
        )
        self.assertIn(self.owner_a.get_full_name() or self.owner_a.username, names)
        self.assertNotIn(self.owner_b.get_full_name() or self.owner_b.username, names)

    def test_project_scope_grant_is_included(self):
        names = self._usernames(
            role_uuid=ProjectRole.ADMIN.uuid.hex,
            customer_uuid=self.customer_a.uuid.hex,
        )
        self.assertIn(self.admin_a.get_full_name() or self.admin_a.username, names)

    def test_other_customer_grants_are_excluded(self):
        names = self._usernames(
            role_uuid=CustomerRole.OWNER.uuid.hex,
            customer_uuid=self.customer_b.uuid.hex,
        )
        self.assertNotIn(self.owner_a.get_full_name() or self.owner_a.username, names)

    def test_response_exposes_username_and_email(self):
        response = self.client.get(
            self.url,
            {
                "role_uuid": CustomerRole.OWNER.uuid.hex,
                "customer_uuid": self.customer_a.uuid.hex,
                "is_active": True,
            },
        )
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        row = next(r for r in response.data if r["user_uuid"] == self.owner_a.uuid.hex)
        self.assertEqual(row["user_username"], self.owner_a.username)
        self.assertEqual(row["user_email"], self.owner_a.email)


class GrantPolicySkipTest(test.APITestCase):
    """add_user_or_skip respects the org-scoping policy without raising."""

    def setUp(self):
        self.fixture = structure_fixtures.ProjectFixture()
        self.customer = self.fixture.customer
        self.project = self.fixture.project
        self.user = structure_factories.UserFactory()

    def test_concealed_role_is_skipped_not_raised(self):
        conceal(ProjectRole.MEMBER, self.customer)
        result = self.project.add_user_or_skip(self.user, ProjectRole.MEMBER)
        self.assertIsNone(result)
        self.assertFalse(self.project.has_user(self.user, ProjectRole.MEMBER))

    def test_allowed_role_is_granted(self):
        result = self.project.add_user_or_skip(self.user, ProjectRole.MEMBER)
        self.assertIsNotNone(result)
        self.assertTrue(self.project.has_user(self.user, ProjectRole.MEMBER))


class ConcealmentAvailabilityGateTest(test.APITestCase):
    """A role must be grantable in the organization to be concealable there."""

    def setUp(self):
        self.staff = structure_factories.UserFactory(is_staff=True)
        self.customer_a = structure_factories.CustomerFactory()
        self.customer_b = structure_factories.CustomerFactory()
        customer_ct = ContentType.objects.get_for_model(Customer)
        # A private clone that belongs to customer B only.
        self.clone_b = Role.objects.create(
            name=f"CUSTOMER.{self.customer_b.uuid.hex}.X",
            content_type=customer_ct,
            is_system_role=False,
        )
        RoleAvailability.objects.create(
            role=self.clone_b, content_type=customer_ct, object_id=self.customer_b.id
        )
        self.url = reverse("customer-role-concealment-list")
        self.client.force_authenticate(self.staff)

    def test_cannot_conceal_other_orgs_clone(self):
        response = self.client.post(
            self.url,
            {"role": self.clone_b.uuid.hex, "customer": self.customer_a.uuid.hex},
        )
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)

    def test_can_conceal_system_role(self):
        response = self.client.post(
            self.url,
            {
                "role": CustomerRole.SUPPORT.uuid.hex,
                "customer": self.customer_a.uuid.hex,
            },
        )
        self.assertEqual(response.status_code, status.HTTP_201_CREATED, response.data)


class AvailableForCustomerMembershipTest(test.APITestCase):
    """available_for_customer must not let a non-member probe another org."""

    def setUp(self):
        self.customer = structure_factories.CustomerFactory()
        self.url = "http://testserver" + reverse("role-list")

    def test_non_member_gets_nothing(self):
        outsider = structure_factories.UserFactory()
        self.client.force_authenticate(outsider)
        response = self.client.get(
            self.url, {"available_for_customer": self.customer.uuid.hex}
        )
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(len(response.data), 0)

    def test_member_sees_roles(self):
        member = structure_factories.UserFactory()
        self.customer.add_user(member, CustomerRole.OWNER)
        self.client.force_authenticate(member)
        response = self.client.get(
            self.url, {"available_for_customer": self.customer.uuid.hex}
        )
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertGreater(len(response.data), 0)


class OrgRoleSlugNamingTest(test.APITestCase):
    """Clone names embed the organization slug and follow slug changes."""

    def setUp(self):
        self.customer = structure_factories.CustomerFactory()
        self.template = CustomerRole.OWNER

    def test_clone_name_uses_slug(self):
        clone = clone_role_for_customer(
            self.template, self.customer, conceal_template=False
        )
        self.assertEqual(clone.name, f"CUSTOMER.{self.customer.slug}.OWNER")

    def test_slug_change_renames_clones(self):
        clone = clone_role_for_customer(
            self.template, self.customer, conceal_template=False
        )
        self.customer.slug = "renamed-org"
        self.customer.save()
        clone.refresh_from_db()
        self.assertEqual(clone.name, "CUSTOMER.renamed-org.OWNER")

    def test_slug_collision_gets_suffix(self):
        customer_b = structure_factories.CustomerFactory()
        customer_b.slug = self.customer.slug
        customer_b.save()
        clone_a = clone_role_for_customer(
            self.template, self.customer, conceal_template=False
        )
        clone_b = clone_role_for_customer(
            self.template, customer_b, conceal_template=False
        )
        self.assertEqual(clone_a.name, f"CUSTOMER.{self.customer.slug}.OWNER")
        self.assertEqual(clone_b.name, f"{clone_a.name}-2")


class OrgRoleEditGuardTest(test.APITestCase):
    """An org clone's name and scope are fixed; only description/permissions edit."""

    def setUp(self):
        self.staff = structure_factories.UserFactory(is_staff=True)
        self.customer = structure_factories.CustomerFactory()
        customer_ct = ContentType.objects.get_for_model(Customer)
        self.clone = Role.objects.create(
            name=f"CUSTOMER.{self.customer.uuid.hex}.OWNER",
            content_type=customer_ct,
            is_system_role=False,
        )
        self.clone.add_permission(PermissionEnum.CREATE_CUSTOMER_PERMISSION.value)
        RoleAvailability.objects.create(
            role=self.clone, content_type=customer_ct, object_id=self.customer.id
        )
        self.url = reverse("role-detail", kwargs={"uuid": self.clone.uuid.hex})
        self.client.force_authenticate(self.staff)

    def _body(self, **overrides):
        body = {
            "name": self.clone.name,
            "content_type": "customer",
            "permissions": [PermissionEnum.CREATE_CUSTOMER_PERMISSION.value],
        }
        body.update(overrides)
        return body

    def test_rename_is_rejected(self):
        response = self.client.put(self.url, self._body(name="RENAMED"))
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)

    def test_scope_change_is_rejected(self):
        response = self.client.put(self.url, self._body(content_type="project"))
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)

    def test_permissions_edit_is_allowed(self):
        response = self.client.put(
            self.url,
            self._body(
                permissions=[
                    PermissionEnum.CREATE_CUSTOMER_PERMISSION.value,
                    PermissionEnum.DELETE_CUSTOMER_PERMISSION.value,
                ]
            ),
        )
        self.assertEqual(response.status_code, status.HTTP_200_OK, response.data)


class ConcealmentEndpointTest(test.APITestCase):
    def setUp(self):
        self.staff = structure_factories.UserFactory(is_staff=True)
        self.regular = structure_factories.UserFactory()
        self.fixture = structure_fixtures.ProjectFixture()
        self.customer = self.fixture.customer
        self.list_url = reverse("customer-role-concealment-list")

    def test_staff_can_conceal_and_reveal(self):
        self.client.force_authenticate(self.staff)
        response = self.client.post(
            self.list_url,
            {
                "role": ProjectRole.MEMBER.uuid.hex,
                "customer": self.customer.uuid.hex,
            },
        )
        self.assertEqual(response.status_code, status.HTTP_201_CREATED, response.data)
        # Concealed role now blocks grants in the org's projects.
        with self.assertRaises(ValidationError):
            self.fixture.project.add_user(self.regular, ProjectRole.MEMBER)

        detail_url = reverse(
            "customer-role-concealment-detail",
            kwargs={"uuid": response.data["uuid"]},
        )
        delete = self.client.delete(detail_url)
        self.assertEqual(delete.status_code, status.HTTP_204_NO_CONTENT)
        # Revealed: grant works again.
        self.fixture.project.add_user(self.regular, ProjectRole.MEMBER)

    def test_non_staff_cannot_conceal(self):
        self.client.force_authenticate(self.regular)
        response = self.client.post(
            self.list_url,
            {"role": ProjectRole.MEMBER.uuid.hex, "customer": self.customer.uuid.hex},
        )
        self.assertIn(
            response.status_code,
            (status.HTTP_403_FORBIDDEN, status.HTTP_404_NOT_FOUND),
        )

    def test_lockout_guard_blocks_concealing_last_owner_capable_role(self):
        customer_ct = ContentType.objects.get_for_model(Customer)
        owner_role = Role.objects.create(
            name="CUSTOMER.SOLEOWNER", content_type=customer_ct, is_system_role=False
        )
        owner_role.add_permission(PermissionEnum.CREATE_CUSTOMER_PERMISSION.value)
        self.client.force_authenticate(self.staff)
        response = self.client.post(
            self.list_url,
            {"role": owner_role.uuid.hex, "customer": self.customer.uuid.hex},
        )
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)

    def test_lockout_guard_allows_when_another_owner_role_remains(self):
        customer_ct = ContentType.objects.get_for_model(Customer)
        role_a = Role.objects.create(
            name="CUSTOMER.OWNERA", content_type=customer_ct, is_system_role=False
        )
        role_a.add_permission(PermissionEnum.CREATE_CUSTOMER_PERMISSION.value)
        role_b = Role.objects.create(
            name="CUSTOMER.OWNERB", content_type=customer_ct, is_system_role=False
        )
        role_b.add_permission(PermissionEnum.CREATE_CUSTOMER_PERMISSION.value)
        self.client.force_authenticate(self.staff)
        response = self.client.post(
            self.list_url,
            {"role": role_a.uuid.hex, "customer": self.customer.uuid.hex},
        )
        self.assertEqual(response.status_code, status.HTTP_201_CREATED, response.data)
