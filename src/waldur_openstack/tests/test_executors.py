from django.test import SimpleTestCase

from waldur_openstack.executors import InstanceCreateExecutor


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
