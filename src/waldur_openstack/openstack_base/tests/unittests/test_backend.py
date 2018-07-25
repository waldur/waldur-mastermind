import pickle
import six

from unittest import TestCase

from cinderclient import exceptions as cinder_exceptions
from ddt import ddt, data
from glanceclient import exc as glance_exceptions
from keystoneclient import exceptions as keystone_exceptions
from neutronclient.client import exceptions as neutron_exceptions
from novaclient import exceptions as nova_exceptions

from waldur_openstack.openstack_base.backend import OpenStackBackendError


@ddt
class TestOpenStackBackendError(TestCase):
    def setUp(self):
        self.cinder_client_exception = cinder_exceptions.ClientException(404)
        self.glance_client_exception = glance_exceptions.ClientException()
        self.keystone_client_exception = keystone_exceptions.ClientException()
        self.neutron_client_exception = neutron_exceptions.NeutronClientException()
        self.nova_client_exception = nova_exceptions.ClientException(404)

    @data('cinder', 'glance', 'keystone', 'neutron', 'nova')
    def test_reraised_client_exception_is_serializable(self, exception_type):
        test_exception = getattr(self, '%s_client_exception' % exception_type)
        try:
            raise test_exception
        except test_exception.__class__ as e:
            try:
                six.reraise(OpenStackBackendError, e)
            except OpenStackBackendError as e:
                self._test_exception_is_serializable(e)

    def _test_exception_is_serializable(self, exc):
        try:
            pickle.loads(pickle.dumps(exc))
        except Exception as e:
            self.fail('Reraised exception is not serializable: %s' % str(e))
