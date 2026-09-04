from django.test import SimpleTestCase, TestCase

from waldur_openstack import executors
from waldur_openstack.executors import InstanceCreateExecutor
from waldur_openstack.tests import factories


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
