from unittest import mock

from django.test import SimpleTestCase, TestCase

from waldur_core.core import utils as core_utils
from waldur_core.core.enums import CoreStates
from waldur_openstack import executors, tasks
from waldur_openstack.exceptions import OpenStackBackendError
from waldur_openstack.executors import InstanceCreateExecutor
from waldur_openstack.tests import factories, fixtures


class InstanceCreateVolumesTest(SimpleTestCase):
    def test_volume_pull_during_creation_does_not_refresh_bootable_flag(self):
        # The post-creation pull_volume task must not refresh the "bootable"
        # field. Cinder may still report bootable="false" in the window right
        # after the volume becomes available, which would clear the flag the
        # serializer set on a system volume and make create_instance fail its
        # `volumes.get(bootable=True)` guard (regression for
        # PUHURI-PORTALS-T2B).
        tasks = InstanceCreateExecutor.create_volumes(["openstack.volume:1"])

        pull_tasks = [
            task
            for task in tasks
            if len(task.args) > 1 and task.args[1] == "pull_volume"
        ]
        self.assertTrue(pull_tasks, "expected a pull_volume task in the chain")
        for task in pull_tasks:
            self.assertNotIn("bootable", task.kwargs.get("update_fields", []))


class TenantCreateQuotasTest(TestCase):
    def setUp(self):
        self.tenant = factories.TenantFactory()

    def get_pushed_quotas(self):
        chain = executors.get_tenant_create_tasks(self.tenant)
        for task in chain.tasks:
            if len(task.args) > 1 and task.args[1] == "push_tenant_quotas":
                return task.args[2]
        self.fail("expected a push_tenant_quotas task in the chain")

    def test_stored_zero_limit_is_pushed_as_zero(self):
        # A stored 0 used to be coerced to -1, so Nova and Cinder received
        # "unlimited" for a quota the operator explicitly zeroed.
        self.tenant.set_quota_limit("vcpu", 0)
        self.tenant.set_quota_limit("gigabytes_ssd", 0)

        quotas = self.get_pushed_quotas()

        self.assertEqual(quotas["vcpu"], 0)
        self.assertEqual(quotas["gigabytes_ssd"], 0)

    def test_quota_without_a_limit_row_is_not_pushed(self):
        quotas = self.get_pushed_quotas()

        self.assertNotIn("vcpu", quotas)

    def test_pushed_quotas_match_the_stored_limits(self):
        limits = {"vcpu": 0, "ram": 0, "storage": 0, "gigabytes_ssd": 0}
        for name, value in limits.items():
            self.tenant.set_quota_limit(name, value)

        self.assertEqual(self.get_pushed_quotas(), limits)


class VolumeExtendExecutorTest(TestCase):
    def setUp(self):
        self.fixture = fixtures.OpenStackFixture()
        self.volume = self.fixture.volume

    def get_backend_methods(self):
        signature = executors.VolumeExtendExecutor.get_task_signature(
            self.volume, core_utils.serialize_instance(self.volume)
        )
        return [
            task.kwargs["backend_method"]
            for task in signature.tasks
            if "backend_method" in task.kwargs
        ]

    def attach_to_stopped_instance(self):
        self.volume.instance = self.fixture.instance
        self.volume.runtime_state = "in-use"
        self.volume.save()

    def set_live_resize(self, enabled):
        settings = self.volume.service_settings
        settings.options["live_resize_of_volumes_enabled"] = enabled
        settings.save()

    def test_attached_volume_is_detached_when_live_resize_is_disabled(self):
        self.set_live_resize(False)
        self.attach_to_stopped_instance()

        self.assertEqual(
            ["detach_volume", "extend_volume", "attach_volume"],
            self.get_backend_methods(),
        )

    def test_attached_volume_is_extended_in_place_when_live_resize_is_enabled(self):
        self.set_live_resize(True)
        self.attach_to_stopped_instance()

        self.assertEqual(["extend_volume"], self.get_backend_methods())

    def test_detached_volume_is_extended_in_place(self):
        self.set_live_resize(False)

        self.assertEqual(["extend_volume"], self.get_backend_methods())

    def test_failure_signature_keeps_instance_of_attached_volume(self):
        self.attach_to_stopped_instance()

        signature = executors.VolumeExtendExecutor.get_failure_signature(
            self.volume, core_utils.serialize_instance(self.volume)
        )

        self.assertEqual(
            self.fixture.instance.uuid.hex, signature.kwargs["instance_uuid"]
        )


@mock.patch("waldur_openstack.backend.OpenStackBackend.attach_volume")
class VolumeExtendErredTaskTest(TestCase):
    def setUp(self):
        self.fixture = fixtures.OpenStackFixture()
        self.volume = self.fixture.volume
        self.instance = self.fixture.instance
        self.instance.state = CoreStates.UPDATING
        self.instance.save()
        # The extend chain has already detached the volume.
        self.volume.state = CoreStates.UPDATING
        self.volume.save()

    def run_task(self):
        task = tasks.VolumeExtendErredTask()
        task.result = mock.Mock(result="Extend failed.", traceback="")
        task.execute(
            self.volume, instance_uuid=self.instance.uuid.hex, device="/dev/vdb"
        )
        self.volume.refresh_from_db()
        self.instance.refresh_from_db()

    def test_detached_volume_is_reattached_and_instance_is_erred(self, attach_volume):
        self.run_task()

        attach_volume.assert_called_once_with(
            self.volume, self.instance.uuid.hex, "/dev/vdb"
        )
        self.assertEqual(CoreStates.ERRED, self.volume.state)
        self.assertEqual(CoreStates.ERRED, self.instance.state)

    def test_failed_reattach_is_reported_in_volume_error(self, attach_volume):
        attach_volume.side_effect = OpenStackBackendError("Attach failed.")

        self.run_task()

        self.assertIn(self.instance.name, self.volume.error_message)
        self.assertEqual(CoreStates.ERRED, self.volume.state)
        self.assertEqual(CoreStates.ERRED, self.instance.state)

    def test_instance_is_erred_even_if_reattach_raises_unexpectedly(
        self, attach_volume
    ):
        attach_volume.side_effect = ConnectionError("Nova is unreachable.")

        with self.assertRaises(ConnectionError):
            self.run_task()

        self.instance.refresh_from_db()
        self.assertEqual(CoreStates.ERRED, self.instance.state)
