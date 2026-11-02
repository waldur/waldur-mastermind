"""Who may read a resource's GLauth export (#983).

The resource-level ``glauth_users_config`` and ``glauth_tree`` carry the
offering's shared password hash and the project users' emails and SSH keys,
and each call marks the offering's GLauth integration as active. They take
what reading the offering's secret options takes, OFFERING.UPDATE_INTEGRATION
on the offering, its organization or its ServiceProvider, and exist only on
the provider resource view.
"""

from ddt import data, ddt, unpack
from django.contrib.contenttypes.models import ContentType
from rest_framework import status, test

from waldur_core.configuration.testing import override_config
from waldur_core.core.tests.helpers import create_pat
from waldur_core.permissions.enums import PermissionEnum
from waldur_core.permissions.fixtures import (
    CustomerRole,
    OfferingRole,
    ProjectRole,
    ServiceProviderRole,
)
from waldur_core.structure.tests import factories as structure_factories
from waldur_mastermind.marketplace import models as marketplace_models
from waldur_mastermind.marketplace.tests import factories as marketplace_factories
from waldur_mastermind.marketplace_site_agent.tests.fixtures import GlauthUserFixture

ACTIONS = ("glauth_users_config", "glauth_tree")


class ResourceGlauthAccessMixin:
    def setUp(self):
        self.fixture = GlauthUserFixture()
        self.offering = self.fixture.offering
        self.offering.secret_options = {"shared_user_password": "s3cret"}
        self.offering.save(update_fields=["secret_options"])
        self.resource = self.fixture.resource
        # As docker/rootfs/etc/waldur/permissions.yaml grants it.
        for role in (
            CustomerRole.OWNER,
            ServiceProviderRole.MANAGER,
            OfferingRole.MANAGER,
        ):
            role.add_permission(PermissionEnum.UPDATE_OFFERING_INTEGRATION)

    def user_with_role(self, role):
        """A user holding ``role`` and nothing else, on its scope."""
        user = structure_factories.UserFactory()
        if role == "project_member":
            self.fixture.project.add_user(user, ProjectRole.MEMBER)
            ProjectRole.MEMBER.add_permission(PermissionEnum.LIST_RESOURCES)
        elif role == "customer_reader":
            self.fixture.customer.add_user(user, CustomerRole.READER)
            CustomerRole.READER.add_permission(PermissionEnum.LIST_RESOURCES)
        elif role == "customer_owner":
            self.fixture.customer.add_user(user, CustomerRole.OWNER)
            CustomerRole.OWNER.add_permission(PermissionEnum.LIST_RESOURCES)
        elif role == "provider_support":
            # A member of the provider organization without manager rights.
            self.fixture.offering_customer.add_user(user, CustomerRole.SUPPORT)
        elif role == "provider_reader":
            self.fixture.offering_customer.add_user(user, CustomerRole.READER)
        elif role == "other_offering_manager":
            other = marketplace_factories.OfferingFactory(
                customer=self.fixture.offering_customer
            )
            other.add_user(user, OfferingRole.MANAGER)
        elif role == "consumer_owner_and_provider_support":
            # Owns the resource's (consumer) organization and can see the
            # resource from the provider side: an owner of the wrong
            # organization must not pass as the offering's owner.
            self.fixture.customer.add_user(user, CustomerRole.OWNER)
            self.fixture.offering_customer.add_user(user, CustomerRole.SUPPORT)
        elif role == "global_support":
            user.is_support = True
            user.save(update_fields=["is_support"])
        elif role == "offering_manager":
            self.offering.add_user(user, OfferingRole.MANAGER)
        elif role == "provider_owner":
            self.fixture.offering_customer.add_user(user, CustomerRole.OWNER)
        elif role == "service_provider_manager":
            # Held on the ServiceProvider record, as the API grants it.
            self.fixture.service_provider.add_user(user, ServiceProviderRole.MANAGER)
        elif role == "staff":
            user.is_staff = True
            user.save(update_fields=["is_staff"])
        else:
            raise AssertionError(role)
        return user

    def glauth_status_exists(self):
        return marketplace_models.IntegrationStatus.objects.filter(
            offering=self.offering,
            agent_type=marketplace_models.IntegrationStatus.AgentTypes.GLAUTH_SYNC,
        ).exists()

    def get(self, url_getter, action, user=None):
        if user is not None:
            self.client.force_authenticate(user)
        return self.client.get(
            url_getter(self.resource, action), HTTP_USER_AGENT="spoofed-agent"
        )

    def provider_url(self, resource, action):
        return marketplace_factories.ResourceFactory.get_provider_resource_url(
            resource, action
        )

    def consumer_url(self, resource, action):
        return marketplace_factories.ResourceFactory.get_url(resource, action)


@ddt
class ProviderResourceGlauthAccessTest(ResourceGlauthAccessMixin, test.APITestCase):
    @data(
        "offering_manager",
        "provider_owner",
        "service_provider_manager",
        "staff",
    )
    def test_offering_managers_read_both_exports(self, role):
        user = self.user_with_role(role)
        for action in ACTIONS:
            response = self.get(self.provider_url, action, user)
            self.assertEqual(response.status_code, status.HTTP_200_OK, action)

        integration_status = marketplace_models.IntegrationStatus.objects.get(
            offering=self.offering,
            agent_type=marketplace_models.IntegrationStatus.AgentTypes.GLAUTH_SYNC,
        )
        self.assertEqual(integration_status.service_name, "spoofed-agent")

    def test_export_carries_the_offering_users(self):
        # The control for the refusals below: the route serves the export.
        user = self.user_with_role("offering_manager")
        response = self.get(self.provider_url, "glauth_users_config", user)
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertIn(self.fixture.offering_user.username, response.content.decode())

    @data(
        # Cannot see the resource from the provider side.
        ("project_member", status.HTTP_404_NOT_FOUND),
        ("customer_reader", status.HTTP_404_NOT_FOUND),
        ("customer_owner", status.HTTP_404_NOT_FOUND),
        ("other_offering_manager", status.HTTP_404_NOT_FOUND),
        # See it, and the check refuses them.
        ("provider_support", status.HTTP_403_FORBIDDEN),
        ("provider_reader", status.HTTP_403_FORBIDDEN),
        ("consumer_owner_and_provider_support", status.HTTP_403_FORBIDDEN),
        ("global_support", status.HTTP_403_FORBIDDEN),
    )
    @unpack
    def test_others_are_refused_and_leave_integration_status_alone(
        self, role, expected
    ):
        user = self.user_with_role(role)
        for action in ACTIONS:
            response = self.get(self.provider_url, action, user)
            self.assertEqual(response.status_code, expected, action)
        self.assertFalse(self.glauth_status_exists())

    def test_provider_member_who_sees_the_resource_gets_403(self):
        # Seeing the resource is not enough: the check itself refuses.
        user = self.user_with_role("provider_support")
        self.client.force_authenticate(user)
        self.assertEqual(
            self.client.get(self.provider_url(self.resource, None)).status_code,
            status.HTTP_200_OK,
        )
        for action in ACTIONS:
            response = self.get(self.provider_url, action)
            self.assertEqual(response.status_code, status.HTTP_403_FORBIDDEN, action)
        self.assertFalse(self.glauth_status_exists())

    def test_refusal_leaves_an_existing_integration_status_unchanged(self):
        integration_status = marketplace_models.IntegrationStatus.objects.create(
            offering=self.offering,
            agent_type=marketplace_models.IntegrationStatus.AgentTypes.GLAUTH_SYNC,
            service_name="real-agent",
        )
        user = self.user_with_role("provider_support")
        for action in ACTIONS:
            self.get(self.provider_url, action, user)

        integration_status.refresh_from_db()
        self.assertEqual(integration_status.service_name, "real-agent")
        self.assertIsNone(integration_status.last_request_timestamp)
        self.assertNotEqual(
            integration_status.status,
            marketplace_models.IntegrationStatus.States.ACTIVE,
        )

    def get_with_token(self, user, scopes, bindings=()):
        user.can_use_personal_access_tokens = True
        user.save(update_fields=["can_use_personal_access_tokens"])
        _, token = create_pat(
            user,
            scopes=scopes,
            allowed_scopes=[
                {
                    "content_type_id": ContentType.objects.get_for_model(scope).id,
                    "object_id": scope.id,
                }
                for scope in bindings
            ],
        )
        self.client.credentials(HTTP_AUTHORIZATION=f"Bearer {token}")
        return [self.get(self.provider_url, action).status_code for action in ACTIONS]

    @data("offering_manager", "staff")
    @override_config(PAT_ENABLED=True)
    def test_token_needs_the_integration_scope(self, role):
        user = self.user_with_role(role)
        self.assertEqual(
            self.get_with_token(
                user,
                [
                    PermissionEnum.LIST_RESOURCES.value,
                    PermissionEnum.STAFF_ACCESS.value,
                ],
            ),
            [status.HTTP_403_FORBIDDEN] * 2,
        )
        self.assertFalse(self.glauth_status_exists())

        self.assertEqual(
            self.get_with_token(
                user, [PermissionEnum.UPDATE_OFFERING_INTEGRATION.value]
            ),
            [status.HTTP_200_OK] * 2,
        )

    @override_config(PAT_ENABLED=True)
    def test_token_bound_elsewhere_is_refused(self):
        user = self.user_with_role("offering_manager")
        other_offering = marketplace_factories.OfferingFactory(
            customer=self.fixture.offering_customer
        )
        self.assertEqual(
            self.get_with_token(
                user,
                [PermissionEnum.UPDATE_OFFERING_INTEGRATION.value],
                bindings=[other_offering],
            ),
            # The binding already hides the resource from the token.
            [status.HTTP_404_NOT_FOUND] * 2,
        )
        self.assertFalse(self.glauth_status_exists())


@ddt
class ConsumerResourceGlauthAccessTest(ResourceGlauthAccessMixin, test.APITestCase):
    """Nothing calls the consumer routes, so they are gone.

    DRF checks permissions before the view refuses a disabled action, so a
    consumer the check refuses gets 403 here, and a caller it admits 405.
    None of them gets the export or touches the integration status.
    """

    @data("project_member", "customer_reader", "customer_owner")
    def test_consumers_are_refused(self, role):
        user = self.user_with_role(role)
        for action in ACTIONS:
            response = self.get(self.consumer_url, action, user)
            self.assertEqual(response.status_code, status.HTTP_403_FORBIDDEN, action)
        self.assertFalse(self.glauth_status_exists())

    def test_route_is_disabled_for_an_offering_manager_in_the_project(self):
        # Sees the resource as a consumer and passes the check: only the
        # disabled action keeps the export from them.
        user = self.user_with_role("offering_manager")
        self.fixture.project.add_user(user, ProjectRole.MEMBER)
        ProjectRole.MEMBER.add_permission(PermissionEnum.LIST_RESOURCES)
        for action in ACTIONS:
            response = self.get(self.consumer_url, action, user)
            self.assertEqual(
                response.status_code, status.HTTP_405_METHOD_NOT_ALLOWED, action
            )
        self.assertFalse(self.glauth_status_exists())

    def test_route_is_disabled_for_staff(self):
        user = self.user_with_role("staff")
        for action in ACTIONS:
            response = self.get(self.consumer_url, action, user)
            self.assertEqual(
                response.status_code, status.HTTP_405_METHOD_NOT_ALLOWED, action
            )
        self.assertFalse(self.glauth_status_exists())
