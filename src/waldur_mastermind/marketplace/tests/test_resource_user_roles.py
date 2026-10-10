from constance.test.unittest import override_config
from django.contrib.contenttypes.models import ContentType
from rest_framework import status, test
from rest_framework.reverse import reverse

from waldur_core.permissions.enums import PermissionEnum
from waldur_core.permissions.fixtures import (
    CustomerRole,
    OfferingRole,
    ServiceProviderRole,
)
from waldur_core.permissions.models import Role, UserRole
from waldur_core.structure.tests import factories as structure_factories
from waldur_mastermind.marketplace import models
from waldur_mastermind.marketplace.serializers import (
    ProviderUserRoleDetailsSerializer,
    UserProfileAttributeFieldsMixin,
)
from waldur_mastermind.marketplace.tests import factories as marketplace_factories
from waldur_mastermind.marketplace.tests import fixtures as marketplace_fixtures


class ResourceListUsersTest(test.APITestCase):
    """Test list_users on ConsumerResourceViewSet — aggregates UserRoles
    from the resource and all its resource projects."""

    def setUp(self):
        self.fixture = marketplace_fixtures.MarketplaceFixture()
        self.resource = self.fixture.resource

        self.resource_ct = ContentType.objects.get_for_model(models.Resource)
        self.rp_ct = ContentType.objects.get_for_model(models.ResourceProject)

        # Create roles
        self.resource_role = Role.objects.create(
            name="Resource Viewer",
            content_type=self.resource_ct,
            is_system_role=False,
        )
        self.project_role = Role.objects.create(
            name="Project Editor",
            content_type=self.rp_ct,
            is_system_role=False,
        )

        # Create resource projects
        self.rp_a = models.ResourceProject.objects.create(
            resource=self.resource, name="Project A"
        )
        self.rp_b = models.ResourceProject.objects.create(
            resource=self.resource, name="Project B"
        )

        # Users
        self.user_a = structure_factories.UserFactory()
        self.user_b = structure_factories.UserFactory()
        self.user_c = structure_factories.UserFactory()

    def _get_list_users_url(self):
        return marketplace_factories.ResourceFactory.get_url(
            self.resource, action="list_users"
        )

    def test_staff_sees_all_user_roles_across_resource_and_projects(self):
        # user_a has role on resource itself
        UserRole.objects.create(
            user=self.user_a,
            role=self.resource_role,
            content_type=self.resource_ct,
            object_id=self.resource.id,
        )
        # user_b has role on project A
        UserRole.objects.create(
            user=self.user_b,
            role=self.project_role,
            content_type=self.rp_ct,
            object_id=self.rp_a.id,
        )
        # user_c has role on project B
        UserRole.objects.create(
            user=self.user_c,
            role=self.project_role,
            content_type=self.rp_ct,
            object_id=self.rp_b.id,
        )

        staff = structure_factories.UserFactory(is_staff=True)
        self.client.force_authenticate(staff)
        response = self.client.get(self._get_list_users_url())
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(len(response.data), 3)

    def test_resource_only_roles_are_included(self):
        UserRole.objects.create(
            user=self.user_a,
            role=self.resource_role,
            content_type=self.resource_ct,
            object_id=self.resource.id,
        )

        staff = structure_factories.UserFactory(is_staff=True)
        self.client.force_authenticate(staff)
        response = self.client.get(self._get_list_users_url())
        self.assertEqual(len(response.data), 1)
        self.assertEqual(response.data[0]["user_uuid"], self.user_a.uuid.hex)

    def test_project_only_roles_are_included(self):
        UserRole.objects.create(
            user=self.user_b,
            role=self.project_role,
            content_type=self.rp_ct,
            object_id=self.rp_a.id,
        )

        staff = structure_factories.UserFactory(is_staff=True)
        self.client.force_authenticate(staff)
        response = self.client.get(self._get_list_users_url())
        self.assertEqual(len(response.data), 1)

    def test_roles_from_other_resources_not_included(self):
        other_resource = marketplace_factories.ResourceFactory(
            offering=self.fixture.offering
        )
        other_rp = models.ResourceProject.objects.create(
            resource=other_resource, name="Other"
        )
        # Role on a different resource's project
        UserRole.objects.create(
            user=self.user_a,
            role=self.project_role,
            content_type=self.rp_ct,
            object_id=other_rp.id,
        )

        staff = structure_factories.UserFactory(is_staff=True)
        self.client.force_authenticate(staff)
        response = self.client.get(self._get_list_users_url())
        self.assertEqual(len(response.data), 0)

    def test_inactive_user_roles_not_included(self):
        role = UserRole.objects.create(
            user=self.user_a,
            role=self.resource_role,
            content_type=self.resource_ct,
            object_id=self.resource.id,
        )
        role.revoke()

        staff = structure_factories.UserFactory(is_staff=True)
        self.client.force_authenticate(staff)
        response = self.client.get(self._get_list_users_url())
        self.assertEqual(len(response.data), 0)

    def test_empty_resource_returns_empty_list(self):
        staff = structure_factories.UserFactory(is_staff=True)
        self.client.force_authenticate(staff)
        response = self.client.get(self._get_list_users_url())
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(len(response.data), 0)

    def test_user_with_roles_in_both_resource_and_project_appears_twice(self):
        UserRole.objects.create(
            user=self.user_a,
            role=self.resource_role,
            content_type=self.resource_ct,
            object_id=self.resource.id,
        )
        UserRole.objects.create(
            user=self.user_a,
            role=self.project_role,
            content_type=self.rp_ct,
            object_id=self.rp_a.id,
        )

        staff = structure_factories.UserFactory(is_staff=True)
        self.client.force_authenticate(staff)
        response = self.client.get(self._get_list_users_url())
        self.assertEqual(len(response.data), 2)

    def test_filter_by_user(self):
        UserRole.objects.create(
            user=self.user_a,
            role=self.resource_role,
            content_type=self.resource_ct,
            object_id=self.resource.id,
        )
        UserRole.objects.create(
            user=self.user_b,
            role=self.project_role,
            content_type=self.rp_ct,
            object_id=self.rp_a.id,
        )

        staff = structure_factories.UserFactory(is_staff=True)
        self.client.force_authenticate(staff)
        response = self.client.get(
            self._get_list_users_url(), {"user": self.user_a.uuid.hex}
        )
        self.assertEqual(len(response.data), 1)
        self.assertEqual(response.data[0]["user_uuid"], self.user_a.uuid.hex)


class ProviderResourceListUsersTest(test.APITestCase):
    """list_users on ProviderResourceViewSet — service providers selling
    a resource (or its ResourceProjects) can enumerate the users with
    access to that consumer-side scope. Mirrors the consumer side's
    ResourceListUsersTest aggregation behavior; differs in that access
    is gated by provider-side roles on the offering rather than by
    consumer-side membership."""

    def setUp(self):
        self.fixture = marketplace_fixtures.MarketplaceFixture()
        self.resource = self.fixture.resource
        self.rp = models.ResourceProject.objects.create(
            resource=self.resource, name="Project A"
        )

        # One consumer-side user role on the project so list_users has
        # something to return.
        rp_ct = ContentType.objects.get_for_model(models.ResourceProject)
        role = Role.objects.create(
            name="Project Member", content_type=rp_ct, is_system_role=False
        )
        self.member = structure_factories.UserFactory()
        UserRole.objects.create(
            user=self.member,
            role=role,
            content_type=rp_ct,
            object_id=self.rp.id,
        )

        # Production deployments grant OFFERING.UPDATE to these roles via
        # permissions.yaml; the test DB doesn't load it, so wire it up
        # the same way other marketplace tests do (e.g. test_plans.py,
        # test_terms_of_service_consent.py).
        CustomerRole.OWNER.add_permission(PermissionEnum.UPDATE_OFFERING)
        OfferingRole.MANAGER.add_permission(PermissionEnum.UPDATE_OFFERING)
        ServiceProviderRole.MANAGER.add_permission(PermissionEnum.UPDATE_OFFERING)

    def _provider_resource_url(self):
        return marketplace_factories.ResourceFactory.get_provider_resource_url(
            self.resource, action="list_users"
        )

    def _provider_resource_project_url(self):
        return reverse(
            "marketplace-provider-resource-project-list-users",
            kwargs={"uuid": self.rp.uuid.hex},
        )

    def test_provider_owner_can_list_users_on_resource(self):
        self.client.force_authenticate(self.fixture.provider_owner)
        response = self.client.get(self._provider_resource_url())
        self.assertEqual(response.status_code, status.HTTP_200_OK)

    def test_provider_manager_can_list_users_on_resource(self):
        self.client.force_authenticate(self.fixture.provider_manager)
        response = self.client.get(self._provider_resource_url())
        self.assertEqual(response.status_code, status.HTTP_200_OK)

    def test_offering_manager_can_list_users_on_resource(self):
        self.client.force_authenticate(self.fixture.offering_manager)
        response = self.client.get(self._provider_resource_url())
        self.assertEqual(response.status_code, status.HTTP_200_OK)

    def test_provider_owner_can_list_users_on_resource_project(self):
        self.client.force_authenticate(self.fixture.provider_owner)
        response = self.client.get(self._provider_resource_project_url())
        self.assertEqual(response.status_code, status.HTTP_200_OK)

    def test_provider_manager_can_list_users_on_resource_project(self):
        self.client.force_authenticate(self.fixture.provider_manager)
        response = self.client.get(self._provider_resource_project_url())
        self.assertEqual(response.status_code, status.HTTP_200_OK)

    def test_offering_manager_can_list_users_on_resource_project(self):
        # Pairs with test_offering_manager_can_list_users_on_resource.
        # OFFERING.MANAGER's UPDATE_OFFERING perm is scoped to the
        # offering itself; the gate's provider-side branch checks both
        # offering and offering.customer scopes so this combination
        # works without any role on the offering's customer.
        self.client.force_authenticate(self.fixture.offering_manager)
        response = self.client.get(self._provider_resource_project_url())
        self.assertEqual(response.status_code, status.HTTP_200_OK)

    def test_unrelated_user_cannot_list_users(self):
        outsider = structure_factories.UserFactory()
        self.client.force_authenticate(outsider)
        response = self.client.get(self._provider_resource_url())
        self.assertEqual(response.status_code, status.HTTP_404_NOT_FOUND)

    def test_unrelated_user_cannot_list_users_on_resource_project(self):
        outsider = structure_factories.UserFactory()
        self.client.force_authenticate(outsider)
        response = self.client.get(self._provider_resource_project_url())
        self.assertEqual(response.status_code, status.HTTP_404_NOT_FOUND)

    # --- security boundary: consumer-side roles must NOT see users via
    # the provider URL (would leak member PII to consumer roles that
    # shouldn't have provider visibility). The queryset filter
    # filter_for_service_provider excludes them before the gate runs,
    # so we expect 404 (resource not found in the provider-scoped
    # queryset), not 403.

    def test_consumer_customer_owner_cannot_use_provider_resource_url(self):
        consumer_owner = structure_factories.UserFactory()
        self.fixture.customer.add_user(consumer_owner, CustomerRole.OWNER)
        self.client.force_authenticate(consumer_owner)
        response = self.client.get(self._provider_resource_url())
        self.assertEqual(response.status_code, status.HTTP_404_NOT_FOUND)

    def test_consumer_customer_owner_cannot_use_provider_rp_url(self):
        consumer_owner = structure_factories.UserFactory()
        self.fixture.customer.add_user(consumer_owner, CustomerRole.OWNER)
        self.client.force_authenticate(consumer_owner)
        response = self.client.get(self._provider_resource_project_url())
        self.assertEqual(response.status_code, status.HTTP_404_NOT_FOUND)


@override_config(ENFORCE_USER_CONSENT_FOR_OFFERINGS=True)
class ProviderResourceListUsersConsentTest(test.APITestCase):
    def setUp(self):
        self.fixture = marketplace_fixtures.MarketplaceFixture()
        self.resource = self.fixture.resource
        self.offering = self.fixture.offering
        self.member = structure_factories.UserFactory()
        resource_ct = ContentType.objects.get_for_model(models.Resource)
        role = Role.objects.create(
            name="Resource Member", content_type=resource_ct, is_system_role=False
        )
        UserRole.objects.create(
            user=self.member,
            role=role,
            content_type=resource_ct,
            object_id=self.resource.id,
        )
        models.OfferingTermsOfService.objects.create(
            offering=self.offering,
            terms_of_service="Test ToS",
            version="1.0",
            is_active=True,
        )
        CustomerRole.OWNER.add_permission(PermissionEnum.UPDATE_OFFERING)
        CustomerRole.OWNER.add_permission(PermissionEnum.VIEW_CUSTOMER_TEAM)
        OfferingRole.MANAGER.add_permission(PermissionEnum.UPDATE_OFFERING)
        self.provider_url = (
            marketplace_factories.ResourceFactory.get_provider_resource_url(
                self.resource, action="list_users"
            )
        )
        self.consumer_url = marketplace_factories.ResourceFactory.get_url(
            self.resource, action="list_users"
        )

    def _user_uuids(self, response):
        self.assertEqual(response.status_code, status.HTTP_200_OK, response.data)
        return [row["user_uuid"] for row in response.data]

    def test_provider_list_users_hides_member_without_consent(self):
        self.client.force_authenticate(self.fixture.provider_owner)
        response = self.client.get(self.provider_url)
        self.assertNotIn(self.member.uuid.hex, self._user_uuids(response))

    def test_provider_list_users_includes_member_with_consent(self):
        models.UserOfferingConsent.objects.create(
            user=self.member, offering=self.offering, version="1.0"
        )
        self.client.force_authenticate(self.fixture.provider_owner)
        response = self.client.get(self.provider_url)
        self.assertIn(self.member.uuid.hex, self._user_uuids(response))

    def test_staff_list_users_includes_member_without_consent(self):
        self.client.force_authenticate(self.fixture.staff)
        response = self.client.get(self.provider_url)
        self.assertIn(self.member.uuid.hex, self._user_uuids(response))

    def test_consumer_list_users_includes_member_without_consent(self):
        self.client.force_authenticate(self.fixture.owner)
        response = self.client.get(self.consumer_url)
        self.assertIn(self.member.uuid.hex, self._user_uuids(response))

    def test_provider_resource_project_list_users_hides_member_without_consent(self):
        resource_project = models.ResourceProject.objects.create(
            resource=self.resource, name="Project A"
        )
        project_ct = ContentType.objects.get_for_model(models.ResourceProject)
        role = Role.objects.create(
            name="Project Member", content_type=project_ct, is_system_role=False
        )
        UserRole.objects.create(
            user=self.member,
            role=role,
            content_type=project_ct,
            object_id=resource_project.id,
        )
        url = reverse(
            "marketplace-provider-resource-project-list-users",
            kwargs={"uuid": resource_project.uuid.hex},
        )
        self.client.force_authenticate(self.fixture.provider_owner)
        response = self.client.get(url)
        self.assertNotIn(self.member.uuid.hex, self._user_uuids(response))

    def test_offering_manager_list_users_hides_member_without_consent(self):
        self.client.force_authenticate(self.fixture.offering_manager)
        response = self.client.get(self.provider_url)
        self.assertNotIn(self.member.uuid.hex, self._user_uuids(response))

    def test_support_list_users_includes_member_without_consent(self):
        self.client.force_authenticate(self.fixture.global_support)
        response = self.client.get(self.provider_url)
        self.assertIn(self.member.uuid.hex, self._user_uuids(response))

    @override_config(ENFORCE_USER_CONSENT_FOR_OFFERINGS=False)
    def test_provider_list_users_includes_member_when_enforcement_is_off(self):
        self.client.force_authenticate(self.fixture.provider_owner)
        response = self.client.get(self.provider_url)
        self.assertIn(self.member.uuid.hex, self._user_uuids(response))


class ProviderListUsersUserAttributesTest(test.APITestCase):
    """Provider list_users returns the user attributes the offering exposes to
    its service provider, e.g. the civil number a site agent matches users by
    in an identity provider Waldur does not share."""

    def setUp(self):
        self.fixture = marketplace_fixtures.MarketplaceFixture()
        self.resource = self.fixture.resource
        self.offering = self.resource.offering
        self.rp = models.ResourceProject.objects.create(
            resource=self.resource, name="Project A"
        )
        self.member = structure_factories.UserFactory(civil_number="EE38001010000")

        rp_ct = ContentType.objects.get_for_model(models.ResourceProject)
        rp_role = Role.objects.create(
            name="Project Member", content_type=rp_ct, is_system_role=False
        )
        UserRole.objects.create(
            user=self.member, role=rp_role, content_type=rp_ct, object_id=self.rp.id
        )
        resource_ct = ContentType.objects.get_for_model(models.Resource)
        resource_role = Role.objects.create(
            name="Cluster Member", content_type=resource_ct, is_system_role=False
        )
        UserRole.objects.create(
            user=self.member,
            role=resource_role,
            content_type=resource_ct,
            object_id=self.resource.id,
        )
        # See ProviderResourceListUsersTest: the test DB has no permissions.yaml.
        OfferingRole.MANAGER.add_permission(PermissionEnum.UPDATE_OFFERING)
        self.client.force_authenticate(self.fixture.offering_manager)

    def _expose(self, **flags):
        models.OfferingUserAttributeConfig.objects.create(
            offering=self.offering, **flags
        )

    def _provider_resource_rows(self):
        response = self.client.get(
            marketplace_factories.ResourceFactory.get_provider_resource_url(
                self.resource, action="list_users"
            )
        )
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        return response.data

    def _provider_resource_project_rows(self):
        response = self.client.get(
            reverse(
                "marketplace-provider-resource-project-list-users",
                kwargs={"uuid": self.rp.uuid.hex},
            )
        )
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        return response.data

    def test_resource_project_returns_exposed_civil_number(self):
        self._expose(expose_civil_number=True)
        rows = self._provider_resource_project_rows()
        self.assertEqual(rows[0]["user_civil_number"], "EE38001010000")

    def test_resource_returns_exposed_civil_number(self):
        self._expose(expose_civil_number=True)
        rows = self._provider_resource_rows()
        self.assertEqual(rows[0]["user_civil_number"], "EE38001010000")

    def test_attribute_not_exposed_is_omitted(self):
        self._expose(expose_civil_number=False, expose_organization=True)
        row = self._provider_resource_project_rows()[0]
        self.assertNotIn("user_civil_number", row)
        self.assertIn("user_organization", row)

    @override_config(DEFAULT_OFFERING_USER_ATTRIBUTES=["username", "full_name"])
    def test_offering_without_config_uses_default_attributes(self):
        row = self._provider_resource_rows()[0]
        self.assertNotIn("user_civil_number", row)
        self.assertNotIn("user_phone_number", row)

    def test_role_details_fields_are_not_gated(self):
        self._expose(expose_email=False, expose_full_name=False)
        row = self._provider_resource_project_rows()[0]
        self.assertEqual(row["user_email"], self.member.email)
        self.assertEqual(row["user_full_name"], self.member.full_name)
        self.assertEqual(row["user_username"], self.member.username)

    def test_consumer_list_users_has_no_user_attributes(self):
        self._expose(expose_civil_number=True)
        staff = structure_factories.UserFactory(is_staff=True)
        self.client.force_authenticate(staff)
        response = self.client.get(
            marketplace_factories.ResourceFactory.get_url(
                self.resource, action="list_users"
            )
        )
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertNotIn("user_civil_number", response.data[0])

    def test_every_profile_field_is_gated_by_an_attribute(self):
        # A field added to the mixin without an entry in
        # OfferingUserSerializer.USER_ATTRIBUTE_FIELD_MAP would be returned
        # to providers regardless of the offering's config.
        self.assertEqual(
            set(ProviderUserRoleDetailsSerializer.GATED_FIELD_ATTRIBUTES),
            set(UserProfileAttributeFieldsMixin._declared_fields),
        )
