"""Who may set a resource end date directly, and that no order is ever created.

Exactly one permission writes the date: RESOURCE.SET_END_DATE. Enabling change
requests on an offering does not widen that — it opens the request flow to
everyone else, project managers included, so that reaching the outcome always
goes through someone holding that permission.
"""

from datetime import timedelta

from django.utils import timezone
from rest_framework import status, test

from waldur_core.permissions.enums import PermissionEnum
from waldur_core.permissions.fixtures import CustomerRole, ProjectRole
from waldur_mastermind.marketplace import models, utils
from waldur_mastermind.marketplace.enums import (
    BASIC_OFFERING,
    BillingTypes,
    ResourceStates,
)
from waldur_mastermind.marketplace.tests import factories
from waldur_mastermind.marketplace.tests.fixtures import MarketplaceFixture


class BaseEndDateDirectWriteTest(test.APITestCase):
    def setUp(self):
        self.fixture = MarketplaceFixture()
        CustomerRole.OWNER.add_permission(PermissionEnum.SET_RESOURCE_END_DATE)
        ProjectRole.MANAGER.add_permission(PermissionEnum.UPDATE_RESOURCE_LIMITS)

        self.offering = self.fixture.offering
        self.offering.type = BASIC_OFFERING
        self.offering.plugin_options = {
            "enable_resource_end_date_change_requests": True
        }
        self.offering.save()

        # A monthly LIMIT component: the non-prepaid shape this feature targets.
        factories.OfferingComponentFactory(
            offering=self.offering,
            type="cpu_hours",
            billing_type=BillingTypes.LIMIT,
            is_prepaid=False,
        )

        self.resource = factories.ResourceFactory(
            offering=self.offering,
            plan=self.fixture.plan,
            project=self.fixture.project,
            state=ResourceStates.OK,
            end_date=timezone.now().date() + timedelta(days=30),
        )
        self.url = factories.ResourceFactory.get_url(self.resource, "set_end_date")
        self.new_end_date = timezone.now().date() + timedelta(days=120)

    def set_end_date(self, user, end_date=None):
        self.client.force_authenticate(user)
        return self.client.post(
            self.url, {"end_date": (end_date or self.new_end_date).isoformat()}
        )


class EndDateDirectWriteTest(BaseEndDateDirectWriteTest):
    def test_owner_sets_the_date_immediately(self):
        response = self.set_end_date(self.fixture.owner)

        self.assertEqual(response.status_code, status.HTTP_200_OK, response.data)
        self.resource.refresh_from_db()
        self.assertEqual(self.resource.end_date, self.new_end_date)

    def test_no_order_is_ever_created(self):
        """The whole point of dropping the order path — nothing lands in orders."""
        self.set_end_date(self.fixture.owner)

        self.assertFalse(models.Order.objects.filter(resource=self.resource).exists())

    def test_project_manager_may_not_set_the_date(self):
        """Managing limits does not carry the end date, option or no option."""
        response = self.set_end_date(self.fixture.manager)

        self.assertEqual(response.status_code, status.HTTP_403_FORBIDDEN)
        self.resource.refresh_from_db()
        self.assertNotEqual(self.resource.end_date, self.new_end_date)

    def test_project_manager_may_not_set_the_date_without_the_option(self):
        self.offering.plugin_options = {}
        self.offering.save()

        response = self.set_end_date(self.fixture.manager)

        self.assertEqual(response.status_code, status.HTTP_403_FORBIDDEN)

    def test_owner_may_still_set_the_date_without_the_option(self):
        self.offering.plugin_options = {}
        self.offering.save()

        response = self.set_end_date(self.fixture.owner)

        self.assertEqual(response.status_code, status.HTTP_200_OK, response.data)
        self.resource.refresh_from_db()
        self.assertEqual(self.resource.end_date, self.new_end_date)

    def test_plain_member_may_not_set_the_date(self):
        response = self.set_end_date(self.fixture.member)

        self.assertEqual(response.status_code, status.HTTP_403_FORBIDDEN)

    def test_prepaid_offering_never_accepts_change_requests(self):
        """Prepaid resources extend through renew, whose bounds must not be bypassed."""
        factories.OfferingComponentFactory(
            offering=self.offering,
            type="prepaid_duration",
            billing_type=BillingTypes.ONE_TIME,
            is_prepaid=True,
        )

        self.assertFalse(utils.offering_allows_end_date_change_requests(self.offering))
        # And the endpoint refuses non-staff outright for prepaid resources.
        response = self.set_end_date(self.fixture.owner)
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)


class EndDateChangeRequestOptionValidationTest(test.APITestCase):
    """The option cannot be switched on where it would have no effect."""

    def setUp(self):
        self.fixture = MarketplaceFixture()
        CustomerRole.OWNER.add_permission(PermissionEnum.UPDATE_OFFERING_INTEGRATION)
        self.offering = self.fixture.offering
        self.offering.type = BASIC_OFFERING
        self.offering.save()
        self.url = factories.OfferingFactory.get_url(
            self.offering, "update_integration"
        )

    def enable_the_option(self):
        # The offering belongs to offering_customer, so the provider side owns it.
        self.client.force_authenticate(self.fixture.offering_owner)
        return self.client.post(
            self.url,
            {"plugin_options": {"enable_resource_end_date_change_requests": True}},
            format="json",
        )

    def test_option_can_be_enabled_on_a_non_prepaid_offering(self):
        factories.OfferingComponentFactory(
            offering=self.offering,
            type="cpu_hours",
            billing_type=BillingTypes.LIMIT,
            is_prepaid=False,
        )

        response = self.enable_the_option()

        self.assertEqual(response.status_code, status.HTTP_200_OK, response.data)
        self.offering.refresh_from_db()
        self.assertTrue(
            self.offering.plugin_options["enable_resource_end_date_change_requests"]
        )

    def test_option_is_refused_on_a_prepaid_offering(self):
        """Without this it would save and then be ignored, which reads as a bug."""
        factories.OfferingComponentFactory(
            offering=self.offering,
            type="prepaid_seat",
            billing_type=BillingTypes.ONE_TIME,
            is_prepaid=True,
        )

        response = self.enable_the_option()

        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        self.offering.refresh_from_db()
        self.assertNotIn(
            "enable_resource_end_date_change_requests",
            self.offering.plugin_options or {},
        )
