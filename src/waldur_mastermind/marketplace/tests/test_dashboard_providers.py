from constance.test.unittest import override_config
from rest_framework import test

from waldur_core.permissions.enums import PermissionEnum
from waldur_core.permissions.fixtures import CustomerRole
from waldur_core.structure.tests import factories as structure_factories
from waldur_core.structure.tests import fixtures as structure_fixtures
from waldur_mastermind.marketplace import models as marketplace_models
from waldur_mastermind.marketplace.enums import OrderStates, ResourceStates
from waldur_mastermind.marketplace.tests.factories import (
    OfferingFactory,
    OfferingUserFactory,
    OrderFactory,
    ResourceFactory,
)


class DashboardOrdersPendingApprovalProviderTest(test.APITestCase):
    def setUp(self):
        self.url = structure_factories.UserFactory.get_list_url(
            "dashboard-pending-actions"
        )
        self.fixture = structure_fixtures.ProjectFixture()
        self.owner = self.fixture.owner

    def test_emits_orders_pending_approval_item(self):
        OrderFactory(
            project=self.fixture.project,
            created_by=self.owner,
            state=OrderStates.PENDING_PROVIDER,
        )
        OrderFactory(
            project=self.fixture.project,
            created_by=self.owner,
            state=OrderStates.PENDING_PROVIDER,
        )

        self.client.force_authenticate(self.owner)
        response = self.client.get(self.url)
        items = [
            item for item in response.data if item["type"] == "orders_pending_approval"
        ]
        self.assertEqual(len(items), 1)
        self.assertEqual(items[0]["count"], 2)
        self.assertEqual(items[0]["variant"], "warning")


class DashboardErredResourcesProviderTest(test.APITestCase):
    def setUp(self):
        self.url = structure_factories.UserFactory.get_list_url(
            "dashboard-pending-actions"
        )
        self.fixture = structure_fixtures.ProjectFixture()
        self.owner = self.fixture.owner
        CustomerRole.OWNER.add_permission(PermissionEnum.LIST_RESOURCES)

    def test_emits_resources_erred_item(self):
        ResourceFactory(project=self.fixture.project, state=ResourceStates.ERRED)
        ResourceFactory(project=self.fixture.project, state=ResourceStates.ERRED)
        ResourceFactory(project=self.fixture.project, state=ResourceStates.OK)

        self.client.force_authenticate(self.owner)
        response = self.client.get(self.url)
        items = [item for item in response.data if item["type"] == "resources_erred"]
        self.assertEqual(len(items), 1)
        self.assertEqual(items[0]["count"], 2)
        self.assertEqual(items[0]["variant"], "error")

    def test_role_without_list_resources_gets_no_count(self):
        # CUSTOMER.READER ships with no permissions at all and sees nothing at
        # /api/marketplace-resources/, so the badge must stay silent too.
        ResourceFactory(project=self.fixture.project, state=ResourceStates.ERRED)
        reader = structure_factories.UserFactory()
        self.fixture.customer.add_user(reader, CustomerRole.READER)

        self.client.force_authenticate(reader)
        response = self.client.get(self.url)
        items = [item for item in response.data if item["type"] == "resources_erred"]
        self.assertEqual(items, [])


@override_config(ENFORCE_USER_CONSENT_FOR_OFFERINGS=True)
class DashboardTOSAcceptanceProviderTest(test.APITestCase):
    def setUp(self):
        self.url = structure_factories.UserFactory.get_list_url(
            "dashboard-pending-actions"
        )
        self.fixture = structure_fixtures.ProjectFixture()
        self.owner = self.fixture.owner
        CustomerRole.OWNER.add_permission(PermissionEnum.LIST_RESOURCES)

    def create_terms(self, plugin_options=None, **kwargs):
        # service_provider_can_create_offering_user is one of the four gates in
        # check_tos_consent_permission, so an offering without it is never
        # actually blocked on consent and must not be prompted for either.
        offering = OfferingFactory(
            plugin_options=(
                {"service_provider_can_create_offering_user": True}
                if plugin_options is None
                else plugin_options
            )
        )
        marketplace_models.OfferingTermsOfService.objects.create(
            offering=offering,
            terms_of_service="Some legal text",
            is_active=True,
            **kwargs,
        )
        return offering

    def get_items(self, user=None):
        self.client.force_authenticate(user or self.owner)
        response = self.client.get(self.url)
        return [
            item for item in response.data if item["type"] == "tos_acceptance_required"
        ]

    def test_emits_tos_acceptance_item_per_unaccepted_offering(self):
        offering = self.create_terms()
        OfferingUserFactory(offering=offering, user=self.owner)

        items = self.get_items()
        self.assertEqual(len(items), 1)
        self.assertIn(offering.name, items[0]["title"])

    def test_emits_tos_item_when_consent_is_for_an_older_version(self):
        # During the re-consent grace period the old consent is still active,
        # and that window is exactly when the prompt needs to show.
        offering = self.create_terms(version="2", requires_reconsent=True)
        OfferingUserFactory(offering=offering, user=self.owner)
        marketplace_models.UserOfferingConsent.objects.create(
            user=self.owner, offering=offering, version="1"
        )

        items = self.get_items()
        self.assertEqual(len(items), 1)
        self.assertIn(offering.name, items[0]["title"])

    def test_skips_tos_item_when_reconsent_is_not_required(self):
        # A provider that edits its terms and bumps the version without asking
        # for re-consent must not re-prompt users who already consented — the
        # same gate check_tos_consent and send_tos_reconsent_notification apply.
        offering = self.create_terms(version="2", requires_reconsent=False)
        OfferingUserFactory(offering=offering, user=self.owner)
        marketplace_models.UserOfferingConsent.objects.create(
            user=self.owner, offering=offering, version="1"
        )

        self.assertEqual(self.get_items(), [])

    def test_skips_tos_item_when_already_consented(self):
        offering = self.create_terms()
        OfferingUserFactory(offering=offering, user=self.owner)
        marketplace_models.UserOfferingConsent.objects.create(
            user=self.owner, offering=offering
        )

        self.assertEqual(self.get_items(), [])

    def test_skips_offering_the_user_only_sees_a_colleagues_resource_of(self):
        # "Offerings I can see a resource of" pulled in offerings the user
        # never used — including terminated ones — and there is no way to stop
        # being prompted about them short of accepting the terms.
        offering = self.create_terms()
        ResourceFactory(
            project=self.fixture.project,
            offering=offering,
            state=ResourceStates.TERMINATED,
        )

        self.assertEqual(self.get_items(), [])

    @override_config(ENFORCE_USER_CONSENT_FOR_OFFERINGS=False)
    def test_skips_tos_item_when_consent_is_not_enforced(self):
        offering = self.create_terms()
        OfferingUserFactory(offering=offering, user=self.owner)

        self.assertEqual(self.get_items(), [])

    def test_skips_tos_item_for_staff(self):
        staff = structure_factories.UserFactory(is_staff=True)
        offering = self.create_terms()
        OfferingUserFactory(offering=offering, user=staff)

        self.assertEqual(self.get_items(staff), [])

    def test_skips_offering_that_cannot_create_offering_users(self):
        # An OfferingUser row is not proof the plugin option is on: the rancher
        # handler, the remote sync task and set_offerings_username all create
        # them without it. check_tos_consent_permission returns early for such
        # an offering, so prompting for it names terms nothing enforces.
        offering = self.create_terms(plugin_options={})
        OfferingUserFactory(offering=offering, user=self.owner)

        self.assertEqual(self.get_items(), [])
