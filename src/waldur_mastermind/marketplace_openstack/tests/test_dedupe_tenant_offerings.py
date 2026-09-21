from decimal import Decimal

from django.test import override_settings
from django.utils import timezone
from rest_framework import status
from rest_framework.reverse import reverse

from waldur_core.core import utils as core_utils
from waldur_mastermind.invoices import models as invoice_models
from waldur_mastermind.invoices.tests import factories as invoice_factories
from waldur_mastermind.marketplace import billing_utils, offering_merge
from waldur_mastermind.marketplace import models as marketplace_models
from waldur_mastermind.marketplace.enums import (
    OPENSTACK_INSTANCE_OFFERING,
    OPENSTACK_TENANT_OFFERING,
    OfferingStates,
    OrderStates,
    ResourceStates,
)
from waldur_mastermind.marketplace.tests import factories as marketplace_factories
from waldur_mastermind.marketplace_openstack import utils
from waldur_openstack.tests.fixtures import OpenStackFixture

from .utils import BaseOpenStackTest

GROUPS_URL = reverse("marketplace-openstack-duplicate-offering-list")
MERGES_URL = reverse("marketplace-offering-merge-list")
MergeStates = marketplace_models.OfferingMerge.States


def merge_action_url(merge_uuid, action):
    return reverse(f"marketplace-offering-merge-{action}", kwargs={"uuid": merge_uuid})


@override_settings(CELERY_TASK_ALWAYS_EAGER=True, CELERY_TASK_EAGER_PROPAGATES=True)
class DuplicateGroupMergeTest(BaseOpenStackTest):
    """A duplicate per-tenant offering group is resolved by an offering merge.

    The merge is built the way the staff UI builds it: from the group's row in
    the duplicate-offerings list (keeper, duplicates, suggested mapping), then
    created, previewed and executed through the generic merge API.
    """

    def setUp(self):
        super().setUp()
        self.fixture = OpenStackFixture()
        self.tenant = self.fixture.tenant
        self.staff = self.fixture.staff
        # Per-tenant offerings are children of the tenant's offering.
        self.parent = marketplace_factories.OfferingFactory(
            type=OPENSTACK_TENANT_OFFERING, state=OfferingStates.ACTIVE
        )

    def _offering(self, scope=None, with_plan=False, **kwargs):
        offering = marketplace_factories.OfferingFactory(
            type=OPENSTACK_INSTANCE_OFFERING,
            scope=scope or self.tenant,
            parent=self.parent,
            customer=self.fixture.customer,
            project=self.fixture.project,
            state=kwargs.pop("state", OfferingStates.ACTIVE),
            **kwargs,
        )
        if with_plan:
            component = marketplace_factories.OfferingComponentFactory(
                offering=offering, type="cores", name="Cores"
            )
            plan = marketplace_factories.PlanFactory(offering=offering, name="Default")
            marketplace_factories.PlanComponentFactory(
                plan=plan, component=component, price=Decimal(10)
            )
        return offering

    def _resource(self, offering, scope=None, **kwargs):
        plan = offering.plans.first()
        resource = marketplace_factories.ResourceFactory(
            scope=scope,
            project=self.fixture.project,
            offering=offering,
            plan=plan,
            state=kwargs.pop("state", ResourceStates.OK),
            **kwargs,
        )
        if plan is not None:
            marketplace_models.ResourcePlanPeriod.objects.filter(
                resource=resource
            ).delete()
            marketplace_factories.ResourcePlanPeriodFactory(
                resource=resource,
                plan=plan,
                start=core_utils.month_start(timezone.now()),
                end=None,
            )
        return resource

    def _groups(self):
        self.client.force_authenticate(self.staff)
        response = self.client.get(GROUPS_URL)
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        return [row for row in response.data if row["tenant_id"] == self.tenant.id]

    def _create_merge(self, row):
        self.client.force_authenticate(self.staff)
        mapping = row["suggested_mapping"]
        response = self.client.post(
            MERGES_URL,
            {
                "sources": [str(uuid) for uuid in row["duplicate_uuids"]],
                "target": str(row["keeper_uuid"]),
                "plan_mapping": mapping["plan_mapping"],
                "component_mapping": mapping["component_mapping"],
            },
            format="json",
        )
        self.assertEqual(response.status_code, status.HTTP_201_CREATED, response.data)
        merge_uuid = response.data["uuid"]
        response = self.client.post(merge_action_url(merge_uuid, "preview"))
        self.assertEqual(response.status_code, status.HTTP_200_OK, response.data)
        return marketplace_models.OfferingMerge.objects.get(uuid=merge_uuid)

    def _execute(self, merge):
        warnings = [warning["code"] for warning in merge.preview["warnings"]]
        response = self.client.post(
            merge_action_url(merge.uuid.hex, "execute"),
            {"acknowledged_warnings": warnings},
            format="json",
        )
        self.assertEqual(response.status_code, status.HTTP_202_ACCEPTED, response.data)
        merge.refresh_from_db()
        self.assertEqual(merge.state, MergeStates.DONE, merge.error_message)
        self.assertTrue(merge.verification["passed"], merge.verification)
        return merge

    def _resolve(self):
        [row] = self._groups()
        self.assertEqual(row["blockers"], [])
        return self._execute(self._create_merge(row))

    def _open_periods(self, resource):
        return marketplace_models.ResourcePlanPeriod.objects.filter(
            resource=resource, end=None
        )

    # --- Resolving a group --------------------------------------------------

    def test_group_with_resources_moves_everything_to_the_keeper(self):
        keeper = self._offering(with_plan=True)
        self._resource(keeper, scope=self.fixture.instance)
        duplicate = self._offering(with_plan=True)
        moved = self._resource(duplicate, scope=self.fixture.volume)
        order = marketplace_factories.OrderFactory(
            offering=duplicate,
            resource=moved,
            plan=moved.plan,
            project=self.fixture.project,
            state=OrderStates.DONE,
        )
        duplicate_component = duplicate.components.get()
        usage = marketplace_factories.ComponentUsageFactory(
            resource=moved,
            component=duplicate_component,
            plan_period=self._open_periods(moved).get(),
        )
        quota = marketplace_models.ComponentQuota.objects.create(
            resource=moved, component=duplicate_component, limit=10, usage=1
        )

        merge = self._resolve()

        self.assertEqual(list(merge.sources.all()), [duplicate])
        self.assertEqual(merge.target, keeper)
        for obj in (moved, order, usage, quota):
            obj.refresh_from_db()
        keeper_plan = keeper.plans.get()
        keeper_component = keeper.components.get()
        self.assertEqual(moved.offering, keeper)
        self.assertEqual(moved.plan, keeper_plan)
        self.assertEqual(order.offering, keeper)
        self.assertEqual(order.plan, keeper_plan)
        self.assertEqual(usage.component, keeper_component)
        self.assertEqual(quota.component, keeper_component)
        self.assertEqual(self._open_periods(moved).get().plan, keeper_plan)
        duplicate.refresh_from_db()
        self.assertEqual(duplicate.state, OfferingStates.ARCHIVED)
        self.assertEqual(self._groups(), [])

    def test_merge_leaves_one_open_plan_period(self):
        keeper = self._offering(with_plan=True)
        self._resource(keeper, scope=self.fixture.instance)
        duplicate = self._offering(with_plan=True)
        moved = self._resource(duplicate, scope=self.fixture.volume)
        self.assertEqual(self._open_periods(moved).count(), 1)

        self._resolve()

        moved.refresh_from_db()
        self.assertEqual(moved.plan, keeper.plans.get())
        self.assertEqual(self._open_periods(moved).count(), 1)

    def test_resolving_creates_terminates_and_changes_no_invoice_item(self):
        keeper = self._offering(with_plan=True)
        self._resource(keeper, scope=self.fixture.instance)
        duplicate = self._offering(with_plan=True)
        moved = self._resource(duplicate, scope=self.fixture.volume)
        source_plan_component = marketplace_models.PlanComponent.objects.get(
            plan__offering=duplicate
        )
        invoice, _ = invoice_models.Invoice.objects.get_or_create(
            customer=moved.project.customer,
            year=timezone.now().year,
            month=timezone.now().month,
        )
        invoice_factories.InvoiceItemFactory(
            invoice=invoice,
            resource=moved,
            project=moved.project,
            plan_component=source_plan_component,
            unit_price=Decimal(10),
            details=billing_utils.get_component_details(moved, source_plan_component),
        )
        # Snapshot fields the open-month policy rewrites; everything else,
        # the billing period and amounts included, must not change.
        rewritten = {"details", "plan_component_id", "modified"}

        def items():
            return {
                row["id"]: row
                for row in invoice_models.InvoiceItem.objects.values().order_by("id")
            }

        before = items()

        self._resolve()

        after = items()
        self.assertEqual(set(after), set(before))
        for pk, row in before.items():
            self.assertEqual(
                {
                    key: value
                    for key, value in after[pk].items()
                    if key not in rewritten
                },
                {key: value for key, value in row.items() if key not in rewritten},
            )
        target_plan_component = marketplace_models.PlanComponent.objects.get(
            plan__offering=keeper
        )
        item = invoice_models.InvoiceItem.objects.get(resource=moved)
        self.assertEqual(item.plan_component, target_plan_component)
        self.assertEqual(item.details["offering_uuid"], keeper.uuid.hex)

    # --- Empty duplicates ---------------------------------------------------

    def test_empty_duplicate_is_archived_not_deleted(self):
        keeper = self._offering()
        self._resource(keeper, scope=self.fixture.instance)
        duplicate = self._offering()

        self._resolve()

        duplicate.refresh_from_db()
        self.assertEqual(duplicate.state, OfferingStates.ARCHIVED)
        self.assertEqual(self._groups(), [])
        # Per-tenant lookups no longer see the merged-away duplicate.
        self.assertEqual(
            utils.get_offering(OPENSTACK_INSTANCE_OFFERING, self.tenant), keeper
        )
        result = utils.self_heal_tenant_offerings(self.tenant)
        self.assertEqual(result[OPENSTACK_INSTANCE_OFFERING], "ok")

    def test_duplicate_that_gained_a_resource_since_the_preview_is_refused(self):
        keeper = self._offering()
        self._resource(keeper, scope=self.fixture.instance)
        duplicate = self._offering()
        [row] = self._groups()
        merge = self._create_merge(row)

        late = self._resource(duplicate, scope=self.fixture.volume)

        with self.assertRaises(offering_merge.OfferingMergeError):
            offering_merge.execute(merge)
        merge.refresh_from_db()
        self.assertEqual(merge.state, MergeStates.FAILED)
        duplicate.refresh_from_db()
        self.assertEqual(duplicate.state, OfferingStates.ACTIVE)
        late.refresh_from_db()
        self.assertEqual(late.offering, duplicate)

    def test_self_heal_unarchives_the_keeper_not_the_merged_away_duplicate(self):
        keeper = self._offering()
        self._resource(keeper, scope=self.fixture.instance)
        duplicate = self._offering()
        self._resolve()
        # Deleting the tenant archives every offering scoped to it.
        marketplace_models.Offering.objects.filter(id=keeper.id).update(
            state=OfferingStates.ARCHIVED
        )

        result = utils.self_heal_tenant_offerings(self.tenant)

        self.assertEqual(result[OPENSTACK_INSTANCE_OFFERING], "unarchived")
        keeper.refresh_from_db()
        duplicate.refresh_from_db()
        self.assertEqual(keeper.state, OfferingStates.ACTIVE)
        self.assertEqual(duplicate.state, OfferingStates.ARCHIVED)

    def test_undone_merge_reports_the_group_again(self):
        keeper = self._offering()
        self._resource(keeper, scope=self.fixture.instance)
        duplicate = self._offering()
        merge = self._resolve()

        offering_merge.undo(merge)

        duplicate.refresh_from_db()
        self.assertEqual(duplicate.state, OfferingStates.ACTIVE)
        [row] = self._groups()
        self.assertEqual(row["duplicate_uuids"], [duplicate.uuid.hex])

    # --- Detection ----------------------------------------------------------

    def test_keeper_is_offering_with_active_resources(self):
        empty = self._offering()
        in_use = self._offering()
        self._resource(in_use, scope=self.fixture.instance)

        [row] = self._groups()

        self.assertEqual(row["keeper_uuid"], in_use.uuid.hex)
        self.assertEqual(row["duplicate_uuids"], [empty.uuid.hex])

    def test_terminated_resource_does_not_block_keeper_choice(self):
        # A duplicate whose only resource is terminated has 0 active resources,
        # so it should not be chosen as keeper over an in-use offering.
        in_use = self._offering()
        self._resource(in_use, scope=self.fixture.instance)
        stale = self._offering()
        self._resource(
            stale, scope=self.fixture.volume, state=ResourceStates.TERMINATED
        )

        [row] = self._groups()

        self.assertEqual(row["keeper_uuid"], in_use.uuid.hex)

    def test_tenant_scoping(self):
        other_tenant = OpenStackFixture().tenant
        self._offering()
        self._offering()
        self._offering(scope=other_tenant)
        self._offering(scope=other_tenant)

        groups = utils.collect_duplicate_offering_groups(self.tenant.id)

        self.assertEqual(list(groups), [(self.tenant.id, OPENSTACK_INSTANCE_OFFERING)])

    def test_offerings_of_another_tenant_cannot_be_merged_in(self):
        keeper = self._offering()
        other = self._offering(scope=OpenStackFixture().tenant)

        preview = offering_merge.preview_selection([other], keeper)

        self.assertIn(
            "offering_hierarchy", {blocker["code"] for blocker in preview["blockers"]}
        )
