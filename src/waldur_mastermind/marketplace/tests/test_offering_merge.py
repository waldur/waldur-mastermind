import inspect
from decimal import Decimal
from unittest import mock

from dateutil.relativedelta import relativedelta
from django.test import TestCase
from django.utils import timezone

from waldur_core.core import utils as core_utils
from waldur_core.core.middleware import skip_side_effects
from waldur_core.structure.tests import factories as structure_factories
from waldur_mastermind.invoices.tests import factories as invoice_factories
from waldur_mastermind.marketplace import models, offering_merge
from waldur_mastermind.marketplace.enums import (
    BASIC_OFFERING,
    OPENSTACK_TENANT_OFFERING,
    REMOTE_OFFERING,
    SITE_AGENT_OFFERING,
    SUPPORT_OFFERING,
    BillingTypes,
    OfferingStates,
    OrderStates,
    ResourceStates,
)
from waldur_mastermind.marketplace.offering_merge import OfferingMergeError
from waldur_mastermind.marketplace.tests import factories
from waldur_mastermind.support import models as support_models
from waldur_mastermind.support.tests import factories as support_factories

MONTH_START = core_utils.month_start(timezone.now())


def make_offering(
    offering_type=SUPPORT_OFFERING,
    components=(("cpu", BillingTypes.USAGE),),
    plans=("plan",),
    price=Decimal(10),
    options=None,
):
    offering = factories.OfferingFactory(
        type=offering_type,
        state=OfferingStates.ACTIVE,
        options=options or {"order": [], "options": {}},
    )
    component_objects = {
        component_type: factories.OfferingComponentFactory(
            offering=offering,
            type=component_type,
            name=component_type,
            billing_type=billing_type,
        )
        for component_type, billing_type in components
    }
    plan_objects = {}
    for name in plans:
        plan = factories.PlanFactory(offering=offering, name=name)
        for component in component_objects.values():
            factories.PlanComponentFactory(plan=plan, component=component, price=price)
        plan_objects[name] = plan
    return offering, plan_objects, component_objects


def make_resource(offering, plan, limits=None, **kwargs):
    resource = factories.ResourceFactory(
        offering=offering,
        plan=plan,
        state=ResourceStates.OK,
        limits=limits or {},
        **kwargs,
    )
    factories.ResourcePlanPeriodFactory(
        resource=resource, plan=plan, start=MONTH_START, end=None
    )
    return resource


def make_merge(target, sources, plan_mapping, component_mapping, **kwargs):
    merge = models.OfferingMerge.objects.create(
        target=target,
        plan_mapping={
            source.uuid.hex: target_plan.uuid.hex
            for source, target_plan in plan_mapping.items()
        },
        component_mapping=component_mapping,
        **kwargs,
    )
    merge.sources.set(sources)
    return merge


def codes(items):
    return {item["code"] for item in items}


SNAPSHOT_MODELS = (
    models.Offering,
    models.Plan,
    models.OfferingComponent,
    models.PlanComponent,
    models.Resource,
    models.Order,
    models.ResourcePlanPeriod,
    models.ComponentUsage,
    models.ComponentUsageMonthly,
    models.ComponentQuota,
    models.OfferingUser,
)


# Everything but the derived monthly summaries, which undo recomputes.
JOURNALLED_MODELS = tuple(
    model for model in SNAPSHOT_MODELS if model is not models.ComponentUsageMonthly
)


def snapshot(model_list=SNAPSHOT_MODELS):
    return {
        model._meta.label: {
            row["id"]: {key: value for key, value in row.items() if key != "modified"}
            for row in model._base_manager.values()
        }
        for model in model_list
    }


class TwoSupportOfferingsScenario:
    """Two Support offerings with differently named plans merged into a third."""

    def setUp(self):
        super().setUp()
        self.target, self.target_plans, self.target_components = make_offering(
            components=(
                ("vcpu", BillingTypes.USAGE),
                ("memory", BillingTypes.USAGE),
            ),
            plans=("Tier 1", "Tier 2"),
        )
        self.source_a, self.a_plans, self.a_components = make_offering(
            components=(("cpu", BillingTypes.USAGE),), plans=("basic",)
        )
        self.source_b, self.b_plans, self.b_components = make_offering(
            components=(
                ("cpu", BillingTypes.USAGE),
                ("ram", BillingTypes.USAGE),
            ),
            plans=("premium",),
        )
        self.plan_mapping = {
            self.a_plans["basic"]: self.target_plans["Tier 1"],
            self.b_plans["premium"]: self.target_plans["Tier 2"],
        }
        self.component_mapping = {
            self.source_a.uuid.hex: {"cpu": "vcpu"},
            self.source_b.uuid.hex: {"cpu": "vcpu", "ram": "memory"},
        }
        self.resource_a = make_resource(
            self.source_a, self.a_plans["basic"], limits={"cpu": 2}
        )
        self.resource_b = make_resource(
            self.source_b, self.b_plans["premium"], limits={"cpu": 4, "ram": 8}
        )
        self.order_a = factories.OrderFactory(
            offering=self.source_a,
            plan=self.a_plans["basic"],
            resource=self.resource_a,
            project=self.resource_a.project,
            state=OrderStates.DONE,
            limits={"cpu": 2},
        )
        self.usage_a = factories.ComponentUsageFactory(
            resource=self.resource_a,
            component=self.a_components["cpu"],
            plan_period=models.ResourcePlanPeriod.objects.get(resource=self.resource_a),
        )
        self.monthly_a = factories.ComponentUsageMonthlyFactory(
            component=self.a_components["cpu"],
            billing_period=(MONTH_START - relativedelta(months=1)).date(),
        )
        self.monthly_b = factories.ComponentUsageMonthlyFactory(
            component=self.b_components["cpu"],
            billing_period=MONTH_START.date(),
        )
        self.quota_b = models.ComponentQuota.objects.create(
            resource=self.resource_b, component=self.b_components["ram"], limit=8
        )
        self.offering_user_a = factories.OfferingUserFactory(offering=self.source_a)
        self.offering_user_b = factories.OfferingUserFactory(offering=self.source_b)
        self.invoice_item = invoice_factories.InvoiceItemFactory(
            resource=self.resource_a,
            project=self.resource_a.project,
            unit_price=Decimal(10),
            plan_component=models.PlanComponent.objects.get(plan=self.a_plans["basic"]),
            details={"offering_component_type": "cpu"},
        )

    def make_merge(self, **kwargs):
        return make_merge(
            self.target,
            [self.source_a, self.source_b],
            self.plan_mapping,
            self.component_mapping,
            **kwargs,
        )


class OfferingMergeExecuteTest(TwoSupportOfferingsScenario, TestCase):
    def test_merge_moves_history_resources_and_archives_sources(self):
        invoice_items_before = snapshot([self.invoice_item.__class__])
        merge = offering_merge.preview(self.make_merge())
        self.assertEqual(merge.preview["blockers"], [])

        merge = offering_merge.execute(merge)

        self.assertEqual(merge.state, models.OfferingMerge.States.DONE)
        for resource in (self.resource_a, self.resource_b):
            resource.refresh_from_db()
            self.assertEqual(resource.offering, self.target)
            self.assertEqual(
                models.ResourcePlanPeriod.objects.filter(
                    resource=resource, end=None
                ).count(),
                1,
            )
        self.assertEqual(self.resource_a.plan, self.target_plans["Tier 1"])
        self.assertEqual(self.resource_b.plan, self.target_plans["Tier 2"])
        self.assertEqual(self.resource_a.limits, {"vcpu": 2})
        self.assertEqual(self.resource_b.limits, {"vcpu": 4, "memory": 8})
        self.assertEqual(
            set(
                models.ResourcePlanPeriod.objects.filter(
                    resource__in=[self.resource_a, self.resource_b]
                ).values_list("plan__offering", flat=True)
            ),
            {self.target.id},
        )

        self.order_a.refresh_from_db()
        self.assertEqual(self.order_a.offering, self.target)
        self.assertEqual(self.order_a.plan, self.target_plans["Tier 1"])
        self.assertEqual(self.order_a.limits, {"vcpu": 2})

        self.usage_a.refresh_from_db()
        self.assertEqual(self.usage_a.component, self.target_components["vcpu"])
        # Monthly summaries are recomputed, not repointed: the sources' rows are
        # gone and the target's component carries the moved usage.
        self.assertFalse(
            models.ComponentUsageMonthly.objects.filter(
                component__offering__in=[self.source_a, self.source_b]
            ).exists()
        )
        summary = models.ComponentUsageMonthly.objects.get(
            component=self.target_components["vcpu"],
            billing_period=MONTH_START.date(),
        )
        self.assertEqual(summary.total_consumed, self.usage_a.usage)
        self.quota_b.refresh_from_db()
        self.assertEqual(self.quota_b.component, self.target_components["memory"])
        for offering_user in (self.offering_user_a, self.offering_user_b):
            offering_user.refresh_from_db()
            self.assertEqual(offering_user.offering, self.target)

        for source in (self.source_a, self.source_b):
            source.refresh_from_db()
            self.assertEqual(source.state, OfferingStates.ARCHIVED)

        self.assertEqual(snapshot([self.invoice_item.__class__]), invoice_items_before)
        self.assertTrue(merge.changes.exists())
        self.assertFalse(
            merge.changes.filter(model="marketplace.ComponentUsageMonthly").exists()
        )

    def test_order_count_follows_moved_orders(self):
        merge = offering_merge.execute(offering_merge.preview(self.make_merge()))
        for offering in (self.target, self.source_a, self.source_b):
            self.assertEqual(
                offering.get_quota_usage("order_count"),
                models.Order.objects.filter(offering=offering).count(),
            )
        self.assertEqual(self.source_a.get_quota_usage("order_count"), 0)

        offering_merge.undo(merge)
        for offering in (self.target, self.source_a, self.source_b):
            self.assertEqual(
                offering.get_quota_usage("order_count"),
                models.Order.objects.filter(offering=offering).count(),
            )

    def test_merge_does_not_fire_billing_or_plan_period_handlers(self):
        merge = offering_merge.preview(self.make_merge())
        with mock.patch(
            "waldur_mastermind.marketplace.handlers.MarketplaceBillingService"
        ) as billing:
            offering_merge.execute(merge)
        billing.handle_plan_change.assert_not_called()
        billing.handle_limits_change.assert_not_called()

    def test_resource_created_between_preview_and_execution_fails_merge(self):
        merge = offering_merge.preview(self.make_merge())
        late = make_resource(self.source_a, self.a_plans["basic"])
        before = snapshot()

        with self.assertRaises(OfferingMergeError):
            offering_merge.execute(merge)

        merge.refresh_from_db()
        self.assertEqual(merge.state, models.OfferingMerge.States.FAILED)
        self.assertIn("changed since it was previewed", merge.error_message)
        self.assertFalse(merge.changes.exists())
        self.assertEqual(snapshot(), before)
        late.refresh_from_db()
        self.assertEqual(late.offering, self.source_a)

    def test_execute_requires_preview(self):
        merge = self.make_merge()
        with self.assertRaises(OfferingMergeError):
            offering_merge.execute(merge)
        merge.refresh_from_db()
        self.assertEqual(merge.state, models.OfferingMerge.States.DRAFT)

    def test_execute_refuses_blocked_merge(self):
        self.plan_mapping.pop(self.b_plans["premium"])
        merge = offering_merge.preview(self.make_merge())
        self.assertIn("unmapped_plan", codes(merge.preview["blockers"]))

        with self.assertRaises(OfferingMergeError):
            offering_merge.execute(merge)

        merge.refresh_from_db()
        self.assertEqual(merge.state, models.OfferingMerge.States.FAILED)
        self.resource_a.refresh_from_db()
        self.assertEqual(self.resource_a.offering, self.source_a)

    def test_undo_restores_every_journalled_field(self):
        before = snapshot(JOURNALLED_MODELS)
        merge = offering_merge.execute(offering_merge.preview(self.make_merge()))

        merge = offering_merge.undo(merge)

        self.assertEqual(merge.state, models.OfferingMerge.States.UNDONE)
        self.assertEqual(snapshot(JOURNALLED_MODELS), before)
        # Summaries are recomputed rather than restored.
        self.assertFalse(
            models.ComponentUsageMonthly.objects.filter(
                component__offering=self.target
            ).exists()
        )
        self.assertEqual(
            models.ComponentUsageMonthly.objects.get(
                component=self.a_components["cpu"],
                billing_period=MONTH_START.date(),
            ).total_consumed,
            self.usage_a.usage,
        )

    def test_undo_after_usage_report_and_limit_change(self):
        before = snapshot(JOURNALLED_MODELS)
        merge = offering_merge.execute(offering_merge.preview(self.make_merge()))

        # A usage report and a limit change arrive for the moved resources.
        new_usage = factories.ComponentUsageFactory(
            resource=self.resource_b,
            component=self.target_components["memory"],
            plan_period=models.ResourcePlanPeriod.objects.get(resource=self.resource_b),
            usage=5,
        )
        models.Resource.objects.filter(pk=self.resource_b.pk).update(
            current_usages={"memory": 5}
        )
        models.Resource.objects.filter(pk=self.resource_a.pk).update(limits={"vcpu": 3})
        new_order = factories.OrderFactory(
            offering=self.target,
            plan=self.target_plans["Tier 1"],
            resource=self.resource_a,
            project=self.resource_a.project,
            state=OrderStates.DONE,
            limits={"vcpu": 3},
            attributes={"old_limits": {"vcpu": 2}},
        )

        merge = offering_merge.undo(merge)

        self.assertEqual(merge.state, models.OfferingMerge.States.UNDONE)
        new_usage.refresh_from_db()
        self.assertEqual(new_usage.component, self.b_components["ram"])
        new_order.refresh_from_db()
        self.assertEqual(new_order.offering, self.source_a)
        self.assertEqual(new_order.plan, self.a_plans["basic"])
        self.assertEqual(new_order.limits, {"cpu": 3})
        self.assertEqual(new_order.attributes["old_limits"], {"cpu": 2})

        after = snapshot(JOURNALLED_MODELS)
        del after["marketplace.ComponentUsage"][new_usage.pk]
        del after["marketplace.Order"][new_order.pk]
        resource_a = after["marketplace.Resource"][self.resource_a.pk]
        resource_b = after["marketplace.Resource"][self.resource_b.pk]
        self.assertEqual(resource_a.pop("limits"), {"cpu": 3})
        self.assertEqual(resource_b.pop("current_usages"), {"ram": 5})
        before["marketplace.Resource"][self.resource_a.pk].pop("limits")
        before["marketplace.Resource"][self.resource_b.pk].pop("current_usages")
        self.assertEqual(after, before)
        self.assertEqual(
            models.ComponentUsageMonthly.objects.get(
                component=self.b_components["ram"],
                billing_period=MONTH_START.date(),
            ).total_consumed,
            5,
        )

    def test_undo_is_refused_after_moved_resource_switched_plan(self):
        merge = offering_merge.execute(offering_merge.preview(self.make_merge()))
        models.Resource.objects.filter(pk=self.resource_a.pk).update(
            plan=self.target_plans["Tier 2"]
        )

        with self.assertRaises(OfferingMergeError) as context:
            offering_merge.undo(merge)

        self.assertIn("resource_changed", codes(context.exception.details))
        merge.refresh_from_db()
        self.assertEqual(merge.state, models.OfferingMerge.States.DONE)
        self.source_a.refresh_from_db()
        self.assertEqual(self.source_a.state, OfferingStates.ARCHIVED)

    def test_undo_is_refused_for_new_row_on_unmapped_target_component(self):
        merge = offering_merge.execute(offering_merge.preview(self.make_merge()))
        # Source A has no counterpart of the target's memory component.
        factories.ComponentUsageFactory(
            resource=self.resource_a,
            component=self.target_components["memory"],
            plan_period=models.ResourcePlanPeriod.objects.get(resource=self.resource_a),
        )

        with self.assertRaises(OfferingMergeError) as context:
            offering_merge.undo(merge)

        self.assertIn("unmappable_new_row", codes(context.exception.details))

    def test_undo_is_refused_when_moving_back_violates_unique_constraint(self):
        merge = offering_merge.execute(offering_merge.preview(self.make_merge()))
        # quota_b moved to the target's memory; a new quota on the source's ram
        # for the same resource would collide with it on the way back.
        models.ComponentQuota.objects.create(
            resource=self.resource_b, component=self.b_components["ram"], limit=1
        )

        with self.assertRaises(OfferingMergeError) as context:
            offering_merge.undo(merge)

        conflict = [
            blocker
            for blocker in context.exception.details
            if blocker["code"] == "unique_conflict"
        ][0]
        self.assertEqual(conflict["details"]["model"], "marketplace.ComponentQuota")
        self.assertEqual(conflict["details"]["fields"], ["component_id", "resource_id"])

    def test_undo_requires_done_merge(self):
        merge = offering_merge.preview(self.make_merge())
        with self.assertRaises(OfferingMergeError):
            offering_merge.undo(merge)


class OfferingMergeTypesTest(TestCase):
    def merge_single(self, source_type, target_type):
        target, target_plans, _ = make_offering(offering_type=target_type)
        source, source_plans, _ = make_offering(offering_type=source_type)
        resource = make_resource(source, source_plans["plan"], limits={"cpu": 1})
        merge = make_merge(
            target,
            [source],
            {source_plans["plan"]: target_plans["plan"]},
            {source.uuid.hex: {"cpu": "cpu"}},
        )
        return merge, resource, target

    def test_basic_into_site_agent_succeeds(self):
        merge, resource, target = self.merge_single(BASIC_OFFERING, SITE_AGENT_OFFERING)
        resource.backend_id = "backend-1"
        resource.save()
        merge = offering_merge.execute(offering_merge.preview(merge))
        self.assertEqual(merge.state, models.OfferingMerge.States.DONE)
        resource.refresh_from_db()
        self.assertEqual(resource.offering, target)

    def test_site_agent_into_support_succeeds(self):
        merge, resource, target = self.merge_single(
            SITE_AGENT_OFFERING, SUPPORT_OFFERING
        )
        merge = offering_merge.execute(offering_merge.preview(merge))
        self.assertEqual(merge.state, models.OfferingMerge.States.DONE)
        resource.refresh_from_db()
        self.assertEqual(resource.offering, target)

    def test_openstack_tenant_into_basic_is_blocked(self):
        merge, _, _ = self.merge_single(OPENSTACK_TENANT_OFFERING, BASIC_OFFERING)
        merge = offering_merge.preview(merge)
        self.assertIn("offering_type_not_allowed", codes(merge.preview["blockers"]))

    def test_same_type_is_allowed(self):
        merge, _, _ = self.merge_single(
            OPENSTACK_TENANT_OFFERING, OPENSTACK_TENANT_OFFERING
        )
        preview = offering_merge.build_preview(merge)
        self.assertNotIn("offering_type_not_allowed", codes(preview["blockers"]))


class OfferingMergePreviewTest(TestCase):
    def setUp(self):
        self.target, self.target_plans, self.target_components = make_offering()
        self.source, self.source_plans, self.source_components = make_offering()
        self.resource = make_resource(self.source, self.source_plans["plan"])
        self.plan_mapping = {self.source_plans["plan"]: self.target_plans["plan"]}
        self.component_mapping = {self.source.uuid.hex: {"cpu": "cpu"}}

    def preview(self, **kwargs):
        merge = make_merge(
            self.target,
            [self.source],
            self.plan_mapping,
            self.component_mapping,
            **kwargs,
        )
        return offering_merge.build_preview(merge)

    def test_clean_merge_has_no_blockers_or_warnings(self):
        preview = self.preview()
        self.assertEqual(preview["blockers"], [])
        self.assertEqual(preview["warnings"], [])
        self.assertEqual(preview["counts"]["marketplace.Resource.offering"], 1)
        self.assertEqual(preview["counts"]["marketplace.ResourcePlanPeriod.plan"], 1)

    def test_preview_writes_nothing_but_the_record(self):
        before = snapshot()
        merge = make_merge(
            self.target, [self.source], self.plan_mapping, self.component_mapping
        )
        merge = offering_merge.preview(merge)
        self.assertEqual(merge.state, models.OfferingMerge.States.PREVIEWED)
        self.assertEqual(snapshot(), before)

    # --- Blockers -----------------------------------------------------------

    def test_type_outside_allowed_set_is_blocked(self):
        self.source.type = OPENSTACK_TENANT_OFFERING
        self.source.save()
        self.assertIn("offering_type_not_allowed", codes(self.preview()["blockers"]))

    def test_remote_offering_is_blocked(self):
        for offering in (self.source, self.target):
            offering.type = REMOTE_OFFERING
            offering.save()
        self.assertIn("remote_offering", codes(self.preview()["blockers"]))

    def test_offering_with_parent_is_blocked(self):
        self.source.parent = factories.OfferingFactory()
        self.source.save()
        self.assertIn("offering_hierarchy", codes(self.preview()["blockers"]))

    def test_offering_with_children_is_blocked(self):
        factories.OfferingFactory(parent=self.target)
        self.assertIn("offering_hierarchy", codes(self.preview()["blockers"]))

    def test_target_in_sources_is_blocked(self):
        merge = make_merge(self.target, [self.source, self.target], {}, {})
        self.assertIn(
            "target_in_sources",
            codes(offering_merge.build_preview(merge)["blockers"]),
        )

    def test_unmapped_plan_is_blocked(self):
        self.plan_mapping = {}
        self.assertIn("unmapped_plan", codes(self.preview()["blockers"]))

    def test_unmapped_component_is_blocked(self):
        self.component_mapping = {}
        self.assertIn("unmapped_component", codes(self.preview()["blockers"]))

    def test_plan_mapped_to_foreign_plan_is_blocked(self):
        self.plan_mapping = {self.source_plans["plan"]: factories.PlanFactory()}
        self.assertIn("invalid_plan_mapping", codes(self.preview()["blockers"]))

    def test_plan_unit_mismatch_is_blocked(self):
        target_plan = self.target_plans["plan"]
        target_plan.unit = "month"
        target_plan.save()
        self.assertIn("plan_unit_mismatch", codes(self.preview()["blockers"]))

    def test_component_billing_type_mismatch_is_blocked(self):
        self.target_components["cpu"].billing_type = BillingTypes.LIMIT
        self.target_components["cpu"].save()
        self.assertIn(
            "component_billing_type_mismatch", codes(self.preview()["blockers"])
        )

    def test_non_injective_component_mapping_is_blocked(self):
        factories.OfferingComponentFactory(
            offering=self.source, type="gpu", billing_type=BillingTypes.USAGE
        )
        self.component_mapping = {self.source.uuid.hex: {"cpu": "cpu", "gpu": "cpu"}}
        self.assertIn(
            "component_mapping_not_injective", codes(self.preview()["blockers"])
        )

    def test_pending_order_is_blocked(self):
        factories.OrderFactory(
            offering=self.source,
            plan=self.source_plans["plan"],
            resource=self.resource,
            project=self.resource.project,
            state=OrderStates.PENDING_PROVIDER,
        )
        self.assertIn("pending_orders", codes(self.preview()["blockers"]))

    def test_monthly_summaries_are_counted_for_recompute_not_blocked(self):
        factories.ComponentUsageMonthlyFactory(
            component=self.source_components["cpu"],
            billing_period=MONTH_START.date(),
        )
        factories.ComponentUsageMonthlyFactory(
            component=self.target_components["cpu"],
            billing_period=MONTH_START.date(),
        )
        preview = self.preview()
        self.assertEqual(preview["blockers"], [])
        self.assertEqual(
            preview["summaries_to_recompute"],
            {"components": 2, "periods": [MONTH_START.strftime("%Y-%m")]},
        )
        self.assertEqual(
            preview["counts"]["marketplace.ComponentUsageMonthly.component"], 2
        )

    def test_unused_plan_and_component_may_stay_unmapped(self):
        unused_plan = factories.PlanFactory(offering=self.source, name="unused")
        factories.PlanComponentFactory(
            plan=unused_plan, component=self.source_components["cpu"]
        )
        unused_component = factories.OfferingComponentFactory(
            offering=self.source, type="gpu", billing_type=BillingTypes.USAGE
        )
        merge = offering_merge.preview(
            make_merge(
                self.target, [self.source], self.plan_mapping, self.component_mapping
            )
        )
        self.assertEqual(merge.preview["blockers"], [])

        offering_merge.execute(merge)

        self.resource.refresh_from_db()
        self.assertEqual(self.resource.offering, self.target)
        unused_plan.refresh_from_db()
        unused_component.refresh_from_db()
        self.assertEqual(unused_plan.offering, self.source)
        self.assertEqual(unused_component.offering, self.source)

    def test_plan_referenced_by_an_order_needs_a_mapping(self):
        old_plan = factories.PlanFactory(offering=self.source, name="old")
        factories.OrderFactory(
            offering=self.source,
            plan=self.source_plans["plan"],
            old_plan=old_plan,
            resource=self.resource,
            project=self.resource.project,
            state=OrderStates.DONE,
        )
        blockers = self.preview()["blockers"]
        self.assertEqual(
            [b["details"]["plan"] for b in blockers if b["code"] == "unmapped_plan"],
            [old_plan.uuid.hex],
        )

    def test_component_referenced_by_limits_needs_a_mapping(self):
        factories.OfferingComponentFactory(
            offering=self.source, type="gpu", billing_type=BillingTypes.USAGE
        )
        models.Resource.objects.filter(pk=self.resource.pk).update(limits={"gpu": 1})
        blockers = self.preview()["blockers"]
        self.assertEqual(
            [
                b["details"]["component_type"]
                for b in blockers
                if b["code"] == "unmapped_component"
            ],
            ["gpu"],
        )

    def _creation_issue(self, status):
        support_factories.IssueStatusFactory(
            name="Resolved", type=support_models.IssueStatus.Types.RESOLVED
        )
        support_factories.IssueStatusFactory(
            name="Canceled", type=support_models.IssueStatus.Types.CANCELED
        )
        issue = support_factories.IssueFactory(status=status)
        self.resource.scope = issue
        self.resource.save()

    def test_open_creation_issue_is_blocked(self):
        self._creation_issue("In progress")
        self.assertIn("open_creation_issue", codes(self.preview()["blockers"]))

    def test_resolved_creation_issue_is_not_blocked(self):
        self._creation_issue("Resolved")
        self.assertNotIn("open_creation_issue", codes(self.preview()["blockers"]))

    # --- Warnings -----------------------------------------------------------

    def test_price_difference_is_warned(self):
        models.PlanComponent.objects.filter(plan=self.target_plans["plan"]).update(
            price=Decimal(20)
        )
        preview = self.preview()
        self.assertEqual(preview["blockers"], [])
        self.assertIn("plan_price_difference", codes(preview["warnings"]))

    def test_unknown_answer_keys_are_warned(self):
        self.source.options = {"order": ["flavor"], "options": {"flavor": {}}}
        self.source.save()
        factories.OrderFactory(
            offering=self.source,
            plan=self.source_plans["plan"],
            resource=self.resource,
            project=self.resource.project,
            state=OrderStates.DONE,
            attributes={"flavor": "large", "name": "x"},
        )
        warnings = self.preview()["warnings"]
        self.assertIn("unknown_answer_keys", codes(warnings))
        self.assertEqual(
            [w for w in warnings if w["code"] == "unknown_answer_keys"][0]["details"][
                "keys"
            ],
            {"flavor": 1},
        )
        self.assertNotIn(
            "unknown_answer_keys",
            codes(self.preview(attribute_key_mapping={"flavor": "size"})["warnings"]),
        )

    def test_user_with_accounts_on_both_is_warned(self):
        user = structure_factories.UserFactory()
        factories.OfferingUserFactory(offering=self.source, user=user)
        factories.OfferingUserFactory(offering=self.target, user=user)
        preview = self.preview()
        self.assertIn("offering_user_on_both", codes(preview["warnings"]))
        self.assertEqual(
            preview["left_on_source"], {"marketplace.OfferingUser.offering": 1}
        )

    def test_empty_backend_id_on_site_agent_target_is_warned(self):
        self.target.type = SITE_AGENT_OFFERING
        self.target.save()
        self.assertIn("empty_backend_id", codes(self.preview()["warnings"]))

    def test_configuration_left_on_source_is_warned(self):
        models.OfferingAccessEndpoint.objects.create(
            offering=self.source, name="login", url="https://login.example.com"
        )
        warnings = self.preview()["warnings"]
        self.assertIn("source_configuration_stays", codes(warnings))


class OfferingMergeHasNoDeletePathTest(TestCase):
    def test_engine_never_deletes(self):
        source = inspect.getsource(offering_merge)
        self.assertNotIn(".delete(", source)


class ProcessBillingSkipSideEffectsTest(TestCase):
    def setUp(self):
        self.offering, self.plans, _ = make_offering(plans=("small", "large"))
        self.resource = make_resource(self.offering, self.plans["small"])

    @mock.patch("waldur_mastermind.marketplace.handlers.MarketplaceBillingService")
    def test_plan_and_limit_changes_are_ignored_under_skip_side_effects(self, billing):
        with skip_side_effects():
            self.resource.plan = self.plans["large"]
            self.resource.limits = {"cpu": 5}
            self.resource.save()
        billing.handle_plan_change.assert_not_called()
        billing.handle_limits_change.assert_not_called()

    @mock.patch("waldur_mastermind.marketplace.handlers.MarketplaceBillingService")
    def test_plan_change_is_billed_otherwise(self, billing):
        self.resource.plan = self.plans["large"]
        self.resource.save()
        billing.handle_plan_change.assert_called_once_with(self.resource)
