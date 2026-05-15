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
