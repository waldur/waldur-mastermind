"""Order validation lets staff, and for some checks support, past limits that
hold for everyone else. A staff or support user's personal access token without
`STAFF.ACCESS` or `SUPPORT.ACCESS` acts as the user without that role (#973):
it is held to the limit, as a non-staff owner of the consumer organization is.

The staff and support users here also own the consumer organization, so the
order itself is allowed through `ORDER.CREATE`, which every token carries.
"""

from rest_framework import status, test

from waldur_core.configuration.testing import override_config
from waldur_core.permissions.enums import PermissionEnum, RoleEnum
from waldur_core.permissions.fixtures import CustomerRole
from waldur_core.permissions.tests.pat_utils import STAFF, SUPPORT, use_pat
from waldur_core.structure.tests import factories as structure_factories
from waldur_core.structure.tests import fixtures as structure_fixtures
from waldur_mastermind.marketplace import models
from waldur_mastermind.marketplace.enums import OfferingStates, ResourceStates
from waldur_mastermind.marketplace.tests import factories

CREATE_ORDER = PermissionEnum.CREATE_ORDER.value
TERMINATE = PermissionEnum.TERMINATE_RESOURCE.value

enabled = override_config(PAT_ENABLED=True, ENFORCE_USER_CONSENT_FOR_OFFERINGS=True)


class OrderValidationTokenScopeMixin:
    """A subclass builds the offering and says how the limit refuses an order
    (``refused_status``) and whether support is exempt from it, as staff are."""

    refused_status = status.HTTP_400_BAD_REQUEST
    allowed_status = status.HTTP_201_CREATED
    support_is_exempt = False
    scopes = (CREATE_ORDER,)

    def setUp(self):
        self.fixture = structure_fixtures.ProjectFixture()
        CustomerRole.OWNER.add_permission(PermissionEnum.CREATE_ORDER)
        # As permissions.yaml grants it. A token without the staff or support
        # scope reads as the user without that role (#987), so the staff and
        # support owners find the project only through this grant.
        CustomerRole.OWNER.add_permission(PermissionEnum.LIST_PROJECTS)
        self.staff = structure_factories.UserFactory(is_staff=True)
        self.support = structure_factories.UserFactory(is_support=True)
        for user in (self.staff, self.support):
            self.fixture.customer.add_user(user, CustomerRole.OWNER)
        self.offering = self.make_offering()
        self.plan = factories.PlanFactory(offering=self.offering)

    def make_offering(self):
        raise NotImplementedError

    def submit(self):
        return self.client.post(
            factories.OrderFactory.get_list_url(),
            {
                "project": structure_factories.ProjectFactory.get_url(
                    self.fixture.project
                ),
                "offering": factories.OfferingFactory.get_public_url(self.offering),
                "plan": factories.PlanFactory.get_public_url(self.plan),
                "attributes": {"name": "resource"},
            },
            format="json",
        )

    def assert_allowed(self):
        response = self.submit()
        self.assertEqual(response.status_code, self.allowed_status, response.data)
        return response

    def assert_refused(self):
        response = self.submit()
        self.assertEqual(response.status_code, self.refused_status, response.data)
        # Refused by the limit under test, not because the caller cannot see
        # the project or the offering.
        self.assertNotIn("project", response.data)
        self.assertNotIn("offering", response.data)
        return response

    def test_owner_session_is_refused(self):
        self.client.force_authenticate(self.fixture.owner)
        self.assert_refused()

    def test_owner_token_is_refused(self):
        use_pat(self.client, self.fixture.owner, *self.scopes)
        self.assert_refused()

    def test_staff_session_overrides(self):
        self.client.force_authenticate(self.staff)
        self.assert_allowed()

    def test_staff_token_with_staff_scope_overrides(self):
        use_pat(self.client, self.staff, STAFF, *self.scopes)
        self.assert_allowed()

    def test_staff_token_without_staff_scope_is_refused(self):
        use_pat(self.client, self.staff, *self.scopes)
        self.assert_refused()

    def test_support_session(self):
        self.client.force_authenticate(self.support)
        if self.support_is_exempt:
            self.assert_allowed()
        else:
            self.assert_refused()

    def test_support_token_with_support_scope(self):
        use_pat(self.client, self.support, SUPPORT, *self.scopes)
        if self.support_is_exempt:
            self.assert_allowed()
        else:
            self.assert_refused()

    def test_support_token_without_support_scope_is_refused(self):
        use_pat(self.client, self.support, *self.scopes)
        self.assert_refused()


@enabled
class OrganizationGroupTokenScopeTest(OrderValidationTokenScopeMixin, test.APITestCase):
    """`validate_public_offering`: the offering is limited to organization
    groups the consumer is not in. Only staff override it."""

    def make_offering(self):
        offering = factories.OfferingFactory(state=OfferingStates.ACTIVE, shared=True)
        offering.organization_groups.add(structure_factories.OrganizationGroupFactory())
        return offering


@enabled
class PrivateOfferingTokenScopeTest(OrderValidationTokenScopeMixin, test.APITestCase):
    """`validate_private_offering`: a private offering of another organization.
    Only staff override it."""

    def make_offering(self):
        return factories.OfferingFactory(state=OfferingStates.ACTIVE, shared=False)


@enabled
class RestrictedOfferingTokenScopeTest(
    OrderValidationTokenScopeMixin, test.APITestCase
):
    """`check_offering_restriction`: the offering is restricted to project
    administrators, a role nobody here holds. Staff and support outrank it."""

    refused_status = status.HTTP_403_FORBIDDEN
    support_is_exempt = True

    def make_offering(self):
        return factories.OfferingFactory(
            state=OfferingStates.ACTIVE,
            shared=True,
            plugin_options={"restricted_to_roles": [RoleEnum.PROJECT_ADMIN]},
        )

    def test_staff_token_with_support_scope_acts_as_support(self):
        use_pat(self.client, self.staff, SUPPORT, *self.scopes)
        self.assert_allowed()


@enabled
class TermsOfServiceTokenScopeTest(OrderValidationTokenScopeMixin, test.APITestCase):
    """`OrderCreateSerializer._validate_terms_of_service`: the offering has
    Terms of Service nobody here has accepted, and the order does not accept
    them. Staff and support are exempt."""

    support_is_exempt = True

    def make_offering(self):
        offering = factories.OfferingFactory(state=OfferingStates.ACTIVE, shared=True)
        models.OfferingTermsOfService.objects.create(
            offering=offering, terms_of_service="Terms", version="1", is_active=True
        )
        return offering

    def assert_allowed(self):
        super().assert_allowed()
        self.assertFalse(models.UserOfferingConsent.objects.exists())

    def assert_refused(self):
        response = super().assert_refused()
        self.assertIn("Terms of service", str(response.data))


@enabled
class TerminateTermsOfServiceTokenScopeTest(
    OrderValidationTokenScopeMixin, test.APITestCase
):
    """`confirm_order_request_user_has_offering_consent`, which every order
    passes through, reached here by terminating an existing resource of an
    offering that creates offering users and has Terms of Service nobody here
    has accepted. Staff and support are exempt."""

    allowed_status = status.HTTP_200_OK
    support_is_exempt = True
    scopes = (TERMINATE,)

    def setUp(self):
        super().setUp()
        CustomerRole.OWNER.add_permission(PermissionEnum.TERMINATE_RESOURCE)
        CustomerRole.OWNER.add_permission(PermissionEnum.LIST_RESOURCES)
        self.resource = factories.ResourceFactory(
            project=self.fixture.project,
            offering=self.offering,
            plan=self.plan,
            state=ResourceStates.OK,
        )

    def make_offering(self):
        offering = factories.OfferingFactory(
            state=OfferingStates.ACTIVE,
            shared=True,
            plugin_options={"service_provider_can_create_offering_user": True},
        )
        models.OfferingTermsOfService.objects.create(
            offering=offering, terms_of_service="Terms", version="1", is_active=True
        )
        return offering

    def submit(self):
        return self.client.post(
            factories.ResourceFactory.get_url(self.resource, "terminate"),
            {},
            format="json",
        )

    def assert_allowed(self):
        super().assert_allowed()
        self.assertTrue(models.Order.objects.filter(resource=self.resource).exists())
        self.assertFalse(models.UserOfferingConsent.objects.exists())

    def assert_refused(self):
        response = super().assert_refused()
        self.assertIn("Terms of Service", str(response.data))
        self.assertFalse(models.Order.objects.filter(resource=self.resource).exists())
