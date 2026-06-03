from decimal import Decimal

from freezegun import freeze_time
from rest_framework import test

from waldur_mastermind.marketplace import models as marketplace_models
from waldur_mastermind.marketplace.callbacks import create_resource_plan_period
from waldur_mastermind.marketplace.enums import (
    OPENSTACK_TENANT_OFFERING,
    BillingTypes,
)
from waldur_mastermind.marketplace.tests import factories as marketplace_factories
from waldur_mastermind.marketplace.utils import create_offering_components
from waldur_openstack import models as openstack_models
from waldur_openstack import signals as openstack_signals
from waldur_openstack.tests import fixtures as openstack_fixtures

TenantQuotas = openstack_models.Tenant.Quotas


@freeze_time("2019-01-01")
class UsagesSynchronizationTest(test.APITransactionTestCase):
    def setUp(self):
        super().setUp()
        self.fixture = openstack_fixtures.OpenStackFixture()
        self.tenant = self.fixture.tenant
        self.resource = marketplace_factories.ResourceFactory(
            scope=self.tenant, plan=marketplace_factories.PlanFactory()
        )
        self.resource.offering.type = OPENSTACK_TENANT_OFFERING
        self.resource.offering.save()
        create_offering_components(self.resource.offering)
        create_resource_plan_period(self.resource)

    def assert_usage_equal(self, name, value):
        self.resource.refresh_from_db()
        self.assertEqual(self.resource.current_usages[name], value)
        component_usage = marketplace_models.ComponentUsage.objects.get(
            resource=self.resource, component__type=name
        )
        self.assertEqual(component_usage.usage, value)

    def test_cores_usage_is_synchronized(self):
        self.tenant.set_quota_usage("vcpu", 10)
        self.assert_usage_equal("cores", 10)

    def test_ram_usage_is_synchronized(self):
        self.tenant.set_quota_usage("ram", 20 * 1024)
        self.assert_usage_equal("ram", 20 * 1024)

    def test_storage_usage_is_synchronized(self):
        self.tenant.set_quota_usage("storage", 100 * 1024)
        self.assert_usage_equal("storage", 100 * 1024)

    def test_decreasing_cores_usage_is_reflected_in_current_usages(self):
        """When tenant vCPU usage drops (e.g. instance deleted),
        marketplace_resource.current_usages['cores'] must follow the drop
        instead of latching at the in-month peak."""
        self.tenant.set_quota_usage("vcpu", 143)
        self.assert_usage_equal("cores", 143)

        self.tenant.set_quota_usage("vcpu", 127)

        self.resource.refresh_from_db()
        self.assertEqual(self.resource.current_usages["cores"], 127)

    def test_decreasing_ram_usage_is_reflected_in_current_usages(self):
        """When tenant RAM usage drops, marketplace_resource.current_usages['ram']
        must follow the drop instead of latching at the in-month peak."""
        self.tenant.set_quota_usage("ram", 291840)
        self.assert_usage_equal("ram", 291840)

        self.tenant.set_quota_usage("ram", 259072)

        self.resource.refresh_from_db()
        self.assertEqual(self.resource.current_usages["ram"], 259072)

    def test_last_sync_is_updated_after_tenant_quota_pull(self):
        """marketplace_resource.last_sync must advance when a tenant quota
        pull runs, not stay frozen at the resource's creation timestamp."""
        initial = self.resource.last_sync

        with freeze_time("2019-01-15"):
            self.tenant.set_quota_usage("vcpu", 10)

        self.resource.refresh_from_db()
        self.assertGreater(self.resource.last_sync, initial)

    def test_stable_usage_resyncs_via_tenant_quotas_pulled_signal(self):
        """A tenant with steady-state usage produces no QuotaUsage delta on
        subsequent polls, so the post_save chain never fires. The pull-completion
        signal must still trigger import_usage so the current-month ComponentUsage
        row is materialised and limit_usage stops returning 0."""
        self.tenant.set_quota_usage("vcpu", 10)
        marketplace_models.ComponentUsage.objects.filter(
            resource=self.resource, component__type="cores"
        ).delete()
        self.resource.current_usages = {}
        self.resource.save(update_fields=["current_usages"])

        self.tenant.set_quota_usage("vcpu", 10)
        self.assertFalse(
            marketplace_models.ComponentUsage.objects.filter(
                resource=self.resource, component__type="cores"
            ).exists()
        )

        openstack_signals.tenant_quotas_pulled.send(
            openstack_models.Tenant, instance=self.tenant
        )

        self.assert_usage_equal("cores", 10)


@freeze_time("2026-04-15 12:00:00")
class UsageBasedTenantSynchronizationTest(test.APITransactionTestCase):
    """End-to-end usage sync for an OpenStack tenant offering with a
    usage-billed component, exercising the hourly accumulator path that
    `import_usage` selects when the offering has any USAGE component."""

    def setUp(self):
        super().setUp()
        self.fixture = openstack_fixtures.OpenStackFixture()
        self.tenant = self.fixture.tenant
        self.resource = marketplace_factories.ResourceFactory(
            scope=self.tenant, plan=marketplace_factories.PlanFactory()
        )
        self.resource.offering.type = OPENSTACK_TENANT_OFFERING
        self.resource.offering.save()
        create_offering_components(self.resource.offering)
        cores_component = marketplace_models.OfferingComponent.objects.get(
            offering=self.resource.offering, type="cores"
        )
        cores_component.billing_type = BillingTypes.USAGE
        cores_component.save(update_fields=["billing_type"])
        create_resource_plan_period(self.resource)

    def test_cores_usage_accumulates_on_first_poll(self):
        """First pull defaults to a 1-hour elapsed window: usage = current × 1h."""
        self.tenant.set_quota_usage("vcpu", 10)
        usage = marketplace_models.ComponentUsage.objects.get(
            resource=self.resource, component__type="cores"
        )
        self.assertEqual(usage.usage, Decimal("10.00"))

    def test_cores_usage_accumulates_over_multiple_polls(self):
        """A subsequent pull within the same billing period adds
        current × hours_since_last_poll to the running total."""
        self.tenant.set_quota_usage("vcpu", 10)
        with freeze_time("2026-04-15 15:00:00"):
            self.tenant.set_quota_usage("vcpu", 12)
        usage = marketplace_models.ComponentUsage.objects.get(
            resource=self.resource, component__type="cores"
        )
        # First: 10 × 1h = 10; second: 12 × 3h = 36; total = 46.
        self.assertEqual(usage.usage, Decimal("46.00"))

    def test_poll_record_is_created_for_usage_component(self):
        """The accumulator path persists a ComponentUsagePollRecord so staff
        can audit the hourly increments behind the billing total."""
        self.tenant.set_quota_usage("vcpu", 10)
        record = marketplace_models.ComponentUsagePollRecord.objects.get(
            resource=self.resource, component__type="cores"
        )
        self.assertEqual(record.raw_usage, Decimal("10.00"))
        self.assertEqual(record.accumulated_total, Decimal("10.00"))

    def test_limit_component_in_same_offering_still_uses_high_watermark(self):
        """When a usage-billed component is present, the offering is in
        accumulator mode — but LIMIT-typed components in the same offering
        must continue using the high-watermark logic, not accumulation."""
        self.tenant.set_quota_usage("ram", 20 * 1024)
        with freeze_time("2026-04-15 15:00:00"):
            self.tenant.set_quota_usage("ram", 15 * 1024)
        usage = marketplace_models.ComponentUsage.objects.get(
            resource=self.resource, component__type="ram"
        )
        # max(20*1024, 15*1024) = 20*1024 — the peak is retained.
        self.assertEqual(usage.usage, 20 * 1024)
