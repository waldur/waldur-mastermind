from ddt import data, ddt
from rest_framework import status
from rest_framework.reverse import reverse

from waldur_mastermind.marketplace import models as marketplace_models
from waldur_mastermind.marketplace.enums import (
    OPENSTACK_INSTANCE_OFFERING,
    OPENSTACK_VOLUME_OFFERING,
    OfferingStates,
)
from waldur_mastermind.marketplace.tests import factories as marketplace_factories
from waldur_openstack.tests.fixtures import OpenStackFixture

from .utils import BaseOpenStackTest

LIST_URL = reverse("marketplace-openstack-duplicate-offering-list")


class DuplicateOfferingsApiTest(BaseOpenStackTest):
    def setUp(self):
        super().setUp()
        self.fixture = OpenStackFixture()
        self.tenant = self.fixture.tenant

    def _offering(self, type=OPENSTACK_INSTANCE_OFFERING, scope=None, **kwargs):
        return marketplace_factories.OfferingFactory(
            type=type,
            scope=scope or self.tenant,
            customer=self.fixture.customer,
            project=self.fixture.project,
            state=kwargs.pop("state", OfferingStates.ACTIVE),
            **kwargs,
        )

    def _get(self, user):
        self.client.force_authenticate(user)
        return self.client.get(LIST_URL)

    def test_staff_sees_duplicate_group_with_keeper_flag(self):
        keeper = self._offering()
        marketplace_factories.ResourceFactory(
            scope=self.fixture.instance,
            project=self.fixture.project,
            offering=keeper,
        )
        self._offering()  # empty duplicate

        response = self._get(self.fixture.staff)

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(len(response.data), 1)
        row = response.data[0]
        self.assertEqual(row["tenant_id"], self.tenant.id)
        self.assertEqual(row["offering_type"], OPENSTACK_INSTANCE_OFFERING)
        self.assertEqual(row["recommended_keeper_id"], keeper.id)
        self.assertEqual(len(row["candidates"]), 2)
        keeper_candidate = next(c for c in row["candidates"] if c["id"] == keeper.id)
        self.assertTrue(keeper_candidate["is_recommended_keeper"])
        self.assertEqual(keeper_candidate["active_resources"], 1)

    def test_support_can_access(self):
        self._offering()
        self._offering()
        response = self._get(self.fixture.global_support)
        self.assertEqual(response.status_code, status.HTTP_200_OK)

    def test_non_staff_is_forbidden(self):
        self._offering()
        self._offering()
        response = self._get(self.fixture.owner)
        self.assertEqual(response.status_code, status.HTTP_403_FORBIDDEN)

    def test_orphan_resources_are_counted(self):
        # fixture.instance is an Instance in the tenant with no marketplace
        # Resource — exactly an orphan the ambiguous offering can't heal.
        self.assertIsNotNone(self.fixture.instance)
        self._offering()
        self._offering()

        response = self._get(self.fixture.staff)

        row = response.data[0]
        self.assertEqual(row["orphan_count"], 1)

    def test_no_duplicates_returns_empty(self):
        self._offering()
        response = self._get(self.fixture.staff)
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(len(response.data), 0)

    def test_both_types_yield_two_rows(self):
        self._offering(type=OPENSTACK_INSTANCE_OFFERING)
        self._offering(type=OPENSTACK_INSTANCE_OFFERING)
        self._offering(type=OPENSTACK_VOLUME_OFFERING)
        self._offering(type=OPENSTACK_VOLUME_OFFERING)

        response = self._get(self.fixture.staff)

        self.assertEqual(len(response.data), 2)
        types = {row["offering_type"] for row in response.data}
        self.assertEqual(
            types, {OPENSTACK_INSTANCE_OFFERING, OPENSTACK_VOLUME_OFFERING}
        )


REMEDIATE_URL = reverse("marketplace-openstack-duplicate-offering-remediate")


@ddt
class DuplicateOfferingsRemediateTest(BaseOpenStackTest):
    """The staff action that collapses a duplicate group onto its keeper."""

    def setUp(self):
        super().setUp()
        self.fixture = OpenStackFixture()
        self.tenant = self.fixture.tenant
        # Keeper owns an active resource, so pick_keeper_offering prefers it.
        self.keeper = self._offering()
        marketplace_factories.ResourceFactory(
            scope=self.fixture.instance,
            project=self.fixture.project,
            offering=self.keeper,
            state=marketplace_models.Resource.States.OK,
        )
        self.duplicate = self._offering()

    def _offering(self, type=OPENSTACK_INSTANCE_OFFERING, **kwargs):
        return marketplace_factories.OfferingFactory(
            type=type,
            scope=self.tenant,
            customer=self.fixture.customer,
            project=self.fixture.project,
            state=kwargs.pop("state", OfferingStates.ACTIVE),
            **kwargs,
        )

    def _post(self, user, **payload):
        self.client.force_authenticate(user)
        body = {
            "tenant_id": self.tenant.id,
            "offering_type": OPENSTACK_INSTANCE_OFFERING,
        }
        body.update(payload)
        return self.client.post(REMEDIATE_URL, body)

    def _attach_resource_with_history(self, offering):
        """A resource on ``offering`` with a plan period, usage and quota."""
        plan = marketplace_factories.PlanFactory(offering=offering, name="default")
        component = marketplace_factories.OfferingComponentFactory(
            offering=offering, type="cpu"
        )
        resource = marketplace_factories.ResourceFactory(
            project=self.fixture.project,
            offering=offering,
            plan=plan,
            state=marketplace_models.Resource.States.OK,
        )
        plan_period = marketplace_factories.ResourcePlanPeriodFactory(
            resource=resource, plan=plan
        )
        usage = marketplace_factories.ComponentUsageFactory(
            resource=resource, component=component, plan_period=plan_period
        )
        quota = marketplace_models.ComponentQuota.objects.create(
            resource=resource, component=component, limit=10, usage=1
        )
        return resource, plan_period, usage, quota

    def _mirror_on_keeper(self):
        """Give the keeper a matching plan name and component type."""
        marketplace_factories.PlanFactory(offering=self.keeper, name="default")
        marketplace_factories.OfferingComponentFactory(offering=self.keeper, type="cpu")

    # -- permissions -----------------------------------------------------

    def test_staff_can_remediate(self):
        response = self._post(self.fixture.staff)
        self.assertEqual(response.status_code, status.HTTP_200_OK)

    @data("global_support", "owner")
    def test_non_staff_can_not_remediate(self, user):
        response = self._post(getattr(self.fixture, user))
        self.assertEqual(response.status_code, status.HTTP_403_FORBIDDEN)

    # -- dry run ---------------------------------------------------------

    def test_dry_run_is_the_default_and_changes_nothing(self):
        response = self._post(self.fixture.staff)
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertTrue(response.data["dry_run"])
        self.assertEqual(response.data["keeper_id"], self.keeper.id)
        self.assertTrue(
            marketplace_models.Offering.objects.filter(id=self.duplicate.id).exists()
        )

    def test_dry_run_reports_what_would_move(self):
        self._mirror_on_keeper()
        self._attach_resource_with_history(self.duplicate)

        response = self._post(self.fixture.staff)

        plan = response.data["duplicates"][0]
        self.assertEqual(plan["action"], "merge")
        self.assertEqual(plan["resource_count"], 1)
        self.assertEqual(plan["plan_period_count"], 1)
        self.assertEqual(plan["component_usage_count"], 1)
        self.assertEqual(plan["component_quota_count"], 1)
        self.assertEqual(plan["blockers"], [])

    # -- applying --------------------------------------------------------

    def test_empty_duplicate_is_deleted(self):
        response = self._post(self.fixture.staff, dry_run=False)
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertFalse(
            marketplace_models.Offering.objects.filter(id=self.duplicate.id).exists()
        )
        self.assertTrue(
            marketplace_models.Offering.objects.filter(id=self.keeper.id).exists()
        )

    def test_merge_preserves_billing_and_usage_history(self):
        self._mirror_on_keeper()
        resource, plan_period, usage, quota = self._attach_resource_with_history(
            self.duplicate
        )

        response = self._post(self.fixture.staff, dry_run=False)
        self.assertEqual(response.status_code, status.HTTP_200_OK)

        # The duplicate is gone but nothing it owned was cascaded away.
        self.assertFalse(
            marketplace_models.Offering.objects.filter(id=self.duplicate.id).exists()
        )
        for obj in (resource, plan_period, usage, quota):
            obj.refresh_from_db()
        self.assertEqual(resource.offering, self.keeper)
        self.assertEqual(plan_period.plan.offering, self.keeper)
        self.assertEqual(usage.component.offering, self.keeper)
        self.assertEqual(quota.component.offering, self.keeper)

    # -- blockers --------------------------------------------------------

    def test_merge_is_blocked_when_keeper_lacks_matching_plan(self):
        # Keeper has the component but no plan named "default", so the
        # non-nullable ResourcePlanPeriod.plan cannot be re-pointed.
        marketplace_factories.OfferingComponentFactory(offering=self.keeper, type="cpu")
        resource, *_ = self._attach_resource_with_history(self.duplicate)

        response = self._post(self.fixture.staff, dry_run=False)

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertTrue(response.data["blockers"])
        # Nothing was touched.
        self.assertTrue(
            marketplace_models.Offering.objects.filter(id=self.duplicate.id).exists()
        )
        resource.refresh_from_db()
        self.assertEqual(resource.offering, self.duplicate)

    def test_merge_is_blocked_when_keeper_lacks_matching_component(self):
        marketplace_factories.PlanFactory(offering=self.keeper, name="default")
        resource, *_ = self._attach_resource_with_history(self.duplicate)

        response = self._post(self.fixture.staff, dry_run=False)

        self.assertTrue(response.data["blockers"])
        resource.refresh_from_db()
        self.assertEqual(resource.offering, self.duplicate)

    def test_unknown_group_is_rejected(self):
        self.client.force_authenticate(self.fixture.staff)
        response = self.client.post(
            REMEDIATE_URL,
            {"tenant_id": self.tenant.id, "offering_type": OPENSTACK_VOLUME_OFFERING},
        )
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
