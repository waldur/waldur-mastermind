from unittest import mock

from waldur_core.core import utils as core_utils
from waldur_mastermind.marketplace import models as marketplace_models
from waldur_mastermind.marketplace.enums import (
    OPENSTACK_INSTANCE_OFFERING,
    OPENSTACK_TENANT_OFFERING,
    OPENSTACK_VOLUME_OFFERING,
    OfferingStates,
)
from waldur_mastermind.marketplace.tests import factories as marketplace_factories
from waldur_mastermind.marketplace_openstack import tasks, utils
from waldur_openstack.tests import factories as os_factories
from waldur_openstack.tests.fixtures import OpenStackFixture

from .utils import BaseOpenStackTest, override_plugin_settings


class SelfHealTenantOfferingsTest(BaseOpenStackTest):
    """Tests for self_heal_tenant_offerings — covers the offering-side gap."""

    def setUp(self):
        super().setUp()
        self.fixture = OpenStackFixture()
        self.tenant = self.fixture.tenant
        # Tenant must have its own marketplace Resource with a parent offering
        # so create_offerings_for_volume_and_instance can derive parent_offering.
        self.parent_offering = marketplace_factories.OfferingFactory(
            type=OPENSTACK_TENANT_OFFERING,
            scope=self.fixture.settings,
            customer=self.fixture.customer,
        )
        self.tenant_resource = marketplace_factories.ResourceFactory(
            scope=self.tenant,
            project=self.fixture.project,
            offering=self.parent_offering,
        )

    def test_recreates_missing_offerings(self):
        result = utils.self_heal_tenant_offerings(self.tenant)

        self.assertEqual(result[OPENSTACK_INSTANCE_OFFERING], "recreated")
        self.assertEqual(result[OPENSTACK_VOLUME_OFFERING], "recreated")
        self.assertTrue(
            marketplace_models.Offering.objects.filter(
                type=OPENSTACK_INSTANCE_OFFERING, scope=self.tenant
            ).exists()
        )
        self.assertTrue(
            marketplace_models.Offering.objects.filter(
                type=OPENSTACK_VOLUME_OFFERING, scope=self.tenant
            ).exists()
        )

    def test_unarchives_archived_offering(self):
        instance_offering = marketplace_factories.OfferingFactory(
            type=OPENSTACK_INSTANCE_OFFERING,
            scope=self.tenant,
            customer=self.fixture.customer,
            project=self.fixture.project,
            state=OfferingStates.ARCHIVED,
        )

        result = utils.self_heal_tenant_offerings(self.tenant)

        self.assertEqual(result[OPENSTACK_INSTANCE_OFFERING], "unarchived")
        instance_offering.refresh_from_db()
        self.assertEqual(instance_offering.state, OfferingStates.ACTIVE)

    def test_ok_when_offerings_already_healthy(self):
        marketplace_factories.OfferingFactory(
            type=OPENSTACK_INSTANCE_OFFERING,
            scope=self.tenant,
            customer=self.fixture.customer,
            project=self.fixture.project,
            state=OfferingStates.ACTIVE,
        )
        marketplace_factories.OfferingFactory(
            type=OPENSTACK_VOLUME_OFFERING,
            scope=self.tenant,
            customer=self.fixture.customer,
            project=self.fixture.project,
            state=OfferingStates.ACTIVE,
        )

        result = utils.self_heal_tenant_offerings(self.tenant)

        self.assertEqual(result[OPENSTACK_INSTANCE_OFFERING], "ok")
        self.assertEqual(result[OPENSTACK_VOLUME_OFFERING], "ok")

    def test_skips_when_tenant_has_no_marketplace_resource(self):
        self.tenant_resource.delete()

        result = utils.self_heal_tenant_offerings(self.tenant)

        self.assertEqual(result[OPENSTACK_INSTANCE_OFFERING], "skipped_no_parent")
        self.assertEqual(result[OPENSTACK_VOLUME_OFFERING], "skipped_no_parent")
        self.assertFalse(
            marketplace_models.Offering.objects.filter(
                type=OPENSTACK_INSTANCE_OFFERING, scope=self.tenant
            ).exists()
        )

    def test_skips_when_multiple_offerings_exist(self):
        marketplace_factories.OfferingFactory(
            type=OPENSTACK_INSTANCE_OFFERING,
            scope=self.tenant,
            customer=self.fixture.customer,
            project=self.fixture.project,
            state=OfferingStates.ACTIVE,
        )
        marketplace_factories.OfferingFactory(
            type=OPENSTACK_INSTANCE_OFFERING,
            scope=self.tenant,
            customer=self.fixture.customer,
            project=self.fixture.project,
            state=OfferingStates.ACTIVE,
        )

        result = utils.self_heal_tenant_offerings(self.tenant)

        self.assertEqual(result[OPENSTACK_INSTANCE_OFFERING], "skipped_multiple")

    @override_plugin_settings(AUTOMATICALLY_CREATE_PRIVATE_OFFERING=False)
    def test_respects_disabled_flag_when_offering_missing(self):
        result = utils.self_heal_tenant_offerings(self.tenant)

        self.assertEqual(result[OPENSTACK_INSTANCE_OFFERING], "skipped_disabled")
        self.assertEqual(result[OPENSTACK_VOLUME_OFFERING], "skipped_disabled")
        self.assertFalse(
            marketplace_models.Offering.objects.filter(
                type=OPENSTACK_INSTANCE_OFFERING, scope=self.tenant
            ).exists()
        )


class SelfHealTenantOrphanResourcesTest(BaseOpenStackTest):
    """Tests for self_heal_tenant_orphan_resources — covers the orphan-resource gap."""

    def setUp(self):
        super().setUp()
        self.fixture = OpenStackFixture()
        self.tenant = self.fixture.tenant

    def _make_offerings(self):
        marketplace_factories.OfferingFactory(
            type=OPENSTACK_INSTANCE_OFFERING,
            scope=self.tenant,
            customer=self.fixture.customer,
            project=self.fixture.project,
            state=OfferingStates.ACTIVE,
        )
        marketplace_factories.OfferingFactory(
            type=OPENSTACK_VOLUME_OFFERING,
            scope=self.tenant,
            customer=self.fixture.customer,
            project=self.fixture.project,
            state=OfferingStates.ACTIVE,
        )

    def test_creates_resources_for_orphan_instances_and_volumes(self):
        self._make_offerings()
        instance = self.fixture.instance
        volume = self.fixture.volume

        result = utils.self_heal_tenant_orphan_resources(self.tenant)

        self.assertEqual(result["instances_healed"], 1)
        self.assertEqual(result["volumes_healed"], 1)
        self.assertTrue(
            marketplace_models.Resource.objects.filter(scope=instance).exists()
        )
        self.assertTrue(
            marketplace_models.Resource.objects.filter(scope=volume).exists()
        )

    def test_reports_skipped_when_offering_missing(self):
        # No offerings created. Orphan instance exists.
        instance = self.fixture.instance

        result = utils.self_heal_tenant_orphan_resources(self.tenant)

        self.assertEqual(result["instances_healed"], 0)
        self.assertEqual(result["instances_skipped_no_offering"], 1)
        self.assertFalse(
            marketplace_models.Resource.objects.filter(scope=instance).exists()
        )

    def test_idempotent(self):
        self._make_offerings()
        instance = self.fixture.instance

        first = utils.self_heal_tenant_orphan_resources(self.tenant)
        second = utils.self_heal_tenant_orphan_resources(self.tenant)

        self.assertEqual(first["instances_healed"], 1)
        self.assertEqual(second["instances_healed"], 0)
        # Exactly one marketplace Resource for the instance.
        self.assertEqual(
            marketplace_models.Resource.objects.filter(scope=instance).count(), 1
        )

    def test_skips_instances_with_existing_resource(self):
        self._make_offerings()
        instance = self.fixture.instance
        marketplace_factories.ResourceFactory(
            scope=instance,
            project=self.fixture.project,
            offering=marketplace_models.Offering.objects.get(
                type=OPENSTACK_INSTANCE_OFFERING, scope=self.tenant
            ),
        )

        result = utils.self_heal_tenant_orphan_resources(self.tenant)

        self.assertEqual(result["instances_healed"], 0)
        self.assertEqual(
            marketplace_models.Resource.objects.filter(scope=instance).count(), 1
        )


class SelfHealLogNoiseTest(BaseOpenStackTest):
    """When the per-tenant offering is broken (duplicates, no parent, disabled),
    every orphan resource fired its own ERROR. With ~hundreds of orphans on a
    single tenant this dominates the error log and obscures real failures.
    The orphan pass must collapse those into a single summary ERROR per
    resource class.
    """

    def setUp(self):
        super().setUp()
        self.fixture = OpenStackFixture()
        self.tenant = self.fixture.tenant

    def _seed_orphans(self, count):
        for _ in range(count):
            os_factories.InstanceFactory(
                tenant=self.tenant,
                project=self.fixture.project,
                service_settings=self.fixture.settings,
            )

    def test_duplicate_offerings_log_once_per_class_not_per_orphan(self):
        # Two per-tenant Instance offerings -> skipped_multiple.
        for _ in range(2):
            marketplace_factories.OfferingFactory(
                type=OPENSTACK_INSTANCE_OFFERING,
                scope=self.tenant,
                customer=self.fixture.customer,
                project=self.fixture.project,
                state=OfferingStates.ACTIVE,
            )
        marketplace_factories.OfferingFactory(
            type=OPENSTACK_VOLUME_OFFERING,
            scope=self.tenant,
            customer=self.fixture.customer,
            project=self.fixture.project,
            state=OfferingStates.ACTIVE,
        )
        self._seed_orphans(5)  # five orphan Instances

        with self.assertLogs(
            "waldur_mastermind.marketplace_openstack.utils", level="ERROR"
        ) as cm:
            result = utils.self_heal_tenant_marketplace_model(self.tenant)

        # Only one ERROR line per resource class should mention "could not link"
        # (the per-orphan loop must be collapsed). The tenant-level offering
        # warning emitted by self_heal_tenant_offerings is allowed.
        orphan_link_errors = [
            line
            for line in cm.output
            if line.startswith("ERROR") and "could not link" in line
        ]
        self.assertEqual(
            len(orphan_link_errors),
            1,
            "Expected exactly one orphan-link ERROR for the broken Instance "
            f"offering; got {len(orphan_link_errors)}:\n{orphan_link_errors}",
        )
        # Counter still reflects the real count.
        self.assertEqual(result["instances_skipped_no_offering"], 5)
        # And the summary mentions the count and the failure reason.
        self.assertIn("5", orphan_link_errors[0])
        self.assertIn("skipped_multiple", orphan_link_errors[0])

    def test_orphans_logged_individually_when_offering_is_healthy(self):
        # Original behavior preserved when the broken-offering optimization
        # doesn't apply: each true failure of
        # create_marketplace_resource_for_imported_resources still surfaces.
        # Here the offerings are healthy and orphans heal successfully — no
        # ERROR lines at all.
        marketplace_factories.OfferingFactory(
            type=OPENSTACK_INSTANCE_OFFERING,
            scope=self.tenant,
            customer=self.fixture.customer,
            project=self.fixture.project,
            state=OfferingStates.ACTIVE,
        )
        marketplace_factories.OfferingFactory(
            type=OPENSTACK_VOLUME_OFFERING,
            scope=self.tenant,
            customer=self.fixture.customer,
            project=self.fixture.project,
            state=OfferingStates.ACTIVE,
        )
        self._seed_orphans(3)

        with self.assertNoLogs(
            "waldur_mastermind.marketplace_openstack.utils", level="ERROR"
        ):
            result = utils.self_heal_tenant_orphan_resources(self.tenant)

        self.assertEqual(result["instances_healed"], 3)

    def test_skipped_no_parent_short_circuits_orphan_loop(self):
        # Offering creation skipped because the tenant has no parent
        # marketplace Resource. Orphans must surface as a single summary
        # ERROR per class, same shape as skipped_multiple.
        self._seed_orphans(4)
        offering_actions = {
            OPENSTACK_INSTANCE_OFFERING: "skipped_no_parent",
            OPENSTACK_VOLUME_OFFERING: "skipped_no_parent",
        }

        with self.assertLogs(
            "waldur_mastermind.marketplace_openstack.utils", level="ERROR"
        ) as cm:
            result = utils.self_heal_tenant_orphan_resources(
                self.tenant, offering_actions
            )

        # InstanceFactory implicitly creates a system Volume per instance,
        # so 4 orphan Instances also yield 4 orphan Volumes — both classes
        # collapse to one ERROR each (2 total). The key invariant is that
        # there is NOT one ERROR per resource.
        orphan_link_errors = [
            line
            for line in cm.output
            if line.startswith("ERROR") and "could not link" in line
        ]
        self.assertEqual(len(orphan_link_errors), 2)
        joined = "\n".join(orphan_link_errors)
        self.assertIn("Instance", joined)
        self.assertIn("Volume", joined)
        self.assertIn("skipped_no_parent", joined)
        self.assertEqual(result["instances_skipped_no_offering"], 4)

    def test_skipped_disabled_short_circuits_orphan_loop(self):
        # AUTOMATICALLY_CREATE_PRIVATE_OFFERING flag is off; offering can't
        # be recreated. Same collapse behavior — one ERROR per class.
        self._seed_orphans(2)
        offering_actions = {
            OPENSTACK_INSTANCE_OFFERING: "skipped_disabled",
            OPENSTACK_VOLUME_OFFERING: "skipped_disabled",
        }

        with self.assertLogs(
            "waldur_mastermind.marketplace_openstack.utils", level="ERROR"
        ) as cm:
            result = utils.self_heal_tenant_orphan_resources(
                self.tenant, offering_actions
            )

        orphan_link_errors = [
            line
            for line in cm.output
            if line.startswith("ERROR") and "could not link" in line
        ]
        self.assertEqual(len(orphan_link_errors), 2)
        joined = "\n".join(orphan_link_errors)
        self.assertIn("skipped_disabled", joined)
        self.assertEqual(result["instances_skipped_no_offering"], 2)

    def test_recreated_does_not_short_circuit_orphan_loop(self):
        # When self_heal_tenant_offerings successfully (re)creates the
        # offering, the per-orphan loop MUST run so new marketplace
        # Resources are created. A regression that adds "recreated" to
        # the broken set would silently skip the heal.
        marketplace_factories.OfferingFactory(
            type=OPENSTACK_INSTANCE_OFFERING,
            scope=self.tenant,
            customer=self.fixture.customer,
            project=self.fixture.project,
            state=OfferingStates.ACTIVE,
        )
        marketplace_factories.OfferingFactory(
            type=OPENSTACK_VOLUME_OFFERING,
            scope=self.tenant,
            customer=self.fixture.customer,
            project=self.fixture.project,
            state=OfferingStates.ACTIVE,
        )
        self._seed_orphans(3)
        offering_actions = {
            OPENSTACK_INSTANCE_OFFERING: "recreated",
            OPENSTACK_VOLUME_OFFERING: "recreated",
        }

        result = utils.self_heal_tenant_orphan_resources(self.tenant, offering_actions)

        self.assertEqual(result["instances_healed"], 3)


class SelfHealTenantMarketplaceModelTest(BaseOpenStackTest):
    """Tests for the top-level orchestrator: offering heal must run before orphan heal."""

    def setUp(self):
        super().setUp()
        self.fixture = OpenStackFixture()
        self.tenant = self.fixture.tenant
        self.parent_offering = marketplace_factories.OfferingFactory(
            type=OPENSTACK_TENANT_OFFERING,
            scope=self.fixture.settings,
            customer=self.fixture.customer,
        )
        marketplace_factories.ResourceFactory(
            scope=self.tenant,
            project=self.fixture.project,
            offering=self.parent_offering,
        )

    def test_offering_recreated_then_orphan_resource_created_in_one_pass(self):
        instance = self.fixture.instance

        result = utils.self_heal_tenant_marketplace_model(self.tenant)

        self.assertEqual(result[OPENSTACK_INSTANCE_OFFERING], "recreated")
        self.assertEqual(result["instances_healed"], 1)
        self.assertTrue(
            marketplace_models.Resource.objects.filter(scope=instance).exists()
        )


class SyncTaskSelfHealIntegrationTest(BaseOpenStackTest):
    """End-to-end: the sync task calls self-heal before the importer."""

    def setUp(self):
        super().setUp()
        self.fixture = OpenStackFixture()
        self.tenant = self.fixture.tenant
        self.parent_offering = marketplace_factories.OfferingFactory(
            type=OPENSTACK_TENANT_OFFERING,
            scope=self.fixture.settings,
            customer=self.fixture.customer,
        )
        marketplace_factories.ResourceFactory(
            scope=self.tenant,
            project=self.fixture.project,
            offering=self.parent_offering,
        )

    @mock.patch("waldur_mastermind.marketplace_openstack.utils.OpenStackBackend")
    def test_sync_task_heals_offerings_and_orphan_resources(self, mock_backend_cls):
        mock_backend = mock_backend_cls.return_value
        mock_backend.get_importable_instances.return_value = []
        mock_backend.get_importable_volumes.return_value = []
        mock_backend.get_expired_instances.return_value = []
        mock_backend.get_expired_volumes.return_value = []
        instance = self.fixture.instance

        tasks.sync_instances_and_volumes_of_tenant(
            core_utils.serialize_instance(self.tenant)
        )

        self.assertTrue(
            marketplace_models.Offering.objects.filter(
                type=OPENSTACK_INSTANCE_OFFERING, scope=self.tenant
            ).exists()
        )
        self.assertTrue(
            marketplace_models.Resource.objects.filter(scope=instance).exists()
        )

    @mock.patch("waldur_mastermind.marketplace_openstack.utils.OpenStackBackend")
    @mock.patch(
        "waldur_mastermind.marketplace_openstack.utils.self_heal_tenant_marketplace_model"
    )
    def test_sync_task_continues_when_self_heal_raises(
        self, mock_self_heal, mock_backend_cls
    ):
        mock_self_heal.side_effect = Exception("boom")
        mock_backend = mock_backend_cls.return_value
        mock_backend.get_importable_instances.return_value = []
        mock_backend.get_importable_volumes.return_value = []
        mock_backend.get_expired_instances.return_value = []
        mock_backend.get_expired_volumes.return_value = []

        # Must not raise.
        tasks.sync_instances_and_volumes_of_tenant(
            core_utils.serialize_instance(self.tenant)
        )

        mock_backend.get_importable_instances.assert_called_once()
