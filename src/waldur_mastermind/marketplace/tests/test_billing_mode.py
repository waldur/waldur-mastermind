"""Per-plan billing mode: one OpenStack offering, a limit plan and a usage plan."""

from ddt import data, ddt
from django.test import TestCase
from rest_framework import status, test

from waldur_core.permissions.enums import PermissionEnum
from waldur_core.permissions.fixtures import CustomerRole
from waldur_core.structure.tests import factories as structure_factories
from waldur_core.structure.tests import fixtures
from waldur_mastermind.common.mixins import UnitPriceMixin
from waldur_mastermind.marketplace import billing_mode, models
from waldur_mastermind.marketplace.enums import (
    BASIC_OFFERING,
    OPENSTACK_TENANT_OFFERING,
    BillingModes,
    BillingTypes,
    LimitPeriods,
    OfferingStates,
)
from waldur_mastermind.marketplace.tests import factories
from waldur_mastermind.marketplace.utils import create_offering_components


def make_openstack_offering(**kwargs):
    """An OpenStack tenant offering with its builtin cores/ram/storage components."""
    offering = factories.OfferingFactory(
        type=OPENSTACK_TENANT_OFFERING, state=OfferingStates.ACTIVE, **kwargs
    )
    create_offering_components(offering)
    return offering


class ResolveComponentTest(TestCase):
    def setUp(self):
        self.offering = make_openstack_offering()
        self.cores = self.offering.components.get(type="cores")
        self.ram = self.offering.components.get(type="ram")
        self.volume_type = factories.OfferingComponentFactory(
            offering=self.offering,
            type="gigabytes_ssd",
            name="Storage (ssd)",
            measured_unit="GB",
            billing_type=BillingTypes.LIMIT,
            limit_period=LimitPeriods.MONTH,
        )
        self.custom = factories.OfferingComponentFactory(
            offering=self.offering,
            type="consultancy",
            name="Consultancy",
            measured_unit="hours",
            billing_type=BillingTypes.USAGE,
        )

    def test_inherit_plan_keeps_component_values(self):
        plan = factories.PlanFactory(offering=self.offering)
        effective = billing_mode.resolve_component(self.cores, plan)
        self.assertEqual(effective.billing_type, BillingTypes.LIMIT)
        self.assertEqual(effective.measured_unit, "cores")
        self.assertEqual(effective.limit_period, LimitPeriods.MONTH)

    def test_no_plan_keeps_component_values(self):
        effective = billing_mode.resolve_component(self.custom, None)
        self.assertEqual(effective.billing_type, BillingTypes.USAGE)
        self.assertEqual(effective.measured_unit, "hours")

    def test_usage_plan_overrides_builtin_components(self):
        plan = factories.PlanFactory(
            offering=self.offering, billing_mode=BillingModes.USAGE
        )
        cores = billing_mode.resolve_component(self.cores, plan)
        ram = billing_mode.resolve_component(self.ram, plan)
        volume_type = billing_mode.resolve_component(self.volume_type, plan)
        self.assertEqual(cores.billing_type, BillingTypes.USAGE)
        self.assertEqual(cores.measured_unit, "core-hours")
        self.assertEqual(ram.billing_type, BillingTypes.USAGE)
        self.assertEqual(ram.measured_unit, "GB-hours")
        self.assertEqual(volume_type.billing_type, BillingTypes.USAGE)
        self.assertEqual(volume_type.measured_unit, "GB-hours")
        self.assertFalse(cores.is_prepaid)

    def test_usage_plan_leaves_custom_components_alone(self):
        plan = factories.PlanFactory(
            offering=self.offering, billing_mode=BillingModes.USAGE
        )
        self.custom.billing_type = BillingTypes.FIXED
        self.custom.save()
        effective = billing_mode.resolve_component(self.custom, plan)
        self.assertEqual(effective.billing_type, BillingTypes.FIXED)
        self.assertEqual(effective.measured_unit, "hours")

    def test_limit_plan_overrides_usage_components(self):
        self.cores.billing_type = BillingTypes.USAGE
        self.cores.measured_unit = "core-hours"
        self.cores.save()
        plan = factories.PlanFactory(
            offering=self.offering, billing_mode=BillingModes.LIMIT
        )
        effective = billing_mode.resolve_component(self.cores, plan)
        self.assertEqual(effective.billing_type, BillingTypes.LIMIT)
        self.assertEqual(effective.limit_period, LimitPeriods.MONTH)
        self.assertEqual(effective.measured_unit, "cores")

    def test_mode_is_ignored_for_offerings_without_builtin_components(self):
        offering = factories.OfferingFactory(type=BASIC_OFFERING)
        component = factories.OfferingComponentFactory(
            offering=offering, type="cpu", billing_type=BillingTypes.LIMIT
        )
        plan = factories.PlanFactory(offering=offering, billing_mode=BillingModes.USAGE)
        effective = billing_mode.resolve_component(component, plan)
        self.assertEqual(effective.billing_type, BillingTypes.LIMIT)


class ResolvedPlanTest(TestCase):
    def setUp(self):
        self.offering = make_openstack_offering()
        self.limit_plan = factories.PlanFactory(offering=self.offering)
        self.usage_plan = factories.PlanFactory(
            offering=self.offering, billing_mode=BillingModes.USAGE
        )

    def test_limit_plan_classification(self):
        resolved = billing_mode.resolve_plan(self.limit_plan)
        self.assertTrue(resolved.is_limit_based)
        self.assertFalse(resolved.is_usage_based)
        self.assertEqual(set(resolved.limit_components), {"cores", "ram", "storage"})

    def test_usage_plan_classification(self):
        resolved = billing_mode.resolve_plan(self.usage_plan)
        self.assertTrue(resolved.is_usage_based)
        self.assertFalse(resolved.is_limit_based)
        self.assertEqual(resolved.limit_components, {})
        self.assertEqual(resolved.usage_types, {"cores", "ram", "storage"})

    def test_offering_level_flags_cover_every_plan(self):
        self.assertTrue(self.offering.is_usage_based)
        self.assertTrue(self.offering.is_limit_based)

    def test_offering_level_flags_without_usage_plan(self):
        self.usage_plan.delete()
        offering = models.Offering.objects.get(pk=self.offering.pk)
        self.assertFalse(offering.is_usage_based)
        self.assertTrue(offering.is_limit_based)

    def test_get_limit_components_for_plan(self):
        self.assertEqual(
            set(self.offering.get_limit_components(self.limit_plan)),
            {"cores", "ram", "storage"},
        )
        self.assertEqual(self.offering.get_limit_components(self.usage_plan), {})
        # Without a plan the union across plans is returned.
        self.assertEqual(
            set(self.offering.get_limit_components()), {"cores", "ram", "storage"}
        )

    def test_resource_flags_follow_its_plan(self):
        limit_resource = factories.ResourceFactory(
            offering=self.offering, plan=self.limit_plan
        )
        usage_resource = factories.ResourceFactory(
            offering=self.offering, plan=self.usage_plan
        )
        self.assertTrue(limit_resource.is_limit_based)
        self.assertFalse(limit_resource.is_usage_based)
        self.assertTrue(usage_resource.is_usage_based)
        self.assertFalse(usage_resource.is_limit_based)

    def test_queryset_helpers(self):
        limit_resource = factories.ResourceFactory(
            offering=self.offering, plan=self.limit_plan
        )
        usage_resource = factories.ResourceFactory(
            offering=self.offering, plan=self.usage_plan
        )
        usage_qs = models.Resource.objects.filter(billing_mode.usage_resource_q())
        self.assertIn(usage_resource, usage_qs)
        self.assertNotIn(limit_resource, usage_qs)
        offerings = models.Offering.objects.filter(billing_mode.usage_offering_q())
        self.assertIn(self.offering, offerings)

    def test_describe_plan_billing(self):
        self.assertEqual(billing_mode.describe_plan_billing(self.limit_plan), "limit")
        self.assertEqual(billing_mode.describe_plan_billing(self.usage_plan), "usage")
        self.assertIsNone(billing_mode.describe_plan_billing(None))

    def test_describe_switch_consequence(self):
        self.assertEqual(billing_mode.describe_switch_consequence("limit", "limit"), "")
        self.assertIn(
            "actual consumption",
            billing_mode.describe_switch_consequence("limit", "usage"),
        )
        self.assertIn(
            "previous plan", billing_mode.describe_switch_consequence("usage", "limit")
        )


@ddt
class PlanBillingModeApiTest(test.APITestCase):
    def setUp(self):
        self.fixture = fixtures.ProjectFixture()
        self.customer = self.fixture.customer
        self.offering = make_openstack_offering(customer=self.customer)
        CustomerRole.OWNER.add_permission(PermissionEnum.CREATE_OFFERING_PLAN)
        CustomerRole.OWNER.add_permission(PermissionEnum.UPDATE_OFFERING_PLAN)

    def create_plan(self, offering, **extra):
        self.client.force_authenticate(self.fixture.owner)
        payload = {
            "name": "plan",
            "offering": factories.OfferingFactory.get_url(offering),
            "customer": structure_factories.CustomerFactory.get_url(self.customer),
            "unit": UnitPriceMixin.Units.PER_MONTH,
            **extra,
        }
        return self.client.post(factories.PlanFactory.get_provider_list_url(), payload)

    def test_billing_mode_defaults_to_inherit(self):
        response = self.create_plan(self.offering)
        self.assertEqual(response.status_code, status.HTTP_201_CREATED, response.data)
        self.assertEqual(response.data["billing_mode"], BillingModes.INHERIT)

    @data(BillingModes.LIMIT, BillingModes.USAGE)
    def test_mode_can_be_set_on_offering_with_builtin_components(self, mode):
        response = self.create_plan(self.offering, billing_mode=mode)
        self.assertEqual(response.status_code, status.HTTP_201_CREATED, response.data)
        plan = models.Plan.objects.get(uuid=response.data["uuid"])
        self.assertEqual(plan.billing_mode, mode)

    def test_mode_is_rejected_for_offering_without_builtin_components(self):
        offering = factories.OfferingFactory(
            customer=self.customer, type=BASIC_OFFERING
        )
        response = self.create_plan(offering, billing_mode=BillingModes.USAGE)
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertIn("billing_mode", response.data)

    def test_invalid_mode_is_rejected(self):
        response = self.create_plan(self.offering, billing_mode="prepaid")
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)

    def test_nested_components_are_resolved_for_the_plan(self):
        response = self.create_plan(self.offering, billing_mode=BillingModes.USAGE)
        components = {c["type"]: c for c in response.data["components"]}
        self.assertEqual(components["cores"]["billing_type"], BillingTypes.USAGE)
        self.assertEqual(components["cores"]["measured_unit"], "core-hours")
        self.assertEqual(components["ram"]["measured_unit"], "GB-hours")
        self.assertFalse(components["cores"]["is_prepaid"])

    def test_plan_type_reflects_the_mode(self):
        usage_plan = factories.PlanFactory(
            offering=self.offering, billing_mode=BillingModes.USAGE
        )
        limit_plan = factories.PlanFactory(offering=self.offering)
        for plan in (usage_plan, limit_plan):
            for component in self.offering.components.all():
                factories.PlanComponentFactory(plan=plan, component=component, price=1)
        self.client.force_authenticate(self.fixture.owner)
        usage = self.client.get(factories.PlanFactory.get_url(usage_plan)).data
        limit = self.client.get(factories.PlanFactory.get_url(limit_plan)).data
        self.assertEqual(usage["plan_type"], "usage-based")
        self.assertEqual(limit["plan_type"], "limit")

    def test_mode_can_be_updated_while_unused(self):
        plan = factories.PlanFactory(offering=self.offering)
        self.client.force_authenticate(self.fixture.owner)
        response = self.client.patch(
            factories.PlanFactory.get_url(plan), {"billing_mode": BillingModes.USAGE}
        )
        self.assertEqual(response.status_code, status.HTTP_200_OK, response.data)
        plan.refresh_from_db()
        self.assertEqual(plan.billing_mode, BillingModes.USAGE)

    def test_mode_update_is_blocked_once_resources_use_the_plan(self):
        plan = factories.PlanFactory(offering=self.offering)
        factories.ResourceFactory(offering=self.offering, plan=plan)
        self.client.force_authenticate(self.fixture.owner)
        response = self.client.patch(
            factories.PlanFactory.get_url(plan), {"billing_mode": BillingModes.USAGE}
        )
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        plan.refresh_from_db()
        self.assertEqual(plan.billing_mode, BillingModes.INHERIT)

    def test_mode_update_is_rejected_without_builtin_components(self):
        offering = factories.OfferingFactory(
            customer=self.customer, type=BASIC_OFFERING
        )
        plan = factories.PlanFactory(offering=offering)
        self.client.force_authenticate(self.fixture.owner)
        response = self.client.patch(
            factories.PlanFactory.get_url(plan), {"billing_mode": BillingModes.LIMIT}
        )
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)

    def test_resource_flags_are_exposed_per_plan(self):
        usage_plan = factories.PlanFactory(
            offering=self.offering, billing_mode=BillingModes.USAGE
        )
        resource = factories.ResourceFactory(
            offering=self.offering, plan=usage_plan, project=self.fixture.project
        )
        self.client.force_authenticate(self.fixture.staff)
        response = self.client.get(factories.ResourceFactory.get_url(resource))
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertTrue(response.data["is_usage_based"])
        self.assertFalse(response.data["is_limit_based"])


class PlanAdminFormTest(TestCase):
    """Django admin applies the same rules as the API."""

    def form(self, plan, mode):
        from django.forms.models import modelform_factory

        from waldur_mastermind.marketplace.admin import PlanAdminForm

        form_class = modelform_factory(
            models.Plan, form=PlanAdminForm, fields=["billing_mode"]
        )
        return form_class(data={"billing_mode": mode}, instance=plan)

    def test_mode_needs_builtin_components(self):
        plan = factories.PlanFactory()
        form = self.form(plan, BillingModes.USAGE)
        self.assertFalse(form.is_valid())
        self.assertIn("builtin", form.errors["billing_mode"][0])

    def test_mode_is_frozen_while_resources_use_the_plan(self):
        plan = factories.PlanFactory(offering=make_openstack_offering())
        factories.ResourceFactory(offering=plan.offering, plan=plan)
        form = self.form(plan, BillingModes.USAGE)
        self.assertFalse(form.is_valid())
        self.assertIn("resources use", form.errors["billing_mode"][0])

    def test_unused_plan_changes_mode(self):
        plan = factories.PlanFactory(offering=make_openstack_offering())
        self.assertTrue(self.form(plan, BillingModes.USAGE).is_valid())
