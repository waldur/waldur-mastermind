"""End-to-end tests for the PAT list-endpoint filter backend.

These tests use staff users and a Bearer PAT so the underlying
permission checks pass unconditionally and the only thing under test is
the queryset narrowing applied by ``PATScopeListFilter``.
"""

from datetime import timedelta

from constance.test import override_config
from django.contrib.contenttypes.models import ContentType
from django.utils import timezone
from rest_framework import status, test
from rest_framework.authtoken.models import Token

from waldur_core.core.models import PersonalAccessToken
from waldur_core.permissions.enums import PermissionEnum
from waldur_core.structure.tests import factories as structure_factories
from waldur_mastermind.marketplace.tests import factories as marketplace_factories
from waldur_mastermind.proposal.tests import factories as proposal_factories


def _create_pat(user, scopes, bindings):
    """Create a PAT and (optionally) attach entity bindings.

    ``bindings`` is a list of model instances; each is recorded as a
    ``{content_type_id, object_id}`` pair, mirroring how the create
    serializer stores them.
    """
    full_token, prefix, token_hash = PersonalAccessToken.generate_token(
        timezone.now() + timedelta(days=30)
    )
    allowed_scopes = [
        {
            "content_type_id": ContentType.objects.get_for_model(type(b)).id,
            "object_id": b.id,
        }
        for b in bindings
    ]
    pat = PersonalAccessToken.objects.create(
        user=user,
        name="filter-test",
        token_prefix=prefix,
        token_hash=token_hash,
        scopes=scopes,
        allowed_scopes=allowed_scopes,
        expires_at=timezone.now() + timedelta(days=30),
    )
    pat._plaintext_token = full_token
    return pat


def _auth_header(pat):
    return f"Bearer {pat._plaintext_token}"


@override_config(PAT_ENABLED=True)
class PATListFilterCustomerTest(test.APITestCase):
    def setUp(self):
        self.staff = structure_factories.UserFactory(
            is_staff=True, can_use_personal_access_tokens=True
        )
        Token.objects.get_or_create(user=self.staff)
        self.bound = structure_factories.CustomerFactory(name="bound")
        self.unbound = structure_factories.CustomerFactory(name="unbound")

    def test_customer_list_filtered_by_binding(self):
        # The customer list endpoint doesn't gate on a specific permission,
        # so any valid scope keeps the PAT auth flow happy.
        pat = _create_pat(
            self.staff,
            scopes=[PermissionEnum.LIST_PROJECTS.value],
            bindings=[self.bound],
        )
        self.client.credentials(HTTP_AUTHORIZATION=_auth_header(pat))
        response = self.client.get("/api/customers/")
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        uuids = {row["uuid"] for row in response.data}
        self.assertEqual(uuids, {self.bound.uuid.hex})

    def test_customer_detail_404_for_unbound(self):
        pat = _create_pat(
            self.staff,
            scopes=[PermissionEnum.LIST_PROJECTS.value],
            bindings=[self.bound],
        )
        self.client.credentials(HTTP_AUTHORIZATION=_auth_header(pat))
        response = self.client.get(f"/api/customers/{self.unbound.uuid.hex}/")
        self.assertEqual(response.status_code, status.HTTP_404_NOT_FOUND)

    def test_unscoped_pat_sees_all(self):
        pat = _create_pat(
            self.staff,
            scopes=[PermissionEnum.LIST_PROJECTS.value],
            bindings=[],  # no bindings → legacy behaviour
        )
        self.client.credentials(HTTP_AUTHORIZATION=_auth_header(pat))
        response = self.client.get("/api/customers/")
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        uuids = {row["uuid"] for row in response.data}
        self.assertIn(self.bound.uuid.hex, uuids)
        self.assertIn(self.unbound.uuid.hex, uuids)

    def test_session_auth_unaffected(self):
        self.client.force_authenticate(user=self.staff)
        response = self.client.get("/api/customers/")
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        uuids = {row["uuid"] for row in response.data}
        self.assertIn(self.bound.uuid.hex, uuids)
        self.assertIn(self.unbound.uuid.hex, uuids)


@override_config(PAT_ENABLED=True)
class PATListFilterDescendantTest(test.APITestCase):
    """Project bound to Customer must reach descendant Projects, not siblings."""

    def setUp(self):
        self.staff = structure_factories.UserFactory(
            is_staff=True, can_use_personal_access_tokens=True
        )
        Token.objects.get_or_create(user=self.staff)
        self.bound_customer = structure_factories.CustomerFactory()
        self.other_customer = structure_factories.CustomerFactory()
        self.bound_project = structure_factories.ProjectFactory(
            customer=self.bound_customer
        )
        self.other_project = structure_factories.ProjectFactory(
            customer=self.other_customer
        )

    def _setup_pat(self, bindings):
        pat = _create_pat(
            self.staff,
            scopes=[PermissionEnum.LIST_PROJECTS.value],
            bindings=bindings,
        )
        self.client.credentials(HTTP_AUTHORIZATION=_auth_header(pat))
        return pat

    def test_customer_binding_reaches_project(self):
        self._setup_pat([self.bound_customer])
        response = self.client.get("/api/projects/")
        uuids = {row["uuid"] for row in response.data}
        self.assertEqual(uuids, {self.bound_project.uuid.hex})

    def test_project_binding_does_not_reach_parent_customer(self):
        """Decision B: binding to a child must not authorise its parent."""
        self._setup_pat([self.bound_project])
        response = self.client.get("/api/customers/")
        # The project's customer is NOT reachable from a project binding.
        self.assertEqual(response.data, [])

    def test_project_binding_reaches_self(self):
        self._setup_pat([self.bound_project])
        response = self.client.get("/api/projects/")
        uuids = {row["uuid"] for row in response.data}
        self.assertEqual(uuids, {self.bound_project.uuid.hex})


@override_config(PAT_ENABLED=True)
class PATListFilterMarketplaceTest(test.APITestCase):
    def setUp(self):
        self.staff = structure_factories.UserFactory(
            is_staff=True, can_use_personal_access_tokens=True
        )
        Token.objects.get_or_create(user=self.staff)
        self.bound_customer = structure_factories.CustomerFactory()
        self.other_customer = structure_factories.CustomerFactory()
        self.bound_offering = marketplace_factories.OfferingFactory(
            customer=self.bound_customer, shared=True
        )
        self.other_offering = marketplace_factories.OfferingFactory(
            customer=self.other_customer, shared=True
        )

    def test_offering_list_filtered_by_customer_binding(self):
        pat = _create_pat(
            self.staff,
            scopes=[PermissionEnum.LIST_PROJECTS.value],
            bindings=[self.bound_customer],
        )
        self.client.credentials(HTTP_AUTHORIZATION=_auth_header(pat))
        response = self.client.get("/api/marketplace-public-offerings/")
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        uuids = {row["uuid"] for row in response.data}
        self.assertIn(self.bound_offering.uuid.hex, uuids)
        self.assertNotIn(self.other_offering.uuid.hex, uuids)


@override_config(PAT_ENABLED=True)
class PATListFilterCallTest(test.APITestCase):
    """Calls have no ancestor inheritance — a customer binding mustn't reach them."""

    def setUp(self):
        from waldur_mastermind.proposal.enums import CallStates

        self.staff = structure_factories.UserFactory(
            is_staff=True, can_use_personal_access_tokens=True
        )
        Token.objects.get_or_create(user=self.staff)
        self.cmo = proposal_factories.CallManagingOrganisationFactory()
        # PublicCallViewSet only lists ACTIVE/ARCHIVED calls.
        self.call_a = proposal_factories.CallFactory(
            manager=self.cmo, state=CallStates.ACTIVE
        )
        self.call_b = proposal_factories.CallFactory(
            manager=self.cmo, state=CallStates.ACTIVE
        )

    def test_call_list_filtered_by_call_binding(self):
        pat = _create_pat(
            self.staff,
            scopes=[PermissionEnum.LIST_PROJECTS.value],
            bindings=[self.call_a],
        )
        self.client.credentials(HTTP_AUTHORIZATION=_auth_header(pat))
        response = self.client.get("/api/proposal-public-calls/")
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        uuids = {row["uuid"] for row in response.data}
        self.assertEqual(uuids, {self.call_a.uuid.hex})

    def test_call_organizer_binding_does_not_reach_call(self):
        """get_scope_ancestors doesn't walk Call->manager, so neither do we."""
        pat = _create_pat(
            self.staff,
            scopes=[PermissionEnum.LIST_PROJECTS.value],
            bindings=[self.cmo],
        )
        self.client.credentials(HTTP_AUTHORIZATION=_auth_header(pat))
        response = self.client.get("/api/proposal-public-calls/")
        # Manager binding doesn't reach calls — empty result.
        self.assertEqual(response.data, [])


@override_config(PAT_ENABLED=True)
class PATListFilterUnregisteredModelTest(test.APITestCase):
    """Endpoints whose model has no registered rule fall through unchanged."""

    def setUp(self):
        self.staff = structure_factories.UserFactory(
            is_staff=True, can_use_personal_access_tokens=True
        )
        Token.objects.get_or_create(user=self.staff)
        self.bound_customer = structure_factories.CustomerFactory()

    def test_users_endpoint_not_filtered(self):
        # users have no PAT-filter rule; the scoped PAT shouldn't hide them.
        pat = _create_pat(
            self.staff,
            scopes=[PermissionEnum.LIST_PROJECTS.value],
            bindings=[self.bound_customer],
        )
        self.client.credentials(HTTP_AUTHORIZATION=_auth_header(pat))
        response = self.client.get("/api/users/")
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        # The staff user themselves should be visible.
        usernames = {row["username"] for row in response.data}
        self.assertIn(self.staff.username, usernames)


@override_config(PAT_ENABLED=True)
class PATListFilterResourceProviderCustomerLeakTest(test.APITestCase):
    """Mirror `get_scope_ancestors` exactly for Resource / ResourceProject.

    ``Resource.customer`` returns ``project.customer`` (the *consumer*),
    not ``offering.customer`` (the *provider*). So a binding to the
    provider customer must NOT surface resources whose consumer lives
    elsewhere — otherwise the list endpoint discloses resources the
    permission check (and detail endpoint) would deny.
    """

    def setUp(self):
        from waldur_mastermind.marketplace import models as marketplace_models

        self.staff = structure_factories.UserFactory(
            is_staff=True, can_use_personal_access_tokens=True
        )
        Token.objects.get_or_create(user=self.staff)

        # Provider side: customer + offering they sell.
        self.provider_customer = structure_factories.CustomerFactory(name="provider")
        self.provider_offering = marketplace_factories.OfferingFactory(
            customer=self.provider_customer, shared=True
        )
        # Consumer side: completely separate customer + project.
        self.consumer_customer = structure_factories.CustomerFactory(name="consumer")
        self.consumer_project = structure_factories.ProjectFactory(
            customer=self.consumer_customer
        )
        # The leaky resource: provider's offering, consumed by an unrelated
        # customer's project.
        self.leaky_resource = marketplace_factories.ResourceFactory(
            offering=self.provider_offering,
            project=self.consumer_project,
        )
        # A second resource whose consumer project lives under the provider's
        # own customer — this one IS reachable from the provider-customer
        # binding via ``project.customer``. Pairing it with the leak case
        # makes the negative assertion below meaningful.
        self.provider_owned_project = structure_factories.ProjectFactory(
            customer=self.provider_customer
        )
        self.legitimate_resource = marketplace_factories.ResourceFactory(
            offering=self.provider_offering,
            project=self.provider_owned_project,
        )
        # ResourceProject under the leaky resource for the RP-level check.
        self.leaky_resource_project = marketplace_models.ResourceProject.objects.create(
            resource=self.leaky_resource,
            name="leaky-rp",
        )

    def _pat(self, bindings):
        pat = _create_pat(
            self.staff,
            scopes=[PermissionEnum.LIST_PROJECTS.value],
            bindings=bindings,
        )
        self.client.credentials(HTTP_AUTHORIZATION=_auth_header(pat))
        return pat

    def test_provider_customer_binding_does_not_leak_resources(self):
        """Binding to the offering owner must NOT surface resources with a
        different consumer project. Matches ``get_scope_ancestors`` which
        only walks ``resource.project.customer``, not ``resource.offering.customer``."""
        self._pat([self.provider_customer])
        response = self.client.get("/api/marketplace-resources/")
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        uuids = {row["uuid"] for row in response.data}
        # The resource consumed by the provider's own project is reachable
        # via the project.customer ancestor — legitimate hit.
        self.assertIn(self.legitimate_resource.uuid.hex, uuids)
        # The leaky resource (consumed by an unrelated customer) MUST NOT
        # appear; otherwise the filter is broader than the permission check.
        self.assertNotIn(self.leaky_resource.uuid.hex, uuids)

    def test_consumer_customer_binding_surfaces_resource(self):
        """Sanity: binding to the consumer's customer DOES reach the resource."""
        self._pat([self.consumer_customer])
        response = self.client.get("/api/marketplace-resources/")
        uuids = {row["uuid"] for row in response.data}
        self.assertIn(self.leaky_resource.uuid.hex, uuids)
        self.assertNotIn(self.legitimate_resource.uuid.hex, uuids)

    def test_resource_project_provider_leak_denied(self):
        """Same leak applies to ResourceProject — its ancestor chain goes
        through ``resource.project.customer``, not ``resource.offering.customer``."""
        self._pat([self.provider_customer])
        response = self.client.get("/api/marketplace-resource-projects/")
        # The endpoint may or may not be available in test_settings_local;
        # accept either a clean 200 with no leak or a 404 (route missing).
        if response.status_code == status.HTTP_404_NOT_FOUND:
            self.skipTest("marketplace-resource-projects endpoint not routed")
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        uuids = {row["uuid"] for row in response.data}
        self.assertNotIn(self.leaky_resource_project.uuid.hex, uuids)


@override_config(PAT_ENABLED=True)
class PATMalformedBindingsTest(test.APITestCase):
    """A PAT row written with malformed ``allowed_scopes`` (e.g. via admin or
    a buggy migration) must not 500 every PAT-auth'd request."""

    def setUp(self):
        self.staff = structure_factories.UserFactory(
            is_staff=True, can_use_personal_access_tokens=True
        )
        Token.objects.get_or_create(user=self.staff)

    def test_missing_keys_are_skipped(self):
        from django.contrib.contenttypes.models import ContentType

        customer_ct = ContentType.objects.get_for_model(
            structure_factories.CustomerFactory._meta.model
        )
        bound = structure_factories.CustomerFactory()
        # Direct DB write — bypass the create serializer to simulate a row
        # that was put in by admin/migration.
        full_token, prefix, token_hash = PersonalAccessToken.generate_token(
            timezone.now() + timedelta(days=30)
        )
        pat = PersonalAccessToken.objects.create(
            user=self.staff,
            name="malformed",
            token_prefix=prefix,
            token_hash=token_hash,
            scopes=[PermissionEnum.LIST_PROJECTS.value],
            allowed_scopes=[
                {"object_id": bound.id},  # missing content_type_id
                {"content_type_id": customer_ct.id},  # missing object_id
                {"content_type_id": customer_ct.id, "object_id": bound.id},  # ok
            ],
            expires_at=timezone.now() + timedelta(days=30),
        )
        pat._plaintext_token = full_token

        self.client.credentials(HTTP_AUTHORIZATION=_auth_header(pat))
        response = self.client.get("/api/customers/")
        # Should NOT 500 — bad entries skipped, good entry applied.
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        uuids = {row["uuid"] for row in response.data}
        self.assertEqual(uuids, {bound.uuid.hex})
