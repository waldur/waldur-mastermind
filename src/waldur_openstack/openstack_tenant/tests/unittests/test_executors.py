from django.test import TestCase

from waldur_openstack.openstack_tenant.executors import InstanceFloatingIPsUpdateExecutor

from .. import factories


class InstanceFloatingIPsUpdateExecutorTest(TestCase):

    def setUp(self):
        self.executor = InstanceFloatingIPsUpdateExecutor()
        self.instance = factories.InstanceFactory()

    def test_executor_does_not_return_empty_message_if_no_ips_have_been_updated(self):
        floating_ip = factories.FloatingIPFactory()
        floating_ip.internal_ip = factories.InternalIPFactory()
        floating_ip.save()
        self.instance.internal_ips_set.add(floating_ip.internal_ip)
        self.instance.save()

        self.instance._new_floating_ips = [floating_ip]
        self.instance._old_floating_ips = [floating_ip]
        result = self.executor.get_action_details(self.instance)

        self.assertFalse(result['attached'])
        self.assertFalse(result['detached'])
        self.assertEqual(result['message'], 'Instance floating IPs have been updated.')
