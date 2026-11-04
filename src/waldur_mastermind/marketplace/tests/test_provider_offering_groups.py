from django.urls import reverse
from rest_framework import status, test

from waldur_core.configuration.testing import override_config
from waldur_core.permissions.enums import RoleEnum
from waldur_core.structure.tests import factories as structure_factories
from waldur_core.structure.tests import fixtures as structure_fixtures
from waldur_mastermind.marketplace.enums import OfferingStates
from waldur_mastermind.marketplace.tests import factories

GROUPS_URL = reverse("marketplace-provider-offering-groups")
PUBLIC_URL = reverse("marketplace-public-offering-list")


class ProviderOfferingGroupsTest(test.APITestCase):
    def setUp(self):
        self.fixture = structure_fixtures.ProjectFixture()
        self.group = structure_factories.OrganizationGroupFactory()

        self.open = self.offering("open")
        self.group_restricted = self.offering("group-restricted")
        self.group_restricted.organization_groups.add(self.group)
        self.role_restricted = self.offering(
            "role-restricted",
            plugin_options={"restricted_to_roles": [RoleEnum.CUSTOMER_OWNER]},
        )
        self.offering("private", shared=False)
        self.offering("draft", state=OfferingStates.DRAFT)

    def offering(self, name, **kwargs):
        kwargs.setdefault("shared", True)
        offering = factories.OfferingFactory(name=name, **kwargs)
        factories.PlanFactory(offering=offering)
        return offering

    def group_names(self, user=None):
        if user:
            self.client.force_authenticate(user)
        response = self.client.get(GROUPS_URL)
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        return sorted(
            offering["offering_name"]
            for group in response.data
            for offering in group["offerings"]
        )

    def public_names(self):
        response = self.client.get(PUBLIC_URL)
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        return {offering["name"] for offering in response.data}

    @override_config(ANONYMOUS_USER_CAN_VIEW_OFFERINGS=False)
    def test_anonymous_is_refused_when_anonymous_view_is_off(self):
        response = self.client.get(GROUPS_URL)
        self.assertEqual(response.status_code, status.HTTP_401_UNAUTHORIZED)

    @override_config(ANONYMOUS_USER_CAN_VIEW_OFFERINGS=True)
    def test_anonymous_sees_public_offerings_when_anonymous_view_is_on(self):
        self.assertEqual(self.group_names(), ["group-restricted", "open"])

    @override_config(RESTRICTED_OFFERING_VISIBILITY_MODE="hide_inaccessible")
    def test_offering_of_organization_group_is_hidden_from_outsiders(self):
        self.assertEqual(self.group_names(self.fixture.user), ["open"])

    @override_config(RESTRICTED_OFFERING_VISIBILITY_MODE="hide_inaccessible")
    def test_offering_of_organization_group_is_shown_to_its_members(self):
        self.fixture.customer.organization_groups.add(self.group)
        self.assertEqual(
            self.group_names(self.fixture.manager), ["group-restricted", "open"]
        )

    def test_offering_restricted_to_roles_is_hidden_from_callers_without_them(self):
        self.assertEqual(
            self.group_names(self.fixture.user), ["group-restricted", "open"]
        )

    def test_offering_restricted_to_roles_is_shown_to_holders(self):
        self.assertEqual(
            self.group_names(self.fixture.owner),
            ["group-restricted", "open", "role-restricted"],
        )

    def test_staff_see_every_active_shared_offering(self):
        self.assertEqual(
            self.group_names(self.fixture.staff),
            ["group-restricted", "open", "role-restricted"],
        )

    @override_config(RESTRICTED_OFFERING_VISIBILITY_MODE="hide_inaccessible")
    def test_groups_show_only_what_public_offerings_show(self):
        for user in (self.fixture.user, self.fixture.owner, self.fixture.staff):
            self.assertTrue(set(self.group_names(user)) <= self.public_names())

    def test_groups_can_be_ordered_by_annotated_totals(self):
        self.client.force_authenticate(self.fixture.owner)
        for ordering in ("total_customers", "total_cost", "total_cost_estimated"):
            with self.subTest(ordering=ordering):
                response = self.client.get(GROUPS_URL, {"o": ordering})
                self.assertEqual(response.status_code, status.HTTP_200_OK)

    @override_config(ANONYMOUS_USER_CAN_VIEW_OFFERINGS=True)
    def test_anonymous_view_opens_groups_alone(self):
        list_url = factories.OfferingFactory.get_list_url()
        self.assertEqual(self.client.get(list_url).status_code, 401)
        self.assertEqual(
            self.client.get(factories.OfferingFactory.get_url(self.open)).status_code,
            401,
        )
