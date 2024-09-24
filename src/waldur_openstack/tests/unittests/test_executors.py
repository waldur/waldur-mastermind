from django.test import TestCase

from waldur_openstack.executors import (
    InstanceFloatingIPsUpdateExecutor,
)
from waldur_openstack.tests import factories
from waldur_openstack.tests.factories import FloatingIPFactory, PortFactory


class InstanceFloatingIPsUpdateExecutorTest(TestCase):
    def setUp(self):
        self.executor = InstanceFloatingIPsUpdateExecutor()
        self.instance = factories.InstanceFactory()

    def test_executor_does_not_return_empty_message_if_no_ips_have_been_updated(self):
        floating_ip = FloatingIPFactory()
        floating_ip.port = PortFactory()
        floating_ip.save()
        self.instance.ports.add(floating_ip.port)
        self.instance.save()

        self.instance._new_floating_ips = [floating_ip]
        self.instance._old_floating_ips = [floating_ip]
        result = self.executor.get_action_details(self.instance)

        self.assertFalse(result["attached"])
        self.assertFalse(result["detached"])
        self.assertEqual(result["message"], "Instance floating IPs have been updated.")
