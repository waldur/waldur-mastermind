from django.test import TestCase
from rest_framework import serializers as rf_serializers

from waldur_mastermind.marketplace.tests import factories as marketplace_factories
from waldur_mastermind.marketplace_openstack import (
    CORES_TYPE,
    RAM_TYPE,
    STORAGE_MODE_DYNAMIC,
    STORAGE_MODE_FIXED,
    STORAGE_TYPE,
)
from waldur_mastermind.marketplace_openstack.serializers import (
    MarketplaceTenantCreateSerializer,
)
from waldur_mastermind.marketplace_openstack.utils import (
    fill_omitted_tenant_limits,
    import_limits,
    map_limits_to_quotas,
    tenant_limit_plan_validator,
)
from waldur_openstack.models import Tenant
from waldur_openstack.tests import fixtures as openstack_fixtures


class TestMapLimitsToQuotas(TestCase):
    def setUp(self):
        self.offering = marketplace_factories.OfferingFactory()

    def test_map_limits_to_quotas_includes_max_security_groups_on_create(self):
        """Test that max_security_groups plugin option is mapped to security_group_count quota during creation"""
        # Setup offering with plugin options
        self.offering.plugin_options = {
            "max_instances": 10,
            "max_volumes": 20,
            "max_security_groups": 15,
        }

        # Setup limits
        limits = {
            CORES_TYPE: 8,
            RAM_TYPE: 16384,
            STORAGE_TYPE: 100,
        }

        # Test quota mapping for creation
        quotas = map_limits_to_quotas(limits, self.offering, is_create=True)

        # Verify all quotas are mapped correctly
        self.assertEqual(quotas[Tenant.Quotas.vcpu.name], 8)
        self.assertEqual(quotas[Tenant.Quotas.ram.name], 16384)
        self.assertEqual(quotas[Tenant.Quotas.storage.name], 100)
        self.assertEqual(quotas[Tenant.Quotas.instances.name], 10)
        self.assertEqual(quotas[Tenant.Quotas.volumes.name], 20)
        self.assertEqual(quotas[Tenant.Quotas.security_group_count.name], 15)

    def test_map_limits_to_quotas_excludes_max_security_groups_on_update(self):
        """Test that max_security_groups is not included during updates (is_create=False)"""
        # Setup offering with plugin options
        self.offering.plugin_options = {
            "max_instances": 10,
            "max_volumes": 20,
            "max_security_groups": 15,
        }

        # Setup limits
        limits = {
            CORES_TYPE: 8,
            RAM_TYPE: 16384,
            STORAGE_TYPE: 100,
        }

        # Test quota mapping for update
        quotas = map_limits_to_quotas(limits, self.offering, is_create=False)

        # Verify only vcpu, ram, and storage are included for updates
        self.assertEqual(quotas[Tenant.Quotas.vcpu.name], 8)
        self.assertEqual(quotas[Tenant.Quotas.ram.name], 16384)
        self.assertEqual(quotas[Tenant.Quotas.storage.name], 100)

        # Verify creation-only quotas are not included
        self.assertNotIn(Tenant.Quotas.instances.name, quotas)
        self.assertNotIn(Tenant.Quotas.volumes.name, quotas)
        self.assertNotIn(Tenant.Quotas.security_group_count.name, quotas)

    def test_map_limits_to_quotas_handles_missing_max_security_groups(self):
        """Test that missing max_security_groups plugin option is handled gracefully"""
        # Setup offering without max_security_groups
        self.offering.plugin_options = {
            "max_instances": 10,
            "max_volumes": 20,
        }

        # Setup limits
        limits = {
            CORES_TYPE: 8,
            RAM_TYPE: 16384,
            STORAGE_TYPE: 100,
        }

        # Test quota mapping for creation
        quotas = map_limits_to_quotas(limits, self.offering, is_create=True)

        # Verify other quotas are mapped correctly
        self.assertEqual(quotas[Tenant.Quotas.vcpu.name], 8)
        self.assertEqual(quotas[Tenant.Quotas.ram.name], 16384)
        self.assertEqual(quotas[Tenant.Quotas.storage.name], 100)
        self.assertEqual(quotas[Tenant.Quotas.instances.name], 10)
        self.assertEqual(quotas[Tenant.Quotas.volumes.name], 20)

        # Verify security_group_count is not included when plugin option is missing
        self.assertNotIn(Tenant.Quotas.security_group_count.name, quotas)

    def test_map_limits_to_quotas_filters_none_values(self):
        """Test that None values are filtered out from quotas"""
        # Setup offering with some None values
        self.offering.plugin_options = {
            "max_instances": None,
            "max_volumes": 20,
            "max_security_groups": None,
        }

        # Setup limits with some None values
        limits = {
            CORES_TYPE: None,
            RAM_TYPE: 16384,
            STORAGE_TYPE: 100,
        }

        # Test quota mapping for creation
        quotas = map_limits_to_quotas(limits, self.offering, is_create=True)

        # Verify only non-None quotas are included
        self.assertNotIn(Tenant.Quotas.vcpu.name, quotas)
        self.assertEqual(quotas[Tenant.Quotas.ram.name], 16384)
        self.assertEqual(quotas[Tenant.Quotas.storage.name], 100)
        self.assertNotIn(Tenant.Quotas.instances.name, quotas)
        self.assertEqual(quotas[Tenant.Quotas.volumes.name], 20)
        self.assertNotIn(Tenant.Quotas.security_group_count.name, quotas)

    def test_map_limits_to_quotas_keeps_zero_values(self):
        self.offering.plugin_options = {"storage_mode": STORAGE_MODE_FIXED}
        limits = {CORES_TYPE: 0, RAM_TYPE: 0, STORAGE_TYPE: 0}

        quotas = map_limits_to_quotas(limits, self.offering, is_create=True)

        self.assertEqual(quotas[Tenant.Quotas.vcpu.name], 0)
        self.assertEqual(quotas[Tenant.Quotas.ram.name], 0)
        self.assertEqual(quotas[Tenant.Quotas.storage.name], 0)

    def test_omitted_quotas_are_left_unchanged_on_update(self):
        self.offering.plugin_options = {"storage_mode": STORAGE_MODE_FIXED}

        quotas = map_limits_to_quotas({CORES_TYPE: 4}, self.offering, is_create=False)

        self.assertEqual(quotas, {Tenant.Quotas.vcpu.name: 4})


class TestFillOmittedTenantLimits(TestCase):
    def setUp(self):
        self.offering = marketplace_factories.OfferingFactory(
            plugin_options={"storage_mode": STORAGE_MODE_FIXED}
        )
        self.limit_types = {CORES_TYPE, RAM_TYPE, STORAGE_TYPE}

    def fill(self, limits):
        return fill_omitted_tenant_limits(limits, self.offering, self.limit_types)

    def test_omitted_limits_are_filled_with_zero(self):
        self.assertEqual(
            self.fill({CORES_TYPE: 4}),
            {CORES_TYPE: 4, RAM_TYPE: 0, STORAGE_TYPE: 0},
        )

    def test_empty_limits_are_kept_empty(self):
        self.assertEqual(self.fill({}), {})

    def test_storage_is_not_filled_in_dynamic_mode(self):
        self.offering.plugin_options = {"storage_mode": STORAGE_MODE_DYNAMIC}
        self.assertEqual(self.fill({CORES_TYPE: 4}), {CORES_TYPE: 4, RAM_TYPE: 0})

    def test_component_billed_by_usage_is_not_filled(self):
        self.limit_types = {STORAGE_TYPE}
        self.assertEqual(self.fill({STORAGE_TYPE: 100}), {STORAGE_TYPE: 100})


class TestTenantLimitPlanValidator(TestCase):
    def test_zero_limits_are_accepted(self):
        tenant_limit_plan_validator({CORES_TYPE: 0, RAM_TYPE: 0, STORAGE_TYPE: 0})

    def test_zero_volume_type_limit_counts_as_storage(self):
        tenant_limit_plan_validator({CORES_TYPE: 0, RAM_TYPE: 0, "gigabytes_ssd": 0})

    def test_missing_limit_is_rejected(self):
        for limits in (
            {RAM_TYPE: 1, STORAGE_TYPE: 1},
            {CORES_TYPE: 1, STORAGE_TYPE: 1},
            {CORES_TYPE: 1, RAM_TYPE: 1},
        ):
            with self.subTest(limits=limits):
                with self.assertRaises(rf_serializers.ValidationError):
                    tenant_limit_plan_validator(limits)


class TestTenantCreateQuotas(TestCase):
    def validate_quotas(self, quotas):
        field = MarketplaceTenantCreateSerializer().fields["quotas"]
        return field.run_validation(quotas)

    def test_zero_quotas_are_accepted(self):
        self.assertEqual(
            self.validate_quotas({"vcpu": 0, "ram": 0, "storage": 0}),
            {"vcpu": 0, "ram": 0, "storage": 0},
        )

    def test_negative_or_fractional_quota_is_rejected(self):
        for value in (-1, 1.5, "abc"):
            with self.subTest(value=value):
                with self.assertRaises(rf_serializers.ValidationError):
                    self.validate_quotas({"vcpu": value})


class TestImportLimits(TestCase):
    def setUp(self):
        self.fixture = openstack_fixtures.OpenStackFixture()
        self.tenant = self.fixture.tenant
        self.offering = marketplace_factories.OfferingFactory(
            scope=self.tenant.service_settings
        )
        self.resource = marketplace_factories.ResourceFactory(
            offering=self.offering,
            scope=self.tenant,
        )

    def test_zero_backend_quota_is_imported_as_zero(self):
        # A backend quota of 0 used to be imported as -1 (unlimited).
        self.offering.plugin_options = {"storage_mode": STORAGE_MODE_FIXED}
        self.offering.save()

        self.tenant.set_quota_limit(Tenant.Quotas.vcpu.name, 0)
        self.tenant.set_quota_limit(Tenant.Quotas.ram.name, 0)
        self.tenant.set_quota_limit(Tenant.Quotas.storage.name, 0)

        import_limits(self.resource)

        self.resource.refresh_from_db()
        self.assertEqual(
            self.resource.limits,
            {CORES_TYPE: 0, RAM_TYPE: 0, STORAGE_TYPE: 0},
        )

    def test_zero_volume_type_quota_is_imported_as_zero(self):
        self.offering.plugin_options = {"storage_mode": STORAGE_MODE_DYNAMIC}
        self.offering.save()

        self.tenant.set_quota_limit("gigabytes_ssd", 0)
        self.tenant.set_quota_limit("gigabytes_rbd", 10)

        import_limits(self.resource)

        self.resource.refresh_from_db()
        self.assertEqual(self.resource.limits["gigabytes_ssd"], 0)
        self.assertEqual(self.resource.limits["gigabytes_rbd"], 10)
