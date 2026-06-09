from io import StringIO
from unittest import mock

from django.core.management import call_command
from django.test import SimpleTestCase
from rest_framework import test

from waldur_mastermind.marketplace.enums import OPENSTACK_TENANT_OFFERING
from waldur_mastermind.marketplace.tests import factories as marketplace_factories
from waldur_mastermind.marketplace.utils import create_offering_components
from waldur_mastermind.marketplace_openstack import (
    CORES_TYPE,
    RAM_TYPE,
    utils,
)
from waldur_openstack.tests import factories as openstack_factories
from waldur_openstack.tests import fixtures as openstack_fixtures
from waldur_openstack.tests.fixtures import mock_session


class ReconcileInstanceAllocationTest(SimpleTestCase):
    """Unit tests for the pure comparison core — no database, no Placement."""

    tracked = {CORES_TYPE, RAM_TYPE}

    def test_matching_allocation_produces_no_drift(self):
        flavor = {CORES_TYPE: 2, RAM_TYPE: 2048}
        # DISK_GB is reported by Placement but deliberately ignored.
        placement = {"VCPU": 2, "MEMORY_MB": 2048, "DISK_GB": 10}
        drifts = utils.reconcile_instance_allocation(
            flavor, placement, self.tracked, flag_untracked=True
        )
        self.assertEqual(drifts, [])

    def test_disk_gb_is_ignored(self):
        # instance.disk is Cinder volumes, not Placement DISK_GB; any mismatch
        # here must not produce drift, even with --flag-untracked.
        flavor = {CORES_TYPE: 2, RAM_TYPE: 2048}
        placement = {"VCPU": 2, "MEMORY_MB": 2048, "DISK_GB": 5}
        drifts = utils.reconcile_instance_allocation(
            flavor, placement, self.tracked, flag_untracked=True
        )
        self.assertEqual(drifts, [])

    def test_extra_vgpu_is_flagged_as_missing_component(self):
        flavor = {CORES_TYPE: 2, RAM_TYPE: 2048}
        placement = {"VCPU": 2, "MEMORY_MB": 2048, "DISK_GB": 10, "VGPU": 1}
        drifts = utils.reconcile_instance_allocation(
            flavor, placement, self.tracked, flag_untracked=True
        )
        self.assertEqual(len(drifts), 1)
        drift = drifts[0]
        self.assertEqual(drift["resource_class"], "VGPU")
        self.assertEqual(drift["billed"], 0)
        self.assertEqual(drift["actual"], 1)
        self.assertEqual(drift["severity"], utils.DriftSeverity.HIGH)
        self.assertEqual(drift["tag"], "no matching OfferingComponent")

    def test_vgpu_not_flagged_without_flag(self):
        flavor = {CORES_TYPE: 2, RAM_TYPE: 2048}
        placement = {"VCPU": 2, "MEMORY_MB": 2048, "VGPU": 1}
        drifts = utils.reconcile_instance_allocation(
            flavor, placement, self.tracked, flag_untracked=False
        )
        self.assertEqual(drifts, [])

    def test_higher_placement_cpu_is_under_billed_high(self):
        flavor = {CORES_TYPE: 2, RAM_TYPE: 2048}
        placement = {"VCPU": 4, "MEMORY_MB": 2048}
        drifts = utils.reconcile_instance_allocation(
            flavor, placement, self.tracked, flag_untracked=False
        )
        self.assertEqual(len(drifts), 1)
        drift = drifts[0]
        self.assertEqual(drift["resource_class"], CORES_TYPE)
        self.assertEqual(drift["billed"], 2)
        self.assertEqual(drift["actual"], 4)
        self.assertEqual(drift["severity"], utils.DriftSeverity.HIGH)
        self.assertEqual(drift["tag"], "under-billed")

    def test_lower_placement_cpu_is_over_billed_medium(self):
        flavor = {CORES_TYPE: 2, RAM_TYPE: 2048}
        placement = {"VCPU": 1, "MEMORY_MB": 2048}
        drifts = utils.reconcile_instance_allocation(
            flavor, placement, self.tracked, flag_untracked=False
        )
        self.assertEqual(len(drifts), 1)
        self.assertEqual(drifts[0]["severity"], utils.DriftSeverity.MEDIUM)
        self.assertEqual(drifts[0]["tag"], "over-billed")

    def test_allocations_are_summed_across_resource_providers(self):
        allocations = {
            "rp-host": {"resources": {"VCPU": 4, "MEMORY_MB": 8192}, "generation": 1},
            "rp-gpu": {"resources": {"VGPU": 1}, "generation": 1},
        }
        totals = utils.aggregate_placement_allocations(allocations)
        self.assertEqual(totals, {"VCPU": 4, "MEMORY_MB": 8192, "VGPU": 1})

    def test_empty_allocations_aggregate_to_empty_dict(self):
        self.assertEqual(utils.aggregate_placement_allocations({}), {})
        self.assertEqual(utils.aggregate_placement_allocations(None), {})


class DetectUntrackedVolumeTypesTest(SimpleTestCase):
    def test_volume_type_in_use_without_component_is_flagged(self):
        usages = {"vcpu": 4, "ram": 8192, "gigabytes_ssd": 50}
        drifts = utils.detect_untracked_volume_types(usages, {CORES_TYPE, RAM_TYPE})
        self.assertEqual(len(drifts), 1)
        drift = drifts[0]
        self.assertEqual(drift["resource_class"], "gigabytes_ssd")
        self.assertEqual(drift["billed"], 0)
        self.assertEqual(drift["actual"], 50)
        self.assertEqual(drift["severity"], utils.DriftSeverity.HIGH)
        self.assertEqual(drift["tag"], "no matching OfferingComponent")

    def test_volume_type_with_component_is_not_flagged(self):
        usages = {"gigabytes_ssd": 50}
        drifts = utils.detect_untracked_volume_types(usages, {"gigabytes_ssd"})
        self.assertEqual(drifts, [])

    def test_zero_usage_is_not_flagged(self):
        usages = {"gigabytes_ssd": 0}
        drifts = utils.detect_untracked_volume_types(usages, set())
        self.assertEqual(drifts, [])

    def test_non_volume_type_quotas_are_ignored(self):
        usages = {"vcpu": 4, "ram": 8192, "instances": 2}
        drifts = utils.detect_untracked_volume_types(usages, set())
        self.assertEqual(drifts, [])


class ValidateOpenstackBillingCommandTest(test.APITestCase):
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

        self.instance = self.fixture.instance
        self.instance.cores = 2
        self.instance.ram = 2048
        self.instance.disk = 10 * 1024
        self.instance.backend_id = "consumer-uuid-1"
        self.instance.save()

    def _set_storage_mode(self, mode):
        offering = self.resource.offering
        offering.plugin_options = {"storage_mode": mode}
        offering.save()

    def tearDown(self):
        super().tearDown()
        mock.patch.stopall()

    def _run(self, allocations, **opts):
        out = StringIO()
        mock_session()
        patcher = mock.patch(
            "waldur_mastermind.marketplace_openstack.management.commands."
            "validate_openstack_billing.get_placement_client"
        )
        self.mock_get_client = patcher.start()
        self.mock_get_client.return_value.get_allocations.return_value = allocations
        try:
            call_command("validate_openstack_billing", stdout=out, **opts)
            exit_code = 0
        except SystemExit as e:
            exit_code = e.code
        return out.getvalue(), exit_code

    def test_matching_allocation_reports_no_drift(self):
        output, exit_code = self._run(
            {"rp": {"resources": {"VCPU": 2, "MEMORY_MB": 2048, "DISK_GB": 10}}}
        )
        self.assertIn("No drift", output)
        self.assertEqual(exit_code, 0)

    def test_extra_vgpu_reported_with_flag(self):
        output, exit_code = self._run(
            {
                "rp": {
                    "resources": {
                        "VCPU": 2,
                        "MEMORY_MB": 2048,
                        "DISK_GB": 10,
                        "VGPU": 1,
                    }
                }
            },
            flag_untracked=True,
        )
        self.assertIn("VGPU", output)
        self.assertIn("no matching OfferingComponent", output)
        self.assertEqual(exit_code, 1)

    def test_disk_mismatch_is_ignored(self):
        # Placement DISK_GB differs from instance.disk, but storage is not
        # reconciled against Placement, so no drift.
        output, exit_code = self._run(
            {"rp": {"resources": {"VCPU": 2, "MEMORY_MB": 2048, "DISK_GB": 5}}}
        )
        self.assertIn("No drift", output)
        self.assertEqual(exit_code, 0)

    def test_cpu_under_billing_reported(self):
        output, exit_code = self._run(
            {"rp": {"resources": {"VCPU": 4, "MEMORY_MB": 2048, "DISK_GB": 10}}}
        )
        self.assertIn(CORES_TYPE, output)
        self.assertIn("under-billed", output)
        self.assertEqual(exit_code, 1)

    def test_empty_placement_record_does_not_crash(self):
        output, exit_code = self._run({})
        self.assertIn("No drift", output)
        self.assertEqual(exit_code, 0)

    def test_untracked_volume_type_flagged_in_dynamic_mode(self):
        self._set_storage_mode("dynamic")
        self.tenant.add_quota_usage("gigabytes_ssd", 50)
        output, exit_code = self._run({}, flag_untracked=True)
        self.assertIn("gigabytes_ssd", output)
        self.assertIn("no matching OfferingComponent", output)
        self.assertIn("tenant", output)
        self.assertEqual(exit_code, 1)

    def test_volume_type_not_flagged_in_fixed_mode(self):
        # Default is fixed mode: all storage rolls into the `storage` component,
        # so there is no per-type under-billing to flag.
        self.tenant.add_quota_usage("gigabytes_ssd", 50)
        output, exit_code = self._run({}, flag_untracked=True)
        self.assertIn("No drift", output)
        self.assertEqual(exit_code, 0)

    def test_null_backend_id_instance_is_excluded(self):
        openstack_factories.InstanceFactory(
            tenant=self.tenant, project=self.fixture.project, backend_id=None
        )
        self._run({"rp": {"resources": {"VCPU": 2, "MEMORY_MB": 2048}}})
        called_consumers = [
            call.args[0]
            for call in self.mock_get_client.return_value.get_allocations.call_args_list
        ]
        self.assertNotIn(None, called_consumers)
        self.assertIn("consumer-uuid-1", called_consumers)

    def test_service_settings_filter_excludes_other_settings(self):
        openstack_factories.InstanceFactory(backend_id="consumer-uuid-2", cores=99)
        output, exit_code = self._run(
            {"rp": {"resources": {"VCPU": 2, "MEMORY_MB": 2048, "DISK_GB": 10}}},
            service_settings=self.fixture.settings.uuid.hex,
        )
        self.assertNotIn("consumer-uuid-2", output)
        self.assertEqual(exit_code, 0)
