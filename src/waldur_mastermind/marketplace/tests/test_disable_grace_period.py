import datetime

from rest_framework import status, test

from waldur_core.permissions.enums import PermissionEnum
from waldur_core.permissions.fixtures import CustomerRole
from waldur_core.structure.tests import fixtures as structure_fixtures
from waldur_mastermind.marketplace.tests import factories, fixtures


class DisableGracePeriodStaffOnlyTest(test.APITestCase):
    """Only staff may change plugin_options.disable_grace_period."""

    def setUp(self):
        self.fixture = structure_fixtures.ProjectFixture()
        CustomerRole.OWNER.add_permission(PermissionEnum.UPDATE_OFFERING_INTEGRATION)
        self.customer = self.fixture.customer
        factories.ServiceProviderFactory(customer=self.customer)
        self.offering = factories.OfferingFactory(customer=self.customer)

    def _update(self, user, value):
        self.client.force_authenticate(user)
        url = factories.OfferingFactory.get_url(self.offering, "update_integration")
        return self.client.post(
            url, {"plugin_options": {"disable_grace_period": value}}
        )

    def test_staff_can_set_disable_grace_period(self):
        response = self._update(self.fixture.staff, True)
        self.assertEqual(response.status_code, status.HTTP_200_OK, response.data)
        self.offering.refresh_from_db()
        self.assertTrue(self.offering.plugin_options["disable_grace_period"])

    def test_owner_cannot_set_disable_grace_period(self):
        response = self._update(self.fixture.owner, True)
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertIn("plugin_options", response.data)

    def test_owner_unchanged_value_is_accepted(self):
        # Submitting the same value is not a change and must not be blocked.
        self.offering.plugin_options["disable_grace_period"] = True
        self.offering.save()
        response = self._update(self.fixture.owner, True)
        self.assertEqual(response.status_code, status.HTTP_200_OK, response.data)


class ResourceEffectiveEndDateFieldTest(test.APITestCase):
    """The resource serializer exposes resource_effective_end_date: the date this
    resource is actually scheduled to terminate, folding in its own end date, the
    project end date, the grace period, and the grace-disabled offering flag. It
    is the single value the frontend renders instead of re-deriving it."""

    def setUp(self):
        self.fixture = fixtures.MarketplaceFixture()
        project = self.fixture.project
        project.end_date = datetime.date(2020, 1, 1)
        project.grace_period_days = 30
        project.save()
        # Offerings default to no plugin options unless a test opts in.
        self.fixture.offering.plugin_options = {}
        self.fixture.offering.save()

    def _get_effective_end_date(self):
        self.client.force_authenticate(self.fixture.staff)
        url = factories.ResourceFactory.get_url(self.fixture.resource)
        response = self.client.get(url)
        self.assertEqual(response.status_code, status.HTTP_200_OK, response.data)
        value = response.data["resource_effective_end_date"]
        # Normalize with str() so the assertion holds whether the field renders
        # as an ISO string or a date object (depends on REST DATE_FORMAT).
        return None if value is None else str(value)

    def test_grace_disabled_offering_uses_raw_project_end_date(self):
        self.fixture.offering.plugin_options = {"disable_grace_period": True}
        self.fixture.offering.save()
        self.assertEqual(self._get_effective_end_date(), "2020-01-01")

    def test_normal_offering_uses_effective_with_grace_date(self):
        # project end_date (2020-01-01) + 30 day grace
        self.assertEqual(self._get_effective_end_date(), "2020-01-31")

    def test_own_end_date_wins_when_earlier_than_project_date(self):
        self.fixture.resource.end_date = datetime.date(2019, 12, 15)
        self.fixture.resource.save()
        self.assertEqual(self._get_effective_end_date(), "2019-12-15")

    def test_project_date_wins_when_earlier_than_own_end_date(self):
        self.fixture.resource.end_date = datetime.date(2020, 6, 1)
        self.fixture.resource.save()
        self.assertEqual(self._get_effective_end_date(), "2020-01-31")

    def test_null_when_no_project_or_own_end_date(self):
        project = self.fixture.project
        project.end_date = None
        project.save()
        self.assertIsNone(self._get_effective_end_date())
