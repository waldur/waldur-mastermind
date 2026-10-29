"""A PAT bound to a service provider reaches the provider's offerings.

``get_scope_ancestors`` stops at an offering's customer, so a binding on the
ServiceProvider used to authorise nothing on its offerings or the resources
sold through them. ``_pat_provider_binding_matches`` accepts the provider in
PAT checks, and the list filters match.
"""

from datetime import timedelta

from django.contrib.contenttypes.models import ContentType
from django.test import RequestFactory
from django.utils import timezone
from rest_framework import status, test
from rest_framework.authtoken.models import Token

from waldur_core.configuration.testing import override_config
from waldur_core.permissions.enums import PermissionEnum
from waldur_core.permissions.fixtures import (
    CustomerRole,
    OfferingRole,
    ServiceProviderRole,
)
from waldur_core.permissions.tests.test_pat_list_filtering import (
    _auth_header,
    _create_pat,
)
from waldur_core.permissions.utils import (
    _pat_provider_binding_matches,
    get_scope_ancestors,
    has_permission,
)
from waldur_core.structure.tests import factories as structure_factories
from waldur_mastermind.marketplace import models as marketplace_models
from waldur_mastermind.marketplace.tests import factories as marketplace_factories
from waldur_mastermind.marketplace.tests.fixtures import MarketplaceFixture


class ProviderSetup:
    def setUp(self):
        self.staff = structure_factories.UserFactory(
            is_staff=True, can_use_personal_access_tokens=True
        )
        Token.objects.get_or_create(user=self.staff)
        self.provider = marketplace_factories.ServiceProviderFactory()
        self.offering = marketplace_factories.OfferingFactory(
            customer=self.provider.customer, shared=True
        )
        # Ordered by an unrelated organization: reachable only through the
        # offering, never through the provider's customer.
        self.consumer_project = structure_factories.ProjectFactory()
        self.resource = marketplace_factories.ResourceFactory(
            offering=self.offering, project=self.consumer_project
        )
        self.resource_project = marketplace_models.ResourceProject.objects.create(
            resource=self.resource, name="rp"
        )
        other_provider = marketplace_factories.ServiceProviderFactory()
        self.other_offering = marketplace_factories.OfferingFactory(
            customer=other_provider.customer, shared=True
        )
        self.other_resource = marketplace_factories.ResourceFactory(
            offering=self.other_offering
        )
        self.other_resource_project = marketplace_models.ResourceProject.objects.create(
            resource=self.other_resource, name="other-rp"
        )


@override_config(PAT_ENABLED=True)
class PATServiceProviderBindingPermissionTest(ProviderSetup, test.APITestCase):
    """Staff user, so the PAT binding is the only check that can deny."""

    def check(self, binding, permission, scope):
        pat = _create_pat(self.staff, scopes=[permission.value], bindings=[binding])
        request = RequestFactory().get("/")
        request.user = self.staff
        request.auth = pat
        return has_permission(request, permission, scope)

    def test_provider_binding_reaches_its_offering(self):
        self.assertTrue(
            self.check(self.provider, PermissionEnum.UPDATE_OFFERING, self.offering)
        )

    def test_provider_binding_reaches_resource_ordered_by_another_organization(self):
        self.assertTrue(
            self.check(self.provider, PermissionEnum.SET_RESOURCE_STATE, self.resource)
        )

    def test_provider_binding_reaches_resource_project(self):
        self.assertTrue(
            self.check(
                self.provider,
                PermissionEnum.SET_RESOURCE_STATE,
                self.resource_project,
            )
        )

    def test_provider_binding_does_not_reach_another_provider(self):
        self.assertFalse(
            self.check(
                self.provider, PermissionEnum.UPDATE_OFFERING, self.other_offering
            )
        )
        self.assertFalse(
            self.check(
                self.provider, PermissionEnum.SET_RESOURCE_STATE, self.other_resource
            )
        )

    def test_provider_customer_binding_still_does_not_reach_consumer_resource(self):
        self.assertFalse(
            self.check(
                self.provider.customer,
                PermissionEnum.SET_RESOURCE_STATE,
                self.resource,
            )
        )

    def test_no_provider_binding_costs_no_query(self):
        offering_ct = ContentType.objects.get_for_model(type(self.offering))
        pairs = frozenset({(offering_ct.id, self.offering.id)})
        ancestors = get_scope_ancestors(self.resource)
        _pat_provider_binding_matches(ancestors, pairs)  # warm the ContentType cache
        with self.assertNumQueries(0):
            self.assertFalse(_pat_provider_binding_matches(ancestors, pairs))

    def test_provider_binding_costs_one_query(self):
        provider_ct = ContentType.objects.get_for_model(type(self.provider))
        pairs = frozenset({(provider_ct.id, self.provider.id)})
        ancestors = get_scope_ancestors(self.resource)
        _pat_provider_binding_matches(ancestors, pairs)  # warm the ContentType cache
        with self.assertNumQueries(1):
            self.assertTrue(_pat_provider_binding_matches(ancestors, pairs))


@override_config(PAT_ENABLED=True)
class PATServiceProviderBindingListFilterTest(ProviderSetup, test.APITestCase):
    def list_uuids(self, url):
        pat = _create_pat(
            self.staff,
            scopes=[PermissionEnum.LIST_PROJECTS.value],
            bindings=[self.provider],
        )
        self.client.credentials(HTTP_AUTHORIZATION=_auth_header(pat))
        response = self.client.get(url)
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        return {row["uuid"] for row in response.data}

    def test_offerings(self):
        uuids = self.list_uuids("/api/marketplace-public-offerings/")
        self.assertIn(self.offering.uuid.hex, uuids)
        self.assertNotIn(self.other_offering.uuid.hex, uuids)

    def test_resources(self):
        uuids = self.list_uuids("/api/marketplace-provider-resources/")
        self.assertIn(self.resource.uuid.hex, uuids)
        self.assertNotIn(self.other_resource.uuid.hex, uuids)

    def test_resource_projects(self):
        uuids = self.list_uuids("/api/marketplace-provider-resource-projects/")
        self.assertIn(self.resource_project.uuid.hex, uuids)
        self.assertNotIn(self.other_resource_project.uuid.hex, uuids)


@override_config(PAT_ENABLED=True)
class PATServiceProviderBindingSiteAgentTest(test.APITestCase):
    """The reported setup, end to end: a site-agent account that manages the
    service provider and the offering creates a provider-bound PAT through the
    API and writes back to a resource ordered by another organization.

    permissions.yaml is not loaded in tests, so the roles get the permission
    the PAT asks for here, as a deployment grants it.
    """

    def setUp(self):
        self.fixture = MarketplaceFixture()
        self.resource = self.fixture.resource
        self.provider = self.fixture.service_provider
        for role in (
            OfferingRole.MANAGER,
            ServiceProviderRole.MANAGER,
            CustomerRole.OWNER,
        ):
            role.add_permission(PermissionEnum.SET_RESOURCE_STATE)

    def agent_user(self):
        user = self.fixture.offering_manager
        self.provider.add_user(user, ServiceProviderRole.MANAGER)
        return user

    def create_pat(self, user, binding_type, binding):
        user.can_use_personal_access_tokens = True
        user.save(update_fields=["can_use_personal_access_tokens"])
        self.client.force_authenticate(user=user)
        response = self.client.post(
            "/api/personal-access-tokens/",
            {
                "name": "site-agent",
                "scopes": [PermissionEnum.SET_RESOURCE_STATE.value],
                "expires_at": (timezone.now() + timedelta(days=30)).isoformat(),
                "allowed_scopes": [{"type": binding_type, "uuid": binding.uuid.hex}],
            },
            format="json",
        )
        self.assertEqual(response.status_code, status.HTTP_201_CREATED, response.data)
        self.client.force_authenticate(user=None)
        return response.data["token"]

    def refresh_last_sync(self, token):
        self.client.credentials(HTTP_AUTHORIZATION=f"Bearer {token}")
        url = marketplace_factories.ResourceFactory.get_provider_resource_url(
            self.resource, action="refresh_last_sync"
        )
        return self.client.post(url)

    def test_provider_bound_pat_can_write_back(self):
        token = self.create_pat(self.agent_user(), "service_provider", self.provider)
        response = self.refresh_last_sync(token)
        self.assertEqual(response.status_code, status.HTTP_200_OK)

    def test_pat_bound_to_another_provider_cannot(self):
        user = self.agent_user()
        other = marketplace_factories.ServiceProviderFactory()
        other.add_user(user, ServiceProviderRole.MANAGER)
        token = self.create_pat(user, "service_provider", other)
        response = self.refresh_last_sync(token)
        # The PAT list filter hides the resource before any permission check.
        self.assertEqual(response.status_code, status.HTTP_404_NOT_FOUND)

    def test_provider_owner_bound_pat_is_still_refused(self):
        """Pins current behaviour, not the intended one: the owner's role is on
        the organization, the binding on the provider, and the binding is
        checked where the role is held."""
        owner = structure_factories.UserFactory()
        self.provider.customer.add_user(owner, CustomerRole.OWNER)
        token = self.create_pat(owner, "service_provider", self.provider)
        response = self.refresh_last_sync(token)
        self.assertEqual(response.status_code, status.HTTP_403_FORBIDDEN)
