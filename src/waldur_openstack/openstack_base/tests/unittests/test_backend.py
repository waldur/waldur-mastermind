import pickle  # noqa: S403
from unittest import TestCase

from cinderclient import exceptions as cinder_exceptions
from glanceclient import exc as glance_exceptions
from keystoneclient import exceptions as keystone_exceptions
from neutronclient.client import exceptions as neutron_exceptions
from novaclient import exceptions as nova_exceptions

from waldur_openstack.openstack_base.exceptions import OpenStackBackendError


class TestOpenStackBackendError(TestCase):
    def test_reraised_client_exception_is_serializable(self):
        for test_exception in [
            cinder_exceptions.ClientException(404),
            glance_exceptions.ClientException(),
            keystone_exceptions.ClientException(),
            neutron_exceptions.NeutronClientException(),
            nova_exceptions.ClientException(404),
        ]:
            try:
                raise test_exception
            except test_exception.__class__ as e:
                try:
                    raise OpenStackBackendError(e)
                except OpenStackBackendError as e:
                    try:
                        pickle.loads(pickle.dumps(test_exception))  # noqa: S301
                    except Exception as e:
                        self.fail("Reraised exception is not serializable: %s" % str(e))
