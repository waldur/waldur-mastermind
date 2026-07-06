from unittest import mock

from django.test import SimpleTestCase
from rest_framework import test

from waldur_mastermind.marketplace import models as marketplace_models
from waldur_mastermind.marketplace.enums import OPENSTACK_TENANT_OFFERING
from waldur_mastermind.marketplace.tests import factories as marketplace_factories
from waldur_mastermind.marketplace.utils import create_offering_components
from waldur_mastermind.marketplace_openstack import (
    CORES_TYPE,
    RAM_TYPE,
    STORAGE_TYPE,
    utils,
)
from waldur_openstack.backend import OpenStackBackendError
from waldur_openstack.tests import fixtures as openstack_fixtures
from waldur_openstack.tests.fixtures import mock_session


class MergePlacementUsagesTest(SimpleTestCase):
    """Unit tests for the pure overlay — no database, no Placement."""

    def test_cores_and_ram_are_taken_from_placement(self):
        quota = {CORES_TYPE: 2, RAM_TYPE: 2048, STORAGE_TYPE: 10240}
        placement = {"VCPU": 4, "MEMORY_MB": 8192}
        result = utils.merge_placement_usages(quota, placement)
        self.assertEqual(result[CORES_TYPE], 4)
        self.assertEqual(result[RAM_TYPE], 8192)

    def test_storage_is_left_untouched(self):
        # Placement DISK_GB is ephemeral disk, not Cinder volumes: storage must
        # keep the Cinder-quota value regardless of what Placement reports.
        quota = {CORES_TYPE: 2, RAM_TYPE: 2048, STORAGE_TYPE: 10240}
        placement = {"VCPU": 2, "MEMORY_MB": 2048, "DISK_GB": 5}
        result = utils.merge_placement_usages(quota, placement)
        self.assertEqual(result[STORAGE_TYPE], 10240)

    def test_specialty_class_is_added_lowercased(self):
        quota = {CORES_TYPE: 2, RAM_TYPE: 2048}
        placement = {"VCPU": 2, "MEMORY_MB": 2048, "VGPU": 1}
        result = utils.merge_placement_usages(quota, placement)
        self.assertEqual(result["vgpu"], 1)

    def test_missing_placement_compute_zeroes_components(self):
        # No instances reported by Placement -> compute usage is zero; storage
        # (Cinder) is still preserved.
        quota = {CORES_TYPE: 2, RAM_TYPE: 2048, STORAGE_TYPE: 10240}
        result = utils.merge_placement_usages(quota, {})
        self.assertEqual(result[CORES_TYPE], 0)
        self.assertEqual(result[RAM_TYPE], 0)
        self.assertEqual(result[STORAGE_TYPE], 10240)

    def test_zero_specialty_amount_is_not_added(self):
        quota = {CORES_TYPE: 2, RAM_TYPE: 2048}
        result = utils.merge_placement_usages(quota, {"VCPU": 2, "VGPU": 0})
        self.assertNotIn("vgpu", result)


class ImportUsageFromPlacementTest(test.APITestCase):
    def setUp(self):
        super().setUp()
        self.fixture = openstack_fixtures.OpenStackFixture()
        self.tenant = self.fixture.tenant
        self.resource = marketplace_factories.ResourceFactory(
            scope=self.tenant, plan=marketplace_factories.PlanFactory()
        )
        offering = self.resource.offering
        offering.type = OPENSTACK_TENANT_OFFERING
        offering.plugin_options = {"billing_source": "placement"}
        offering.save()
        create_offering_components(offering)

        self.instance = self.fixture.instance
        self.instance.cores = 2
        self.instance.ram = 2048
        self.instance.disk = 10 * 1024
        self.instance.backend_id = "consumer-uuid-1"
        self.instance.save()

    def tearDown(self):
        super().tearDown()
        mock.patch.stopall()

    def _add_component(self, component_type, name):
        return marketplace_factories.OfferingComponentFactory(
            offering=self.resource.offering, type=component_type, name=name
        )

    def _start_placement(self, allocations=None, side_effect=None):
        # Any quota mutation fires update_openstack_tenant_usages -> import_usage,
        # which in placement mode reaches Placement; install the mock before
        # touching quotas so those signal-driven calls hit the mock too.
        mock_session()
        patcher = mock.patch(
            "waldur_mastermind.marketplace_openstack.utils.get_placement_client"
        )
        client = patcher.start()
        if side_effect is not None:
            client.return_value.get_allocations.side_effect = side_effect
        else:
            client.return_value.get_allocations.return_value = allocations or {}
        return client

    def _import(self, allocations):
        self.mock_get_client = self._start_placement(allocations)
        utils.import_usage(self.resource)

    def _usage(self, component_type):
        return marketplace_models.ComponentUsage.objects.filter(
            resource=self.resource, component__type=component_type
        ).first()

    def test_steady_state_matches_flavor(self):
        # Nova reserves exactly the flavor amount, so Placement-derived usage
        # equals the flavor-derived numbers.
        self._import({"rp": {"resources": {"VCPU": 2, "MEMORY_MB": 2048}}})
        self.assertEqual(self._usage(CORES_TYPE).usage, 2)
        self.assertEqual(self._usage(RAM_TYPE).usage, 2048)

    def test_resize_reflects_active_allocation(self):
        # The DB flavor still says 2 cores, but Placement already holds the
        # resized allocation of 4 — billing must follow Placement.
        self._import({"rp": {"resources": {"VCPU": 4, "MEMORY_MB": 4096}}})
        self.assertEqual(self._usage(CORES_TYPE).usage, 4)
        self.assertEqual(self._usage(RAM_TYPE).usage, 4096)

    def test_vgpu_billed_when_component_exists(self):
        self._add_component("vgpu", "Virtual GPU")
        self._import({"rp": {"resources": {"VCPU": 2, "MEMORY_MB": 2048, "VGPU": 2}}})
        self.assertEqual(self._usage("vgpu").usage, 2)

    def test_vgpu_not_billed_without_component(self):
        with self.assertLogs(
            "waldur_mastermind.marketplace.utils", level="WARNING"
        ) as logs:
            self._import(
                {"rp": {"resources": {"VCPU": 2, "MEMORY_MB": 2048, "VGPU": 1}}}
            )
        self.assertIsNone(self._usage("vgpu"))
        self.assertTrue(
            any("OfferingComponent does not exist" in m for m in logs.output)
        )

    def test_disk_gb_does_not_affect_storage(self):
        # Storage stays on the Cinder quota; Placement DISK_GB is ignored.
        self._start_placement(
            {"rp": {"resources": {"VCPU": 2, "MEMORY_MB": 2048, "DISK_GB": 5}}}
        )
        self.tenant.set_quota_usage(self.tenant.Quotas.storage.name, 10240)
        utils.import_usage(self.resource)
        self.assertEqual(self._usage(STORAGE_TYPE).usage, 10240)

    def test_placement_unavailable_falls_back_to_quota(self):
        self._start_placement(side_effect=OpenStackBackendError("Placement is down"))
        self.tenant.set_quota_usage(self.tenant.Quotas.vcpu.name, 7)
        self.tenant.set_quota_usage(self.tenant.Quotas.ram.name, 14336)
        with self.assertLogs(
            "waldur_mastermind.marketplace_openstack.utils", level="WARNING"
        ) as logs:
            utils.import_usage(self.resource)
        self.assertEqual(self._usage(CORES_TYPE).usage, 7)
        self.assertEqual(self._usage(RAM_TYPE).usage, 14336)
        self.assertTrue(any("falling back to quota usage" in m for m in logs.output))

    def test_quota_source_does_not_call_placement(self):
        # Un-opted offering keeps the legacy flavor/quota path untouched.
        offering = self.resource.offering
        offering.plugin_options = {}
        offering.save()
        mock_session()
        patcher = mock.patch(
            "waldur_mastermind.marketplace_openstack.utils.get_placement_client"
        )
        mock_client = patcher.start()
        self.tenant.set_quota_usage(self.tenant.Quotas.vcpu.name, 3)
        self.tenant.set_quota_usage(self.tenant.Quotas.ram.name, 6144)
        utils.import_usage(self.resource)
        mock_client.assert_not_called()
        self.assertEqual(self._usage(CORES_TYPE).usage, 3)
